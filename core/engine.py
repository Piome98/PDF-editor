"""PDF 읽기 → 값 계산 → 수정 → 검증 파이프라인."""
from __future__ import annotations

import csv
import difflib
import math
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

import pymupdf

from . import fonts
from .bulk import BulkItem, BulkList, norm_key
from .fonts import TextStyle
from .formula import FormulaError, evaluate, expand_wildcards, is_valid_name
from .models import ERASE, ERASE_PICK, LIST, TEXT, VALUE, Region, Template
from .numbers import detect_style, format_number, parse_number
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
    hidden = _invisible_origins(page)

    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            cur: dict | None = None

            def flush():
                nonlocal cur
                if cur and cur["chars"]:
                    words.append(Word("".join(cur["chars"]), tuple(cur["bbox"]), cur["size"],
                                      cur["color"], cur["baseline"], font=cur["font"],
                                      bold=bool(cur["flags"] & 16), italic=bool(cur["flags"] & 2),
                                      invisible=cur["hidden"] * 2 > len(cur["chars"])))
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
                               "font": span["font"], "flags": span["flags"], "hidden": 0}
                    cur["chars"].append(ch["c"])
                    if (round(ch["origin"][0], 1), round(ch["origin"][1], 1)) in hidden:
                        cur["hidden"] += 1
                    cur["bbox"] |= bb
            flush()

    # PDF 내부 순서가 아니라 화면에 보이는 위치 순서로 정렬
    return order_words(words)


def _invisible_origins(page: pymupdf.Page) -> set[tuple[float, float]]:
    """화면에 그려지지 않는 글자(렌더 모드 3, 투명)의 위치."""
    out = set()
    try:
        for span in page.get_texttrace():
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                for ch in span["chars"]:
                    out.add((round(ch[2][0], 1), round(ch[2][1], 1)))
    except Exception:  # noqa: BLE001
        pass
    return out


_BAD_CHAR = re.compile(r"[\ufffd\ue000-\uf8ff\x00-\x08\x0b-\x1f]")


def _overlap(a: Word, b: Word) -> float:
    """두 단어가 겹친 면적 ÷ 작은 쪽 면적."""
    ra, rb = pymupdf.Rect(a.bbox), pymupdf.Rect(b.bbox)
    inter = ra & rb
    small = min(abs(ra), abs(rb))
    return abs(inter) / small if small > 0 and not inter.is_empty else 0.0


def merge_words(text_words: list[Word], ocr_words: list[Word]) -> list[Word]:
    """텍스트 레이어와 OCR을 합친다.

    - 제대로 읽히는 텍스트 레이어 단어는 그대로 쓴다 (원본 글꼴·정확한 위치·진짜 글자 삭제).
    - 보이지 않는 글자, 깨진 글자, OCR이 확실히 다르게 읽은 글자는 버리고 그 자리는 OCR 단어를 쓴다.
    - 텍스트 레이어가 없는 곳(이미지)은 OCR 단어를 쓴다.
    """
    kept = []
    for t in text_words:
        if t.invisible or _BAD_CHAR.search(t.text):
            continue
        over = [o for o in ocr_words if _overlap(t, o) > 0.5]
        if over:
            o = max(over, key=lambda o: _overlap(t, o))
            similar = difflib.SequenceMatcher(None, _norm(t.text), _norm(o.text)).ratio()
            if o.score >= 0.9 and similar < 0.5 and len(_norm(o.text)) >= 2:
                continue       # 글자 대응표가 잘못된 PDF: 보이는 모양(OCR)을 믿는다
        kept.append(t)
    extra = [o for o in ocr_words if not any(_overlap(o, t) > 0.3 for t in kept)]
    return order_words(kept + extra)


def read_region(page: pymupdf.Page, rect, ocr_words: list[Word] | None = None) -> TextInfo:
    """영역 안의 글자를 읽는다. 그 쪽을 OCR했으면(ocr_words) 텍스트 레이어와 OCR을 합쳐 쓴다."""
    words = read_words(page, rect)
    if ocr_words is not None:
        words = merge_words(words, words_in_rect(ocr_words, rect))
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
    if region.kind != VALUE or region.target == TEXT or not region.number_only:
        return None
    return next((i for i, w in enumerate(words) if parse_number(w.text) is not None), None)


# ───────────────────────── 계산 ─────────────────────────

@dataclass
class Edit:
    """영역 안의 단어 하나를 새 글자로 바꾸기."""
    word: int | None          # 바꿀 단어 번호 (None = 영역 전체에 새로 씀)
    text: str
    style: TextStyle | None = None


