"""PDF 읽기 → 값 계산 → 수정 → 검증 파이프라인."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from decimal import Decimal

import pymupdf

from .formula import FormulaError, evaluate, expand_wildcards, is_valid_name
from .models import ERASE, ERASE_PICK, VALUE, Region, Template
from .numbers import format_number, parse_number

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\NanumGothic.ttf",
    r"C:\Windows\Fonts\gulim.ttc",
]
FONT_ALIAS = "pricefont"


# ───────────────────────── 읽기 ─────────────────────────

@dataclass
class Word:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    color: tuple[float, float, float]
    baseline: float
    line: int = 0          # 영역 안에서 몇 번째 줄인지 (위에서부터 0)


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
                                      cur["color"], cur["baseline"]))
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
                               "color": _rgb(span["color"]), "baseline": ch["origin"][1]}
                    cur["chars"].append(ch["c"])
                    cur["bbox"] |= bb
            flush()

    # PDF 내부 순서가 아니라 화면에 보이는 위치 순서로 정렬
    words.sort(key=lambda w: (w.bbox[1] + w.bbox[3]) / 2)
    line_no, line_y = -1, None
    for w in words:
        yc, h = (w.bbox[1] + w.bbox[3]) / 2, w.bbox[3] - w.bbox[1]
        if line_y is None or abs(yc - line_y) > max(h, 1) * 0.5:
            line_no += 1
            line_y = yc
        w.line = line_no
    words.sort(key=lambda w: (w.line, w.bbox[0]))
    return words


def read_region(page: pymupdf.Page, rect) -> TextInfo:
    words = read_words(page, rect)
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

    @property
    def base_text(self) -> str:
        """숫자 영역에서 실제로 바뀌는 부분의 원본 텍스트."""
        if self.target is not None:
            return self.original.words[self.target].text
        return self.original.text

    def kept_words(self) -> list[Word]:
        return [w for w, e in zip(self.original.words, self.erase_flags) if not e]


def check_template(doc: pymupdf.Document, tpl: Template) -> list[str]:
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
        info = read_region(doc[r.page], r.rect)
        if r.sample_text and not info.text:
            warnings.append(f"'{r.name}' 영역에 텍스트가 없습니다 (템플릿 작성 시: '{r.sample_text}')")
        elif r.kind == ERASE and r.erase_mode == ERASE_PICK:
            found = {w.text for w in info.words}
            missing = [t for t in r.keep_texts if t not in found]
            if missing:
                warnings.append(f"'{r.name}' 영역에서 남길 글자 {', '.join(repr(t) for t in missing)}를 "
                                "찾지 못했습니다 — 양식이 다를 수 있습니다")
    return warnings


def compute(doc: pymupdf.Document, tpl: Template, inputs: dict[str, str]) -> dict[str, RegionResult]:
    """각 영역의 새 값을 계산한다. inputs는 {영역 id: 사용자가 입력한 값}."""
    results: dict[str, RegionResult] = {}
    for r in tpl.regions:
        res = RegionResult(r.id)
        if r.page < len(doc):
            res.original = read_region(doc[r.page], r.rect)
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

def find_font(preferred: str = "") -> str | None:
    for path in [preferred, *FONT_CANDIDATES]:
        if path and os.path.exists(path):
            return path
    return None


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def redact_rects(r: Region, res: RegionResult) -> list[pymupdf.Rect]:
    """실제로 지울 사각형들. 단어 단위로 지울 때는 옆 단어를 건드리지 않게 살짝 안쪽으로 줄인다."""
    def word_rect(w: Word) -> pymupdf.Rect:
        return pymupdf.Rect(w.bbox) + (0.2, 0, -0.2, 0)

    if r.kind == ERASE:
        if r.erase_mode == ERASE_PICK:
            return [word_rect(w) for w, e in zip(res.original.words, res.erase_flags) if e]
        return [pymupdf.Rect(r.rect)]
    if res.target is not None:
        return [word_rect(res.original.words[res.target])]
    return [pymupdf.Rect(r.rect)]


def apply(doc: pymupdf.Document, tpl: Template, results: dict[str, RegionResult]) -> pymupdf.Document:
    """원본은 건드리지 않고, 수정된 새 문서를 만들어 돌려준다."""
    out = pymupdf.open("pdf", doc.tobytes())
    font_path = find_font(tpl.font_file)
    font = pymupdf.Font(fontfile=font_path) if font_path else pymupdf.Font("helv")

    for page_no in range(len(out)):
        regions = [r for r in tpl.regions
                   if r.page == page_no and results.get(r.id) and results[r.id].changed
                   and not results[r.id].error]
        if not regions:
            continue
        page = out[page_no]
        for r in regions:
            fill = hex_to_rgb(r.fill) if r.fill else None
            for rect in redact_rects(r, results[r.id]):
                page.add_redact_annot(rect, fill=fill, cross_out=False)
        # 표 테두리 같은 선과 이미지는 남기고 글자만 지운다 (덮을 색을 지정한 영역은 그 색으로 덮임)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        if font_path:
            page.insert_font(fontname=FONT_ALIAS, fontfile=font_path)
        for r in regions:
            if r.kind == VALUE:
                _draw_value(page, r, results[r.id], font, FONT_ALIAS if font_path else "helv")
    return out


def _draw_value(page: pymupdf.Page, r: Region, res: RegionResult, font: pymupdf.Font, fontname: str):
    rect = pymupdf.Rect(r.rect)
    pad = 1.0
    target = res.original.words[res.target] if res.target is not None else None
    src_size = target.size if target else res.original.size
    size = r.font_size or src_size or rect.height * 0.7

    # 쓸 수 있는 가로 범위: 영역 안이면서, 같은 줄에 남겨 둔 글자(라벨·단위)와 겹치지 않는 곳
    left, right = rect.x0 + pad, rect.x1 - pad
    if target:
        gap = size * 0.25
        for w in res.original.words:
            if w is target or w.line != target.line:
                continue
            if w.bbox[2] <= target.bbox[0]:
                left = max(left, w.bbox[2] + gap)
            elif w.bbox[0] >= target.bbox[2]:
                right = min(right, w.bbox[0] - gap)

    width = font.text_length(res.new_text, fontsize=size)
    if width > right - left:                  # 칸에 안 들어가면 글자를 줄인다
        size *= (right - left) / max(width, 0.01)
        width = font.text_length(res.new_text, fontsize=size)

    # 원본 글자가 있던 자리에 맞춰 정렬하면 옆 칸과 줄이 어긋나지 않는다
    if target:
        anchor = pymupdf.Rect(target.bbox)
    elif res.original.bbox:
        anchor = pymupdf.Rect(res.original.bbox)
    else:
        anchor = pymupdf.Rect(left, rect.y0, right, rect.y1)
    if r.align == "left":
        x = anchor.x0
    elif r.align == "center":
        x = (anchor.x0 + anchor.x1 - width) / 2
    else:
        x = anchor.x1 - width
    x = min(max(x, left), right - width)

    baseline = target.baseline if target else res.original.baseline
    if baseline is not None and not r.font_size:
        y = baseline
    else:
        asc, desc = font.ascender, font.descender
        y = rect.y0 + (rect.height - (asc - desc) * size) / 2 + asc * size
    src_color = target.color if target else res.original.color
    color = hex_to_rgb(r.color) if r.color else (src_color or (0, 0, 0))
    page.insert_text((x, y), res.new_text, fontsize=size, fontname=fontname, color=color)


# ───────────────────────── 검증 ─────────────────────────

@dataclass
class Check:
    region_id: str
    ok: bool
    message: str


def verify(out: pymupdf.Document, tpl: Template, results: dict[str, RegionResult]) -> list[Check]:
    """결과 PDF를 다시 읽어 각 영역이 의도대로 바뀌었는지 확인한다."""
    checks = []
    for r in tpl.regions:
        res = results.get(r.id)
        if res is None or res.error or r.page >= len(out):
            checks.append(Check(r.id, False, res.error if res else "결과 없음"))
            continue
        actual = read_region(out[r.page], r.rect).text
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
