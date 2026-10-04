"""PDF 읽기 → 값 계산 → 수정 → 검증 파이프라인."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from decimal import Decimal

import pymupdf

from . import fonts
from .fonts import TextStyle
from .formula import FormulaError, evaluate, expand_wildcards, is_valid_name
from .models import ERASE, ERASE_PICK, VALUE, Region, Template
from .numbers import format_number, parse_number
from .words import Word, order_words, words_in_rect

OcrMap = dict[int, list[Word]]   # 쪽 번호 → OCR로 읽은 단어 (OCR한 쪽만 들어 있음)

# ───────────────────────── 읽기 ─────────────────────────

@dataclass
class TextInfo:
    text: str = ""
    size: float = 0.0
    color: tuple[float, float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None   # 원본 글자들이 차지한 범위
    baseline: float | None = None                            # 한 줄일 때 원본 글자의 기준선 y
    words: list[Word] = field(default_factory=list)

    def lines(self) -> list[str]:
        out: dict[int, list[str]] = {}
        for w in self.words:
            out.setdefault(w.line, []).append(w.text)
        return [" ".join(ws) for _, ws in sorted(out.items())]


def _rgb(c: int) -> tuple[float, float, float]:
    return (((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)


def read_words(page: pymupdf.Page, rect) -> list[Word]:
    """영역 안에 중심이 들어오는 글자를 단어 단위로 모아, 위→아래·왼→오른쪽 순서로 돌려준다."""
    r = pymupdf.Rect(rect)
    raw = page.get_text("rawdict", clip=r + (-2, -2, 2, 2))
    words: list[Word] = []

    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            cur: dict | None = None

            def flush():
                nonlocal cur
                if cur and cur["chars"]:
                    words.append(Word("".join(cur["chars"]), tuple(cur["bbox"]), cur["size"],
                                      cur["color"], cur["baseline"], font=cur["font"],
                                      bold=bool(cur["flags"] & 16), italic=bool(cur["flags"] & 2)))
                cur = None

            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    bb = pymupdf.Rect(ch["bbox"])
                    inside = r.contains(pymupdf.Point((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2))
                    if not inside or ch["c"].isspace():
                        flush()
                        continue
                    # 공백 문자 없이 간격만 벌려 놓은 PDF도 있어서, 글자 사이가 넓으면 단어를 나눈다
                    if cur and bb.x0 - cur["bbox"].x1 > span["size"] * 0.3:
                        flush()
                    if cur is None:
                        cur = {"chars": [], "bbox": pymupdf.Rect(bb), "size": span["size"],
                               "color": _rgb(span["color"]), "baseline": ch["origin"][1],
                               "font": span["font"], "flags": span["flags"]}
                    cur["chars"].append(ch["c"])
                    cur["bbox"] |= bb
            flush()

    # PDF 내부 순서가 아니라 화면에 보이는 위치 순서로 정렬
    return order_words(words)


def read_region(page: pymupdf.Page, rect, ocr_words: list[Word] | None = None) -> TextInfo:
    """영역 안의 글자를 읽는다. ocr_words가 있으면(이미지 PDF) 텍스트 레이어 대신 OCR 결과를 쓴다."""
    words = words_in_rect(ocr_words, rect) if ocr_words is not None else read_words(page, rect)
    if not words:
        return TextInfo()
    box = pymupdf.Rect(words[0].bbox)
    for w in words[1:]:
        box |= pymupdf.Rect(w.bbox)
    biggest = max(words, key=lambda w: w.size)
    single_line = len({w.line for w in words}) == 1
    info = TextInfo(size=biggest.size, color=biggest.color, bbox=tuple(box),
                    baseline=words[0].baseline if single_line else None, words=words)
    info.text = " ".join(info.lines())
    return info


def page_sizes(doc: pymupdf.Document) -> list[list[float]]:
    return [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]


def number_target(region: Region, words: list[Word]) -> int | None:
    """숫자 영역에서 교체할 단어(숫자가 들어 있는 첫 단어)의 위치."""
    if region.kind != VALUE or not region.number_only:
        return None
    return next((i for i, w in enumerate(words) if parse_number(w.text) is not None), None)


# ───────────────────────── 계산 ─────────────────────────

@dataclass
class RegionResult:
    region_id: str
    original: TextInfo = field(default_factory=TextInfo)
    value: Decimal | None = None
    new_text: str = ""
    changed: bool = False
    error: str = ""
    target: int | None = None                           # 숫자 영역: 교체할 단어 번호
    erase_flags: list[bool] = field(default_factory=list)  # 지우기 영역: 단어별 지움 여부
    style: TextStyle | None = None                      # 숫자 영역: 새 글자를 쓸 글꼴·크기·색

    @property
    def base_text(self) -> str:
        """숫자 영역에서 실제로 바뀌는 부분의 원본 텍스트."""
        if self.target is not None:
            return self.original.words[self.target].text
        return self.original.text

    def kept_words(self) -> list[Word]:
        return [w for w, e in zip(self.original.words, self.erase_flags) if not e]


def check_template(doc: pymupdf.Document, tpl: Template, ocr: OcrMap | None = None) -> list[str]:
    """템플릿을 이 PDF에 적용해도 되는지 점검하고 경고 목록을 돌려준다."""
    warnings = []
    sizes = page_sizes(doc)
    if tpl.page_sizes and len(tpl.page_sizes) != len(sizes):
        warnings.append(f"페이지 수가 다릅니다 (템플릿 {len(tpl.page_sizes)}쪽, PDF {len(sizes)}쪽)")
    for i, (a, b) in enumerate(zip(tpl.page_sizes, sizes)):
        if abs(a[0] - b[0]) > 1 or abs(a[1] - b[1]) > 1:
            warnings.append(f"{i + 1}쪽 크기가 템플릿과 다릅니다 — 영역 위치가 어긋날 수 있습니다")
    names = [r.name for r in tpl.regions if r.kind == VALUE]
    for n in sorted({n for n in names if names.count(n) > 1}):
        warnings.append(f"영역 이름 '{n}'이 중복되었습니다")
    for r in tpl.regions:
        if r.page >= len(doc):
            warnings.append(f"'{r.name}' 영역이 PDF에 없는 {r.page + 1}쪽에 있습니다")
            continue
        info = read_region(doc[r.page], r.rect, (ocr or {}).get(r.page))
        if r.sample_text and not info.text:
            warnings.append(f"'{r.name}' 영역에 텍스트가 없습니다 (템플릿 작성 시: '{r.sample_text}')")
        elif r.kind == ERASE and r.erase_mode == ERASE_PICK:
            found = {w.text for w in info.words}
            missing = [t for t in r.keep_texts if t not in found]
            if missing:
                warnings.append(f"'{r.name}' 영역에서 남길 글자 {', '.join(repr(t) for t in missing)}를 "
                                "찾지 못했습니다 — 양식이 다를 수 있습니다")
    return warnings


def compute(doc: pymupdf.Document, tpl: Template, inputs: dict[str, str],
            ocr: OcrMap | None = None) -> dict[str, RegionResult]:
    """각 영역의 새 값을 계산한다. inputs는 {영역 id: 사용자가 입력한 값}."""
    results: dict[str, RegionResult] = {}
    for r in tpl.regions:
        res = RegionResult(r.id)
        if r.page < len(doc):
            res.original = read_region(doc[r.page], r.rect, (ocr or {}).get(r.page))
            res.target = number_target(r, res.original.words)
            if r.kind == ERASE:
                res.erase_flags = [r.should_erase(w.text) for w in res.original.words]
        else:
            res.error = "PDF에 해당 페이지가 없습니다"
        results[r.id] = res

    value_regions = [r for r in tpl.regions if r.kind == VALUE]
    by_name: dict[str, Region] = {}
    for r in value_regions:
        by_name.setdefault(r.name, r)
    names = list(by_name)
    cache: dict[str, Decimal] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> Decimal:
        if name in cache:
            return cache[name]
        region = by_name.get(name)
        if region is None:
            raise FormulaError(f"'{name}' 영역이 없습니다")
        if name in visiting:
            raise FormulaError(f"'{name}' 수식이 자기 자신을 참조합니다")
        visiting.add(name)
        try:
            raw_input = inputs.get(region.id, "").strip()
            if region.formula.strip():
                expr = expand_wildcards(region.formula, [n for n in names if n != name])
                value = evaluate(expr, resolve)
            elif raw_input:
                value = parse_number(raw_input)
                if value is None:
                    raise FormulaError(f"'{name}' 입력값 '{raw_input}'이 숫자가 아닙니다")
            else:
                value = parse_number(results[region.id].base_text)
                if value is None:
                    raise FormulaError(f"'{name}' 원본 값을 읽을 수 없습니다 — 새 값을 입력하세요")
        finally:
            visiting.discard(name)
        cache[name] = value
        return value

    for r in tpl.regions:
        res = results[r.id]
        if res.error:
            continue
        if r.kind == ERASE:
            res.changed = any(res.erase_flags) if r.erase_mode == ERASE_PICK else True
            continue
        if not is_valid_name(r.name):
            res.error = "이름은 공백 없이 한글/영문/숫자/_로 지어야 하며 숫자로 시작할 수 없습니다"
            continue
        if by_name[r.name] is not r:
            res.error = f"이름 '{r.name}'이 중복되었습니다"
            continue
        try:
            res.value = resolve(r.name)
        except FormulaError as e:
            res.error = str(e)
            continue
        user_set = bool(r.formula.strip() or inputs.get(r.id, "").strip())
        res.new_text = format_number(res.value, r.decimals, r.thousands, r.prefix, r.suffix)
        res.changed = user_set and _norm(res.new_text) != _norm(res.base_text)
        if res.changed:
            res.style = region_style(doc, r, res, ocr)
    return results


def _norm(s: str) -> str:
    return "".join(s.split())


def expected_text(r: Region, res: RegionResult) -> str:
    """수정이 끝난 뒤 영역 안에 보여야 할 텍스트."""
    if not res.changed:
        return res.original.text
    if r.kind == ERASE:
        return " ".join(w.text for w in res.kept_words()) if r.erase_mode == ERASE_PICK else ""
    if res.target is None:
        return res.new_text
    words = [w.text for w in res.original.words]
    words[res.target] = res.new_text
    return " ".join(words)


# ───────────────────────── 쓰기 ─────────────────────────

def region_style(doc: pymupdf.Document, r: Region, res: RegionResult, ocr: OcrMap | None = None) -> TextStyle:
    """숫자 영역의 새 글자를 원본과 같게 쓰기 위한 글꼴·크기·색. 사용자가 정한 값이 있으면 그걸 쓴다."""
    words = res.original.words
    ref = words[res.target] if res.target is not None else (words[0] if words else None)
    if ref is None:          # 빈 칸에 새로 쓰는 경우
        face = fonts.find_face(fonts.FALLBACK_FONT)
        rect = pymupdf.Rect(r.rect)
        style = TextStyle(face.label if face else "Helvetica", round(rect.height * 0.6, 1), (0, 0, 0),
                          "fallback", face=face)
    elif ref.ocr:
        style = fonts.ocr_style(words, ref, (ocr or {}).get(r.page))
    else:
        style = fonts.text_layer_style(doc, doc[r.page], ref, res.new_text or ref.text)
    return fonts.apply_overrides(style, r.font, r.font_size, r.color)


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def redact_rects(r: Region, res: RegionResult, image_page: bool = False
                 ) -> list[tuple[pymupdf.Rect, tuple[float, float, float] | None]]:
    """실제로 지울 (사각형, 채울 색) 목록.

    - 텍스트 레이어 단어: 옆 단어를 건드리지 않게 살짝 안쪽으로 줄이고, 색을 따로 정하지 않았으면 채우지 않는다.
    - OCR 단어(이미지): 글자 가장자리 번짐까지 지우도록 살짝 키우고, 원래 배경색으로 채운다.
    """
    user_fill = hex_to_rgb(r.fill) if r.fill else None

    def word_item(w: Word):
        if w.ocr:
            return pymupdf.Rect(w.bbox) + (-0.6, -0.6, 0.6, 0.6), user_fill or w.bg or (1, 1, 1)
        return pymupdf.Rect(w.bbox) + (0.2, 0, -0.2, 0), user_fill

    # 이미지 위에서는 채우지 않으면 원래 글자가 그대로 보이므로 흰색으로 덮는다
    whole = (pymupdf.Rect(r.rect), user_fill or ((1, 1, 1) if image_page else None))
    if r.kind == ERASE:
        if r.erase_mode == ERASE_PICK:
            return [word_item(w) for w, e in zip(res.original.words, res.erase_flags) if e]
        return [whole]
    if res.target is not None:
        return [word_item(res.original.words[res.target])]
    return [whole]


def apply(doc: pymupdf.Document, tpl: Template, results: dict[str, RegionResult],
          ocr: OcrMap | None = None) -> pymupdf.Document:
    """원본은 건드리지 않고, 수정된 새 문서를 만들어 돌려준다."""
    out = pymupdf.open("pdf", doc.tobytes())
    for page_no in range(len(out)):
        regions = [r for r in tpl.regions
                   if r.page == page_no and results.get(r.id) and results[r.id].changed
                   and not results[r.id].error]
        if not regions:
            continue
        page = out[page_no]
        image_page = page_no in (ocr or {})
        for r in regions:
            for rect, fill in redact_rects(r, results[r.id], image_page):
                page.add_redact_annot(rect, fill=fill, cross_out=False)
        # 표 테두리 같은 선은 남긴다. 이미지 PDF는 지울 자리의 이미지 픽셀까지 실제로 지운다
        # (덮기만 하면 PDF 안에 원래 글자 이미지가 그대로 남아 개인정보가 새어 나갈 수 있음)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS if image_page else pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for r in regions:
            if r.kind == VALUE:
                res = results[r.id]
                style = res.style or region_style(doc, r, res, ocr)
                _draw_value(page, r, res, style)
    try:
        out.subset_fonts()       # 넣은 글꼴에서 실제 쓴 글자만 남겨 파일 크기를 줄인다
    except Exception:  # noqa: BLE001
        pass
    return out


def _bounds(r: Region, res: RegionResult, size: float) -> tuple[float, float]:
    """새 글자를 쓸 수 있는 가로 범위: 영역 안이면서, 같은 줄에 남겨 둔 글자(라벨·단위)와 겹치지 않는 곳."""
    rect = pymupdf.Rect(r.rect)
    left, right = rect.x0 + 1, rect.x1 - 1
    if res.target is not None:
        target = res.original.words[res.target]
        gap = size * 0.25
        for w in res.original.words:
            if w is target or w.line != target.line:
                continue
            if w.bbox[2] <= target.bbox[0]:
                left = max(left, w.bbox[2] + gap)
            elif w.bbox[0] >= target.bbox[2]:
                right = min(right, w.bbox[0] - gap)
    return left, right


def _anchor(r: Region, res: RegionResult, left: float, right: float) -> pymupdf.Rect:
    if res.target is not None:
        return pymupdf.Rect(res.original.words[res.target].bbox)
    if res.original.bbox:
        return pymupdf.Rect(res.original.bbox)
    rect = pymupdf.Rect(r.rect)
    return pymupdf.Rect(left, rect.y0, right, rect.y1)


def _place(align: str, anchor: pymupdf.Rect, x0: float, x1: float, left: float, right: float) -> float:
    """글자 범위 [x0, x1] (원점 기준 상대 위치)를 원래 글자 자리에 맞추는 원점 x."""
    if align == "left":
        x = anchor.x0 - x0
    elif align == "center":
        x = (anchor.x0 + anchor.x1) / 2 - (x0 + x1) / 2
    else:
        x = anchor.x1 - x1
    return min(max(x, left - x0), right - x1)


def _draw_value(page: pymupdf.Page, r: Region, res: RegionResult, style: TextStyle) -> None:
    rect = pymupdf.Rect(r.rect)
    target = res.original.words[res.target] if res.target is not None else None
    size = style.size or rect.height * 0.7
    left, right = _bounds(r, res, size)
    anchor = _anchor(r, res, left, right)
    baseline = target.baseline if target else res.original.baseline
    text = res.new_text

    if style.raster_dpi and style.face is not None:
        # 이미지 PDF: 원본과 같은 해상도의 그림으로 넣어 글자 번짐 정도까지 맞춘다
        png, (w, h), (ox, oy), (ink0, ink1) = fonts.text_png(text, style.face, size, style.color, style.raster_dpi)
        if ink1 - ink0 > right - left:
            size *= (right - left) / max(ink1 - ink0, 0.01)
            png, (w, h), (ox, oy), (ink0, ink1) = fonts.text_png(text, style.face, size, style.color,
                                                                 style.raster_dpi)
        if style.origin is not None and style.old_advance:
            # 원래 글자의 시작점·글자 폭을 기준으로 정렬 (표 칸 정렬 방식과 같음)
            new_adv = fonts.advance(style.face, size, text)
            ox0, y = style.origin
            if r.align == "left":
                x = ox0
            elif r.align == "center":
                x = ox0 + (style.old_advance - new_adv) / 2
            else:
                x = ox0 + style.old_advance - new_adv
            x = min(max(x, left - ink0), right - ink1)
        else:
            x = _place(r.align, anchor, ink0, ink1, left, right)
            y = baseline if baseline is not None else rect.y0 + (rect.height + size * 0.7) / 2
        x0, y0 = x - ox, y - oy
        if style.raster_grid is not None:
            # 넣는 그림의 픽셀을 원본 이미지의 픽셀 격자에 맞추고, 남는 소수점은 그림 안에서 글자를 밀어 보정
            step = 72 / style.raster_dpi
            gx, gy = style.raster_grid
            sx0 = gx + math.floor((x0 - gx) / step) * step
            sy0 = gy + math.floor((y0 - gy) / step) * step
            shift = ((x0 - sx0) / step, (y0 - sy0) / step)
            png, (w, h), (ox, oy), _ = fonts.text_png(text, style.face, size, style.color, style.raster_dpi, shift)
            x0, y0 = sx0, sy0
        page.insert_image(pymupdf.Rect(x0, y0, x0 + w, y0 + h), stream=png)
        return

    # 텍스트 PDF: 원본 글꼴(또는 같은 이름의 설치 글꼴)로 글자를 쓴다
    buf = style.font_buffer or (fonts.face_bytes(style.face) if style.face else None)
    if buf:
        font = pymupdf.Font(fontbuffer=buf)
        fontname = "pf" + format(abs(hash((style.font_label, len(buf)))) % 10 ** 8, "d")
        page.insert_font(fontname=fontname, fontbuffer=buf)
    else:
        font, fontname = pymupdf.Font("helv"), "helv"
    width = font.text_length(text, fontsize=size)
    if width > right - left:                  # 칸에 안 들어가면 글자를 줄인다
        size *= (right - left) / max(width, 0.01)
        width = font.text_length(text, fontsize=size)
    x = _place(r.align, anchor, 0, width, left, right)
    if baseline is not None:
        y = baseline
    else:
        asc, desc = font.ascender, font.descender
        y = rect.y0 + (rect.height - (asc - desc) * size) / 2 + asc * size
    page.insert_text((x, y), text, fontsize=size, fontname=fontname, color=style.color)


# ───────────────────────── 검증 ─────────────────────────

@dataclass
class Check:
    region_id: str
    ok: bool
    message: str


def verify(out: pymupdf.Document, tpl: Template, results: dict[str, RegionResult],
           ocr_out: OcrMap | None = None) -> list[Check]:
    """결과 PDF를 다시 읽어 각 영역이 의도대로 바뀌었는지 확인한다.

    이미지 PDF는 결과 PDF를 다시 OCR한 단어(ocr_out)로 확인한다.
    """
    checks = []
    for r in tpl.regions:
        res = results.get(r.id)
        if res is None or res.error or r.page >= len(out):
            checks.append(Check(r.id, False, res.error if res else "결과 없음"))
            continue
        ocr_words = (ocr_out or {}).get(r.page)
        actual = read_region(out[r.page], r.rect, ocr_words).text
        expected = expected_text(r, res)
        ok = _norm(actual) == _norm(expected)
        if ok:
            if not res.changed:
                msg = "변경 없음(원본 유지)"
            elif r.kind == ERASE:
                kept = expected
                msg = f"삭제됨 (남김: {kept})" if kept else "삭제됨"
            else:
                msg = "변경됨"
        else:
            msg = f"예상 '{expected}', 실제 '{actual}'"
            if ocr_words is not None:
                msg = "OCR로 다시 읽은 결과가 다름 — 미리보기로 직접 확인하세요. " + msg
        checks.append(Check(r.id, ok, msg))
    return checks


def write_log(path: str, tpl: Template, results: dict[str, RegionResult], checks: list[Check]) -> None:
    by_id = {c.region_id: c for c in checks}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["페이지", "이름", "종류", "원본", "변경 후", "지운 글자", "검증", "메시지"])
        for r in tpl.regions:
            res, chk = results.get(r.id), by_id.get(r.id)
            erased = ""
            if res and r.kind == ERASE:
                erased = (" ".join(wd.text for wd, e in zip(res.original.words, res.erase_flags) if e)
                          if r.erase_mode == ERASE_PICK else "(영역 전체)")
            w.writerow([
                r.page + 1, r.name, "삭제" if r.kind == ERASE else "값",
                res.original.text if res else "",
                expected_text(r, res) if res and not res.error else "",
                erased,
                "OK" if chk and chk.ok else "확인 필요",
                chk.message if chk else "",
            ])