@dataclass
class RowCheck:
    """품번 대조 영역의 줄 하나에 대한 판정."""
    line: int
    text: str                       # 줄 전체 글자
    key: str = ""                   # PDF에서 찾은 품번 단어
    status: str = "other"           # matched(엑셀에 있음) | similar(비슷함) | unmatched(엑셀에 없음) | other(품번 없는 줄)
    item: BulkItem | None = None
    note: str = ""
    pdf_price: str = ""
    price_status: str = ""          # ok | diff | unknown(가격 위치 모름) | none(엑셀에 가격 없음)
    new_price: str = ""
    action: str = ""                # 화면 표시용 조치


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
    edits: list[Edit] = field(default_factory=list)       # 품번 대조 영역: 가격 바꾸기
    rows: list[RowCheck] = field(default_factory=list)     # 품번 대조 영역: 줄별 판정

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
            ocr: OcrMap | None = None, bulk: BulkList | None = None) -> dict[str, RegionResult]:
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

    value_regions = [r for r in tpl.regions if r.kind == VALUE and r.target != TEXT]
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
        if r.kind == VALUE and r.target == TEXT:
            _compute_text(doc, r, res, inputs.get(r.id, ""), ocr)
            continue
        if r.kind == LIST:
            if bulk is None or not bulk.items:
                res.error = "엑셀 품번 목록을 먼저 불러오세요"
            else:
                _compute_list(doc, r, res, bulk, ocr)
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


_DATE = re.compile(r"^\d{2,4}[./-]\d{1,2}[./-]\d{1,2}\.?$")


def _is_amount(text: str) -> bool:
    """가격 후보 단어: 숫자이면서 날짜·백분율이 아닌 것."""
    return (any(c.isdigit() for c in text) and parse_number(text) is not None
            and not _DATE.match(text) and not text.endswith("%"))


def _cx(w: Word) -> float:
    return (w.bbox[0] + w.bbox[2]) / 2


def _compute_text(doc: pymupdf.Document, r: Region, res: RegionResult, new: str, ocr: OcrMap | None) -> None:
    """텍스트 바꾸기: 영역 안 글자를 모두 지우고, 첫 단어 자리에 같은 서식으로 새 글자를 쓴다."""
    new = new.strip()
    if not new or _norm(new) == _norm(res.original.text):
        return                                   # 입력이 없으면 원본 그대로
    words = res.original.words
    res.new_text = new
    res.target = 0 if words else None
    res.erase_flags = [True] * len(words)
    res.changed = True
    res.style = region_style(doc, r, res, ocr)


