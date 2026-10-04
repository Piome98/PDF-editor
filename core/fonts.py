"""새로 쓰는 글자를 원본과 같은 글꼴·크기·색으로 만들기.

- 텍스트 PDF: PDF 안에 들어 있는 원본 글꼴을 그대로 다시 쓴다. 필요한 글자가 빠진 부분 글꼴이면
  같은 이름의 Windows 글꼴을 찾아 쓴다. 크기·색은 PDF에 적힌 값을 그대로 쓴다.
- 이미지 PDF(OCR): 글자 그림을 후보 글꼴로 직접 그려 비교해서 가장 닮은 글꼴과 크기를 고른다.
  색은 글자 가장자리 번짐(안티앨리어싱)을 감안해 보정하고, 새 글자는 원본 그림과 같은 해상도의
  그림으로 넣어 번짐 정도까지 맞춘다.
"""
from __future__ import annotations

import io
import math
import os
import re
import weakref
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pymupdf

from .words import Word

FALLBACK_FONT = "Malgun Gothic"

# 이미지 PDF에서 비교해 볼 글꼴 (설치된 것만 사용). 한국 공문서·견적서에 흔한 글꼴 위주
_CANDIDATE_FAMILIES = re.compile(
    r"^(Malgun Gothic|Gulim|GulimChe|Dotum|DotumChe|Batang|BatangChe|Gungsuh|"
    r"NanumGothic|NanumMyeongjo|NanumBarunGothic|Nanum Gothic|Nanum Myeongjo|"
    r"HCR Batang|HCR Dotum|Hancom.*|HY.*|"
    r"Arial|Times New Roman|Calibri|Segoe UI|Tahoma|Verdana|Courier New|Consolas)$", re.I)


# ───────────────────────── 설치된 글꼴 목록 ─────────────────────────

@dataclass(frozen=True)
class Face:
    path: str
    index: int          # .ttc 안의 몇 번째 글꼴인지
    family: str         # 영문 글꼴 이름 (예: Batang)
    family_local: str   # 한글 이름이 있으면 (예: 바탕)
    style: str          # Regular / Bold ...
    full: str
    postscript: str

    @property
    def bold(self) -> bool:
        return "bold" in self.style.lower()

    @property
    def italic(self) -> bool:
        s = self.style.lower()
        return "italic" in s or "oblique" in s

    @property
    def label(self) -> str:
        name = self.family_local or self.family
        return name if self.style.lower() in ("regular", "normal", "") else f"{name} {self.style}"


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    return [d for d in dirs if d.is_dir()]


def _names(tt) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for rec in tt["name"].names:
        if rec.nameID in (1, 2, 4, 6, 16, 17):
            try:
                text = rec.toUnicode().strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                out.setdefault(rec.nameID, []).append(text)
    return out


@lru_cache(maxsize=1)
def system_faces() -> tuple[Face, ...]:
    """Windows에 설치된 글꼴 목록 (처음 한 번 1~2초)."""
    from fontTools.ttLib import TTCollection, TTFont

    faces = []
    for d in _font_dirs():
        for path in sorted(d.iterdir()):
            ext = path.suffix.lower()
            if ext not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                fonts = TTCollection(str(path), lazy=True).fonts if ext == ".ttc" else [TTFont(str(path), lazy=True)]
            except Exception:  # noqa: BLE001
                continue
            for i, tt in enumerate(fonts):
                try:
                    n = _names(tt)
                except Exception:  # noqa: BLE001
                    continue
                fams = n.get(16) or n.get(1) or []
                if not fams:
                    continue
                ascii_fam = next((f for f in fams if f.isascii()), fams[0])
                local = next((f for f in fams if not f.isascii()), "")
                style = (n.get(17) or n.get(2) or ["Regular"])[0]
                full = next((f for f in n.get(4, []) if f.isascii()), ascii_fam)
                ps = (n.get(6) or [""])[0]
                faces.append(Face(str(path), i, ascii_fam, local, style, full, ps))
    return tuple(faces)