def _compute_list(doc: pymupdf.Document, r: Region, res: RegionResult, bulk: BulkList,
                  ocr: OcrMap | None) -> None:
    words = res.original.words
    lines: dict[int, list[int]] = {}
    for i, w in enumerate(words):
        lines.setdefault(w.line, []).append(i)
    matches = {i: bulk.match(w.text) for i, w in enumerate(words)}

    def key_of(idxs):
        cands = [(i, matches[i]) for i in idxs if matches[i]]
        exact = [c for c in cands if c[1].kind == "exact"]
        return exact[0] if exact else (cands[0] if cands else None)

    # 엑셀과 정확히 맞은 줄들로 '품번 열'과 '가격 열'의 위치를 배운다
    key_xs, price_x1s = [], []
    for idxs in lines.values():
        pick = key_of(idxs)
        if not pick or pick[1].kind != "exact":
            continue
        key_xs.append(_cx(words[pick[0]]))
        price = pick[1].item.price
        if price is not None:
            price_x1s += [words[j].bbox[2] for j in idxs
                          if j != pick[0] and _is_amount(words[j].text) and parse_number(words[j].text) == price]
    key_x = statistics.median(key_xs) if key_xs else None
    price_x = statistics.median(price_x1s) if price_x1s else None
    tol = max(15.0, (r.rect[2] - r.rect[0]) * 0.06)

    erase = [False] * len(words)
    edits: list[Edit] = []
    rows: list[RowCheck] = []
    for line, idxs in sorted(lines.items()):
        row = RowCheck(line, " ".join(words[i].text for i in idxs))
        rows.append(row)
        pick = key_of(idxs)
        if pick is None:
            keyish = [i for i in idxs if bulk.looks_like_key(words[i].text)
                      or (key_x is not None and abs(_cx(words[i]) - key_x) <= tol and len(norm_key(words[i].text)) >= 4
                          and not _DATE.match(words[i].text))]
            if keyish:
                row.key, row.status = words[keyish[0]].text, "unmatched"
                if r.list_unmatched == "erase":
                    for i in idxs:
                        erase[i] = True
                    row.action = "엑셀에 없음 → 줄 삭제"
                else:
                    row.action = "엑셀에 없음 (표시만)"
            else:
                row.action = "품번 없는 줄 → 유지"
            continue

        ki, m = pick
        row.key, row.item, row.note = words[ki].text, m.item, m.note
        row.status = "matched" if m.kind == "exact" else "similar"
        if m.item.price is None:
            row.price_status = "none"
        else:
            amounts = [j for j in idxs if j != ki and _is_amount(words[j].text)]
            equal = [j for j in amounts if parse_number(words[j].text) == m.item.price]
            if equal:
                row.pdf_price, row.price_status = words[equal[0]].text, "ok"
            else:
                pj = None
                if price_x is not None and amounts:
                    pj = min(amounts, key=lambda j: abs(words[j].bbox[2] - price_x))
                    if abs(words[pj].bbox[2] - price_x) > tol:
                        pj = None
                elif len(amounts) == 1:
                    pj = amounts[0]
                if pj is None:
                    row.price_status = "unknown"
                else:
                    row.pdf_price, row.price_status = words[pj].text, "diff"
                    st = detect_style(words[pj].text)
                    exp = m.item.price.as_tuple().exponent
                    decimals = max(st.decimals, -exp if isinstance(exp, int) and exp < 0 else 0)
                    row.new_price = format_number(m.item.price, decimals, st.thousands, st.prefix, st.suffix)
                    if r.list_price == "replace" and row.status == "matched":
                        edits.append(Edit(pj, row.new_price))
        if row.status == "similar":
            row.action = "비슷한 품번 → 유지, 확인 필요"
        elif row.price_status == "diff":
            row.action = (f"가격 {row.pdf_price} → {row.new_price}" if r.list_price == "replace"
                          else f"가격 다름: PDF {row.pdf_price} / 엑셀 {row.new_price}")
        elif row.price_status == "unknown":
            row.action = "엑셀에 있음 → 유지 (가격 위치를 못 찾음, 확인 필요)"
        elif row.price_status == "ok":
            row.action = "엑셀에 있음, 가격 일치 → 유지"
        else:
            row.action = "엑셀에 있음 → 유지"

    res.erase_flags, res.edits, res.rows = erase, edits, rows
    res.changed = any(erase) or bool(edits)
    for e in edits:
        e.style = word_style(doc, r, words, e.word, e.text, ocr)


def list_summary(res: RegionResult) -> dict[str, int]:
    rows = res.rows
    return {
        "유지": sum(1 for x in rows if x.status in ("matched", "similar")),
        "삭제": sum(1 for x in rows if x.status == "unmatched" and "삭제" in x.action),
        "가격수정": len(res.edits),
        "확인": sum(1 for x in rows if x.status == "similar" or x.price_status in ("unknown",)
                   or (x.price_status == "diff" and not any(e.text == x.new_price for e in res.edits))),
    }


def edits_of(r: Region, res: RegionResult) -> list[Edit]:
    if r.kind == VALUE:
        return [Edit(res.target, res.new_text, res.style)]
    if r.kind == LIST:
        return res.edits
    return []


def expected_text(r: Region, res: RegionResult) -> str:
    """수정이 끝난 뒤 영역 안에 보여야 할 텍스트."""
    if not res.changed:
        return res.original.text
    if r.kind == ERASE and r.erase_mode != ERASE_PICK:
        return ""
    if r.kind == VALUE and res.target is None:
        return res.new_text
    new = {e.word: e.text for e in edits_of(r, res)}
    erase = res.erase_flags or [False] * len(res.original.words)
    return " ".join(new[i] if i in new else w.text for i, w in enumerate(res.original.words)
                    if i in new or not erase[i])


# ───────────────────────── 쓰기 ─────────────────────────

def word_style(doc: pymupdf.Document, r: Region, words: list[Word], idx: int | None, new_text: str,
               ocr: OcrMap | None = None) -> TextStyle:
    """새 글자를 원본과 같게 쓰기 위한 글꼴·크기·색. 사용자가 영역에 정한 값이 있으면 그걸 쓴다."""
    ref = words[idx] if idx is not None else (words[0] if words else None)
    if ref is None:          # 빈 칸에 새로 쓰는 경우
        face = fonts.find_face(fonts.FALLBACK_FONT)
        rect = pymupdf.Rect(r.rect)
        style = TextStyle(face.label if face else "Helvetica", round(rect.height * 0.6, 1), (0, 0, 0),
                          "fallback", face=face)
    elif ref.ocr:
        same_line = [w for w in words if w.line == ref.line]
        style = fonts.ocr_style(same_line, ref, (ocr or {}).get(r.page))
    else:
        style = fonts.text_layer_style(doc, doc[r.page], ref, new_text or ref.text)
    return fonts.apply_overrides(style, r.font, r.font_size, r.color)


def region_style(doc: pymupdf.Document, r: Region, res: RegionResult, ocr: OcrMap | None = None) -> TextStyle:
    return word_style(doc, r, res.original.words, res.target, res.new_text, ocr)


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
    if r.kind == LIST:
        targets = {e.word for e in res.edits}
        return [word_item(w) for i, w in enumerate(res.original.words) if res.erase_flags[i] or i in targets]
    if r.kind == ERASE:
        if r.erase_mode == ERASE_PICK:
            return [word_item(w) for w, e in zip(res.original.words, res.erase_flags) if e]
        return [whole]
    if r.kind == VALUE and r.target == TEXT and res.original.words:
        return [word_item(w) for w in res.original.words]
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
        image_page = any(_uses_image(r, results[r.id], doc[page_no]) for r in regions)
        for r in regions:
            for rect, fill in redact_rects(r, results[r.id], image_page):
                page.add_redact_annot(rect, fill=fill, cross_out=False)
        # 표 테두리 같은 선은 남긴다. 이미지 PDF는 지울 자리의 이미지 픽셀까지 실제로 지운다
        # (덮기만 하면 PDF 안에 원래 글자 이미지가 그대로 남아 개인정보가 새어 나갈 수 있음)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS if image_page else pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for r in regions:
            res = results[r.id]
            for e in edits_of(r, res):
                style = e.style or word_style(doc, r, res.original.words, e.word, e.text, ocr)
                _draw_replacement(page, r, res.original, e.word, e.text, style, res.erase_flags)
    try:
        out.subset_fonts()       # 넣은 글꼴에서 실제 쓴 글자만 남겨 파일 크기를 줄인다
    except Exception:  # noqa: BLE001
        pass
    return out


def _uses_image(r: Region, res: RegionResult, page: pymupdf.Page) -> bool:
    """이 영역을 고치려면 이미지 픽셀을 지워야 하는지 (지우거나 바꾸는 단어가 OCR로 읽은 그림 글자인지)."""
    words = res.original.words
    touched = {e.word for e in edits_of(r, res) if e.word is not None}
    touched |= {i for i, e in enumerate(res.erase_flags) if e}
    if touched:
        return any(words[i].ocr for i in touched if i < len(words))
    # 영역 전체를 덮는 경우: 그 자리에 이미지가 있으면
    rect = pymupdf.Rect(r.rect)
    return any(pymupdf.Rect(i["bbox"]).intersects(rect) for i in page.get_image_info())


def _bounds(rect: pymupdf.Rect, words: list[Word], idx: int | None, size: float,
            erased: list[bool] | None = None) -> tuple[float, float]:
    """새 글자를 쓸 수 있는 가로 범위: 영역 안이면서, 같은 줄에 남겨 둔 글자(라벨·단위)와 겹치지 않는 곳."""
    left, right = rect.x0 + 1, rect.x1 - 1
    if idx is not None:
        target = words[idx]
        gap = size * 0.25
        for i, w in enumerate(words):
            if w is target or w.line != target.line or (erased and erased[i]):
                continue
            if w.bbox[2] <= target.bbox[0]:
                left = max(left, w.bbox[2] + gap)
            elif w.bbox[0] >= target.bbox[2]:
                right = min(right, w.bbox[0] - gap)
    return left, right


def _anchor(rect: pymupdf.Rect, info: TextInfo, idx: int | None, left: float, right: float) -> pymupdf.Rect:
    if idx is not None:
        return pymupdf.Rect(info.words[idx].bbox)
    if info.bbox:
        return pymupdf.Rect(info.bbox)
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