def _norm(name: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", name)                 # 부분 글꼴 접두어 (ABCDEF+Gulim)
    if any(0x80 <= ord(c) <= 0xFF for c in name):          # PDF에 cp949 바이트로 적힌 한글 이름
        try:
            name = name.encode("latin-1").decode("cp949")
        except UnicodeError:
            pass
    name = re.sub(r"[\s\-_,]", "", name).lower()
    return re.sub(r"(psmt|mt|ps)$", "", name)


_STYLE_WORDS = ("semibold", "bold", "italic", "oblique", "regular", "medium", "light", "black", "normal")


def _base(name: str) -> str:
    """스타일 단어를 뗀 이름. 'Arial-BoldMT' → 'arial', 'Batang Regular' → 'batang'"""
    q = _norm(name)
    for w in _STYLE_WORDS:
        q = q.replace(w, "")
    return q


def _is_bold(name: str) -> bool:
    return "bold" in _norm(name) and "semibold" not in _norm(name)


def find_face(name: str, bold: bool = False, italic: bool = False) -> Face | None:
    """PDF에 적힌 글꼴 이름(예: ArialMT, Arial-BoldMT, ABCDEF+Gulim, 굴림)으로 설치된 글꼴을 찾는다."""
    q = _norm(name)
    if not q:
        return None
    bold = bold or _is_bold(name)
    italic = italic or "italic" in q or "oblique" in q
    base = _base(name)
    best, best_score = None, 0
    for f in system_faces():
        exact = q in {_norm(f.postscript), _norm(f.full)}
        same_family = base and base in ({_base(f.family), _base(f.family_local), _base(f.postscript)} - {""})
        if not (exact or same_family):
            continue
        score = (4 if exact else 2) + (3 if f.bold == bold else 0) + (1 if f.italic == italic else 0)
        if score > best_score:
            best, best_score = f, score
    return best


def face_by_label(label: str) -> Face | None:
    return next((f for f in system_faces() if f.label == label), None)


def font_choices() -> list[str]:
    """영역 설정의 글꼴 선택 목록."""
    return sorted({f.label for f in system_faces()}, key=lambda s: (not any("\uac00" <= c <= "\ud7a3" for c in s), s))


@lru_cache(maxsize=64)
def face_bytes(face: Face) -> bytes:
    """PyMuPDF에 넣을 글꼴 데이터. .ttc 안의 두 번째 이후 글꼴은 따로 꺼내야 한다."""
    if face.index == 0:
        return Path(face.path).read_bytes()
    from fontTools.ttLib import TTCollection
    buf = io.BytesIO()
    TTCollection(face.path).fonts[face.index].save(buf)
    return buf.getvalue()


@lru_cache(maxsize=256)
def _cmap(face: Face) -> frozenset[int]:
    from fontTools.ttLib import TTFont
    tt = TTFont(face.path, fontNumber=face.index, lazy=True)
    return frozenset(tt.getBestCmap() or {})


def covers(face: Face, text: str) -> bool:
    cmap = _cmap(face)
    return all(ord(c) in cmap for c in text if not c.isspace())


# ───────────────────────── 결과 서식 ─────────────────────────

@dataclass
class TextStyle:
    font_label: str                              # 화면 표시용 글꼴 이름
    size: float
    color: tuple[float, float, float]
    source: str                                  # pdf(원본 글꼴) | system(같은 이름 글꼴) | matched(그림 비교) | user | fallback
    font_buffer: bytes | None = None             # 텍스트 PDF: 원본 글꼴 데이터
    face: Face | None = None                     # 설치된 글꼴
    raster_dpi: float | None = None              # 이미지 PDF: 이 해상도의 그림으로 넣는다
    raster_grid: tuple[float, float] | None = None  # 이미지 PDF: 원본 이미지 픽셀 격자의 기준점
    origin: tuple[float, float] | None = None    # 이미지 PDF: 원래 글자의 시작점(왼쪽 기준선) PDF 좌표
    old_advance: float = 0.0                     # 이미지 PDF: 원래 글자의 글자 폭(advance) pt

    def describe(self) -> str:
        how = {"pdf": "PDF 원본 글꼴", "system": "같은 이름의 설치 글꼴", "matched": "그림 비교로 추정",
               "user": "직접 지정", "fallback": "원본 글꼴을 찾지 못해 기본 글꼴"}[self.source]
        r, g, b = (round(c * 255) for c in self.color)
        return f"{self.font_label} · {self.size:.1f}pt · #{r:02X}{g:02X}{b:02X}  ({how})"


def _fallback_face() -> Face | None:
    return next((f for f in system_faces() if f.family == FALLBACK_FONT and not f.bold), None)


# ── 텍스트 PDF ──

_pdf_font_cache: "weakref.WeakKeyDictionary[pymupdf.Document, dict]" = weakref.WeakKeyDictionary()


def _embedded_fonts(doc: pymupdf.Document, page: pymupdf.Page) -> list[tuple[str, bytes]]:
    """이 쪽에서 쓰는, PDF 안에 들어 있는 글꼴들 (글꼴 이름, 글꼴 데이터)."""
    try:
        cache = _pdf_font_cache.setdefault(doc, {})
    except TypeError:          # 약한 참조를 지원하지 않으면 캐시 없이
        cache = {}
    out = []
    for xref, *_ in page.get_fonts(full=True):
        if xref not in cache:
            try:
                _, ext, _, buf = doc.extract_font(xref)
                cache[xref] = (pymupdf.Font(fontbuffer=buf).name, buf) if buf and ext not in ("n/a", "") else None
            except Exception:  # noqa: BLE001
                cache[xref] = None
        if cache[xref]:
            out.append(cache[xref])
    return out


def text_layer_style(doc: pymupdf.Document, page: pymupdf.Page, word: Word, new_text: str) -> TextStyle:
    color = word.color or (0, 0, 0)
    bold = word.bold or _is_bold(word.font)
    for name, buf in _embedded_fonts(doc, page):
        if _base(name) and _base(name) == _base(word.font) and _is_bold(name) == bold:
            font = pymupdf.Font(fontbuffer=buf)
            if all(font.has_glyph(ord(c)) for c in new_text if not c.isspace()):
                return TextStyle(word.font, word.size, color, "pdf", font_buffer=buf)
            break   # 원본 글꼴이 부분 글꼴이라 새 글자가 없음 → 같은 이름의 설치 글꼴
    face = find_face(word.font, bold, word.italic)
    if face and covers(face, new_text):
        return TextStyle(face.label, word.size, color, "system", face=face)
    fb = _fallback_face()
    return TextStyle(fb.label if fb else "Helvetica", word.size, color, "fallback", face=fb)


# ── 이미지 PDF (OCR) ──

@lru_cache(maxsize=512)
def _pil_font(face: Face, px: float):
    from PIL import ImageFont
    return ImageFont.truetype(face.path, max(px, 1.0), index=face.index, layout_engine=ImageFont.Layout.BASIC)


_SUPERSAMPLE = 4


def _alpha(face: Face, px: float, text: str,
           shift: tuple[float, float] = (0.0, 0.0)) -> tuple[np.ndarray, tuple[float, float]]:
    """글자를 그린 알파(0~1) 배열과, 그림 안에서 '왼쪽 기준선' 원점의 위치.

    PDF 뷰어처럼 힌팅 없이 그리기 위해 4배 크게 그린 뒤 평균으로 줄인다.
    (힌팅은 작은 글자의 높이·간격을 픽셀 격자에 맞춰 바꿔서 원본과 크기가 3~4% 어긋난다)
    """
    from PIL import Image, ImageDraw
    ss = _SUPERSAMPLE
    font = _pil_font(face, px * ss)
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    pad = 2 * ss
    w = int(math.ceil((right - left + 2 * pad) / ss) + 1) * ss
    h = int(math.ceil((bottom - top + 2 * pad) / ss) + 1) * ss
    img = Image.new("L", (max(w, ss), max(h, ss)), 0)
    origin = (pad - left + shift[0] * ss, pad - top + shift[1] * ss)
    ImageDraw.Draw(img).text(origin, text, font=font, fill=255, anchor="ls")
    a = np.asarray(img, np.float32) / 255
    a = a.reshape(a.shape[0] // ss, ss, a.shape[1] // ss, ss).mean(axis=(1, 3))
    return a, (origin[0] / ss, origin[1] / ss)


def _resize(a: np.ndarray, w: int, h: int) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((max(w, 1), max(h, 1)), Image.BILINEAR))


def _bbox(a: np.ndarray, thr: float = 0.25) -> tuple[int, int, int, int] | None:
    rows, cols = np.flatnonzero((a > thr).any(axis=1)), np.flatnonzero((a > thr).any(axis=0))
    if not len(rows) or not len(cols):
        return None
    return rows[0], rows[-1] + 1, cols[0], cols[-1] + 1


def _target(word: Word) -> tuple[np.ndarray, np.ndarray, tuple[int, int]] | None:
    """원본 글자 그림 → (덮임 정도 0~1, 컬러 픽셀 0~1, 잘라 낸 위치). 둘 다 잉크 범위로 잘라 둔다."""
    if word.patch_rgb is None:
        return None
    rgb = word.patch_rgb.astype(np.float32) / 255
    bg = np.array(word.bg or (1, 1, 1), np.float32)
    lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
    lum_bg = float(bg @ np.array([0.299, 0.587, 0.114], np.float32))
    lum_ink = float(np.percentile(lum, 3))
    if lum_bg - lum_ink < 0.08:
        return None
    alpha = np.clip((lum_bg - lum) / (lum_bg - lum_ink), 0, 1)
    box = _bbox(alpha)
    if box is None:
        return None
    y0, y1, x0, x1 = box
    return alpha[y0:y1, x0:x1], rgb[y0:y1, x0:x1], (int(y0), int(x0))


def _model_full(face: Face, size_pt: float, word: Word) -> tuple[np.ndarray, tuple[float, float]] | None:
    """원본 이미지가 만들어진 과정을 흉내 낸 글자 그림: 원본 해상도로 그린 뒤 OCR 해상도로 확대.

    돌려주는 값: (잉크 범위로 자른 그림, 그 그림 안에서 원점(왼쪽 기준선)의 위치 (y, x))
    """
    dpi = word.src_dpi or word.px_scale * 72
    alpha, (ox, oy) = _alpha(face, size_pt * dpi / 72, word.text)
    zoom = word.px_scale * 72 / dpi
    up = _resize(alpha, round(alpha.shape[1] * zoom), round(alpha.shape[0] * zoom))
    box = _bbox(up)
    if box is None:
        return None
    y0, y1, x0, x1 = box
    return up[y0:y1, x0:x1], (oy * zoom - y0, ox * zoom - x0)


def _model(face: Face, size_pt: float, word: Word) -> np.ndarray | None:
    full = _model_full(face, size_pt, word)
    return full[0] if full else None


def _fit(face: Face, word: Word) -> tuple[float, float, np.ndarray, np.ndarray] | None:
    """(닮은 정도 -1~1, 글자 크기 pt, 맞춘 모델 그림, 원본 컬러 픽셀).

    후보 글꼴로 그린 글자를 원본과 같은 크기로 맞춘 뒤 픽셀 밝기의 상관계수로 비교한다.
    획 굵기·삐침(세리프)·글자 폭이 모두 반영된다.
    """
    if not covers(face, word.text):
        return None
    tgt = _target(word)
    if tgt is None:
        return None
    t_alpha, t_rgb, _ = tgt
    th, tw = t_alpha.shape
    size = word.size or 10.0
    for _ in range(2):                         # 글자 높이가 맞도록 크기를 두 번 보정
        m = _model(face, size, word)
        if m is None:
            return None
        size *= th / max(m.shape[0], 1)
    m = _model(face, size, word)
    if m is None:
        return None
    aspect = abs(math.log((tw / th) / (m.shape[1] / m.shape[0])))
    fitted = _resize(m, tw, th)
    a, b = fitted.ravel() - fitted.mean(), t_alpha.ravel() - t_alpha.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    corr = float((a * b).sum() / denom) if denom else 0.0
    return corr * math.exp(-3 * aspect), size, fitted, t_rgb


_match_cache: dict[tuple, tuple[Face | None, float]] = {}


def _word_key(w: Word) -> tuple:
    return (w.text, round(w.bbox[0], 1), round(w.bbox[1], 1), round(w.bbox[2], 1), round(w.bbox[3], 1))


def _evidence(region_words: list[Word], target: Word, page_words: list[Word] | None) -> list[Word]:
    """글꼴을 판단할 근거 단어: 영역 안 단어 + 같은 줄의 단어 (숫자만으로는 글꼴 구분이 어렵다)."""
    words = [w for w in region_words if w.patch_rgb is not None and w.text.strip()]
    if page_words:
        h = target.bbox[3] - target.bbox[1]
        keys = {_word_key(w) for w in words}
        for w in page_words:
            if (w.patch_rgb is not None and _word_key(w) not in keys and abs(w.baseline - target.baseline) < h * 0.5
                    and abs((w.bbox[3] - w.bbox[1]) - h) < h * 0.5):
                words.append(w)
    hangul = [w for w in words if any("\uac00" <= c <= "\ud7a3" for c in w.text)]
    others = [w for w in words if w not in hangul]
    # 한글이 글꼴 구분에 가장 유리하므로 먼저, 긴 단어 위주로
    ordered = sorted(hangul, key=lambda w: -len(w.text)) + sorted(others, key=lambda w: -len(w.text))
    if target not in ordered:
        ordered.insert(0, target)
    return ordered[:8]


def match_face(words: list[Word]) -> tuple[Face | None, float]:
    """여러 단어를 모두 비교해서 가장 닮은 글꼴을 고른다 (같은 칸·줄의 글자는 보통 같은 글꼴)."""
    if not words:
        return None, 0.0
    key = tuple(_word_key(w) for w in words)
    if key in _match_cache:
        return _match_cache[key]
    candidates = [f for f in system_faces() if _CANDIDATE_FAMILIES.match(f.family)
                  and f.style.lower() in ("regular", "normal", "bold")]
    best, best_score = None, -1.0
    for face in candidates:
        scores = []
        for w in words:
            fit = _fit(face, w)
            scores.append(fit[0] if fit else -0.5)        # 글자가 없는 글꼴은 감점
        s = float(np.mean(scores))
        if s > best_score:
            best, best_score = face, s
    _match_cache[key] = (best, best_score)
    return best, best_score


def _ls_color(fitted: np.ndarray, t_rgb: np.ndarray, bg) -> tuple[float, float, float]:
    """원본 픽셀 = 배경 + 덮임 × (글자색 - 배경) 을 최소제곱으로 풀어 진짜 글자색을 구한다."""
    bg = np.array(bg or (1, 1, 1), np.float32)
    a = fitted.reshape(-1, 1)
    diff = t_rgb.reshape(-1, 3) - bg
    denom = float((a * a).sum())
    if denom <= 0:
        return tuple(float(c) for c in bg)  # type: ignore[return-value]
    color = bg + (a * diff).sum(axis=0) / denom
    return tuple(float(c) for c in np.clip(color, 0, 1))  # type: ignore[return-value]


def _centroid(a: np.ndarray) -> tuple[float, float]:
    total = float(a.sum()) or 1.0
    ys, xs = np.indices(a.shape)
    return float((ys * a).sum() / total), float((xs * a).sum() / total)


def _place_on(target_shape, m: np.ndarray, t_alpha: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """모델 그림을 크기를 바꾸지 않고, 무게중심을 원본에 맞춰 원본 크기의 캔버스에 놓는다. (캔버스, 놓은 위치)"""
    canvas = np.zeros(target_shape, np.float32)
    ty, tx = _centroid(t_alpha)
    my, mx = _centroid(m)
    dy, dx = int(round(ty - my)), int(round(tx - mx))
    y0, x0 = max(dy, 0), max(dx, 0)
    y1, x1 = min(dy + m.shape[0], canvas.shape[0]), min(dx + m.shape[1], canvas.shape[1])
    if y1 > y0 and x1 > x0:
        canvas[y0:y1, x0:x1] = m[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
    return canvas, (dy, dx)


def _refine(face: Face, word: Word, size0: float
            ) -> tuple[float, np.ndarray, np.ndarray, tuple[float, float]] | None:
    """크기를 ±6% 범위에서 1%씩 바꿔 가며, 늘리거나 줄이지 않은 그림을 원본과 직접 비교해 가장 맞는 크기를 찾는다.

    (원본 그림을 OCR용으로 확대할 때 생기는 번짐 때문에 '잉크 높이'로 잰 크기는 3~4% 크게 나올 수 있다)
    """
    tgt = _target(word)
    if tgt is None:
        return None
    t_alpha, t_rgb, (cy0, cx0) = tgt
    pad = 4
    t_alpha = np.pad(t_alpha, pad)
    t_rgb = np.pad(t_rgb, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    b = t_alpha.ravel() - t_alpha.mean()
    best = None
    for k in range(-6, 7):
        size = size0 * (1 + k / 100)
        full = _model_full(face, size, word)
        if full is None:
            continue
        m, (my, mx) = full
        placed, (dy, dx) = _place_on(t_alpha.shape, m, t_alpha)
        a = placed.ravel() - placed.mean()
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        corr = float((a * b).sum() / denom) if denom else 0.0
        if best is None or corr > best[0]:
            # 원점 위치: 캔버스 → 원본 글자 그림(patch) → PDF 좌표
            py = dy + my - pad + cy0
            px = dx + mx - pad + cx0
            origin = (word.patch_xy[0] + px / word.px_scale, word.patch_xy[1] + py / word.px_scale)
            best = (corr, size, placed, origin)
    if best is None:
        return None
    _, size, placed, (gx, gy) = best
    # 0.25픽셀 단위로 조금씩 밀어 보며 가장 잘 겹치는 위치를 찾는다 (150dpi에서 1픽셀 = 0.5pt)
    from PIL import Image
    src = Image.fromarray(placed, "F")
    best_shift, best_corr = (0.0, 0.0), -2.0
    for sy in np.arange(-1.0, 1.01, 0.25):
        for sx in np.arange(-1.0, 1.01, 0.25):
            moved = np.asarray(src.transform(src.size, Image.AFFINE, (1, 0, -sx, 0, 1, -sy), Image.BILINEAR))
            a = moved.ravel() - moved.mean()
            denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
            corr = float((a * b).sum() / denom) if denom else 0.0
            if corr > best_corr:
                best_corr, best_shift, best_placed = corr, (float(sx), float(sy)), moved
    sx, sy = best_shift
    origin = (gx + sx / word.px_scale, gy + sy / word.px_scale)
    return size, best_placed, t_rgb, origin


def ocr_style(region_words: list[Word], target: Word, page_words: list[Word] | None = None) -> TextStyle:
    face, score = match_face(_evidence(region_words, target, page_words))
    source = "matched"
    if face is None or score < 0.3:
        face, source = _fallback_face(), "fallback"
    fit = _fit(face, target) if face else None
    refined = _refine(face, target, fit[1]) if fit else None
    origin = None
    if refined:
        size, placed, t_rgb, origin = refined
        color = _ls_color(placed, t_rgb, target.bg)
    elif fit:
        _, size, fitted, t_rgb = fit
        color = _ls_color(fitted, t_rgb, target.bg)
    else:
        size, color = target.size, target.color
    return TextStyle(face.label if face else "Helvetica", round(size, 2), color, source, face=face,
                     raster_dpi=target.src_dpi or target.px_scale * 72, origin=origin,
                     raster_grid=target.src_grid if target.src_dpi else None,
                     old_advance=advance(face, size, target.text) if face else 0.0)


def advance(face: Face, size_pt: float, text: str) -> float:
    """글자 폭(다음 글자가 시작하는 위치까지의 거리) pt. 표 칸의 정렬은 이 폭을 기준으로 한다."""
    return _pil_font(face, 100.0 * _SUPERSAMPLE).getlength(text) / _SUPERSAMPLE / 100.0 * size_pt


def apply_overrides(style: TextStyle, font_label: str, size: float, color_hex: str) -> TextStyle:
    """영역 설정에서 사용자가 직접 정한 값으로 덮어쓴다."""
    if font_label:
        face = face_by_label(font_label)
        if face:
            style.face, style.font_buffer, style.font_label, style.source = face, None, face.label, "user"
    if size:
        style.size = size
    if color_hex:
        h = color_hex.lstrip("#")
        style.color = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[assignment]
    return style


# ───────────────────────── 그리기 ─────────────────────────

def text_png(text: str, face: Face, size_pt: float, color, dpi: float, shift: tuple[float, float] = (0.0, 0.0)):
    """이미지 PDF용: 새 글자를 원본과 같은 해상도의 투명 PNG로 그린다.

    shift: 글자를 그림 안에서 소수점 이하 픽셀만큼 미는 양 (원본 이미지 픽셀 격자에 맞추기 위해)
    돌려주는 값: (PNG 바이트, 그림 크기 pt (w, h), 그림 안 원점(왼쪽 기준선) 위치 pt, 잉크 범위 pt (x0, x1))
    """
    from PIL import Image
    px = size_pt * dpi / 72
    alpha, (ox, oy) = _alpha(face, px, text, shift)
    rgb = np.array([round(c * 255) for c in color], np.uint8)
    img = np.zeros(alpha.shape + (4,), np.uint8)
    img[..., :3] = rgb
    img[..., 3] = np.round(alpha * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, format="PNG")
    k = 72 / dpi
    cols = np.flatnonzero((alpha > 0.5).any(axis=0))
    ink = ((cols[0] - ox) * k, (cols[-1] + 1 - ox) * k) if len(cols) else (0.0, 0.0)
    return buf.getvalue(), (alpha.shape[1] * k, alpha.shape[0] * k), (ox * k, oy * k), ink