def _draw_replacement(page: pymupdf.Page, r: Region, info: TextInfo, idx: int | None, text: str,
                      style: TextStyle, erased: list[bool] | None = None) -> None:
    rect = pymupdf.Rect(r.rect)
    target = info.words[idx] if idx is not None else None
    size = style.size or rect.height * 0.7
    left, right = _bounds(rect, info.words, idx, size, erased)
    anchor = _anchor(rect, info, idx, left, right)
    # 텍스트 바꾸기(줄의 글자를 모두 지우고 새로 씀): 지운 글자들 전체 자리를 기준으로 정렬
    whole_line = bool(target is not None and erased and erased[idx])
    if whole_line:
        for i, w in enumerate(info.words):
            if erased[i] and w.line == target.line:
                anchor |= pymupdf.Rect(w.bbox)
    baseline = target.baseline if target else info.baseline

    if style.raster_dpi and style.face is not None:
        # 이미지 PDF: 원본과 같은 해상도의 그림으로 넣어 글자 번짐 정도까지 맞춘다
        png, (w, h), (ox, oy), (ink0, ink1) = fonts.text_png(text, style.face, size, style.color, style.raster_dpi)
        if ink1 - ink0 > right - left:
            size *= (right - left) / max(ink1 - ink0, 0.01)
            png, (w, h), (ox, oy), (ink0, ink1) = fonts.text_png(text, style.face, size, style.color,
                                                                 style.raster_dpi)
        if style.origin is not None and style.old_advance and not whole_line:
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
            if style.origin is not None:
                y = style.origin[1]
            else:
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
            elif r.kind == LIST:
                sm = list_summary(res)
                msg = f"품번 대조 반영됨 (유지 {sm['유지']} · 줄 삭제 {sm['삭제']} · 가격 수정 {sm['가격수정']})"
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
            if res and r.kind in (ERASE, LIST) and (r.kind == LIST or r.erase_mode == ERASE_PICK):
                erased = " ".join(wd.text for wd, e in zip(res.original.words, res.erase_flags) if e)
            elif res and r.kind == ERASE:
                erased = "(영역 전체)"
            w.writerow([
                r.page + 1, r.name,
                {ERASE: "삭제", LIST: "품번대조"}.get(r.kind, "텍스트" if r.target == TEXT else "숫자"),
                res.original.text if res else "",
                expected_text(r, res) if res and not res.error else "",
                erased,
                "OK" if chk and chk.ok else "확인 필요",
                chk.message if chk else "",
            ])


# ───────────────────────── 엑셀 대조 결과 ─────────────────────────

@dataclass
class BulkRow:
    key: str
    excel_price: str
    pdf_key: str
    pdf_price: str
    status: str          # 화면·CSV 표시용
    level: str           # ok(문제없음) | warn(확인 필요) | err(PDF에 없음) | del(엑셀에 없어 지운 줄)


def bulk_report(tpl: Template, results: dict[str, RegionResult], bulk: BulkList) -> list[BulkRow]:
    """엑셀 품번 하나하나가 PDF에 있었는지, 가격이 맞았는지 + PDF에만 있던 품번."""
    found: dict[str, RowCheck] = {}
    extra: list[RowCheck] = []
    for r in tpl.regions:
        res = results.get(r.id)
        if r.kind != LIST or res is None:
            continue
        for row in res.rows:
            if row.item is not None:
                found.setdefault(norm_key(row.item.key), row)
            elif row.status == "unmatched":
                extra.append(row)
    out = []
    for k, item in bulk.items.items():
        price = format_number(item.price, 0) if item.price is not None else ""
        row = found.get(k)
        if row is None:
            out.append(BulkRow(item.key, price, "", "", "PDF에 없음", "err"))
            continue
        if row.status == "similar":
            status, level = f"비슷한 품번 — 확인 필요 ({row.note})", "warn"
        elif row.price_status == "diff":
            status = "가격 다름 → 엑셀 가격으로 바꿈" if row.new_price and "→" in row.action else "가격 다름 — 확인 필요"
            level = "warn" if "확인" in status else "ok"
        elif row.price_status == "unknown":
            status, level = "PDF에 있음 (가격 위치 못 찾음 — 확인 필요)", "warn"
        elif row.price_status == "ok":
            status, level = "PDF에 있음, 가격 일치", "ok"
        else:
            status, level = "PDF에 있음", "ok"
        out.append(BulkRow(item.key, price, row.key, row.pdf_price, status, level))
    for row in extra:
        deleted = "삭제" in row.action
        out.append(BulkRow("", "", row.key, "", row.action, "del" if deleted else "warn"))
    return out


def write_bulk_report(path: str, rows: list[BulkRow]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["엑셀 품번", "엑셀 가격", "PDF 품번", "PDF 가격", "결과"])
        for x in rows:
            w.writerow([x.key, x.excel_price, x.pdf_key, x.pdf_price, x.status])
