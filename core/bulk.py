"""엑셀(품번·가격) 목록을 읽고, PDF 단어가 그 품번인지 맞춰 보는 기능."""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .numbers import parse_number

KEY_HEADERS = ("품번", "품목번호", "품목코드", "부품번호", "제품번호", "자재번호", "자재코드", "모델", "모델명",
               "코드", "part", "partno", "pn", "p/n", "item", "itemno", "itemcode", "sku", "code", "model")
PRICE_HEADERS = ("가격", "단가", "판매가", "공급가", "금액", "price", "unitprice", "amount", "cost")

# OCR이 흔히 헷갈리는 글자 → 같은 글자로 보고 비교 ('비슷한 품번'으로 표시)
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1",
                             "Z": "2", "S": "5", "B": "8", "G": "6"})


def norm_key(text: str) -> str:
    """비교용 품번: 전각/반각 통일, 대문자, 공백·하이픈 같은 구분 기호 제거."""
    t = unicodedata.normalize("NFKC", text or "").upper()
    return re.sub(r"[^0-9A-Z가-힣]", "", t)


def key_shape(key: str) -> str:
    """품번 모양: 숫자는 9, 영문은 A, 한글은 가. 예) AB-1234 → AA9999"""
    return re.sub(r"[0-9]", "9", re.sub(r"[A-Z]", "A", re.sub(r"[가-힣]", "가", key)))


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


@dataclass
class BulkItem:
    key: str                  # 엑셀에 적힌 품번 그대로
    price: Decimal | None     # 엑셀 가격 (없으면 품번 확인만)
    row: int                  # 엑셀 행 번호 (1부터)


@dataclass
class Match:
    kind: str                 # exact(일치) | similar(비슷함 — 확인 필요)
    item: BulkItem
    note: str = ""


@dataclass
class BulkList:
    items: dict[str, BulkItem] = field(default_factory=dict)    # 비교용 품번 → 항목
    source: str = ""
    duplicates: list[str] = field(default_factory=list)
    skipped: int = 0                                           # 품번이 비어 있어 건너뛴 행

    def __post_init__(self):
        self._reindex()

    def _reindex(self) -> None:
        self._confused = {}
        self._by_len: dict[int, list[str]] = {}
        for k in self.items:
            self._confused.setdefault(k.translate(_CONFUSABLE), []).append(k)
            self._by_len.setdefault(len(k), []).append(k)
        self.shapes = {key_shape(k) for k in self.items}
        self.lengths = sorted(self._by_len)

    def add(self, key: str, price: Decimal | None, row: int) -> None:
        k = norm_key(key)
        if not k:
            self.skipped += 1
            return
        if k in self.items:
            self.duplicates.append(key)
        self.items[k] = BulkItem(key.strip(), price, row)

    def finish(self) -> "BulkList":
        self._reindex()
        return self

    def match(self, text: str) -> Match | None:
        k = norm_key(text)
        if len(k) < 2:
            return None
        if k in self.items:
            return Match("exact", self.items[k])
        # 단어 안에 품번이 들어 있는 경우 (예: '품번:A1234', OCR이 옆 글자와 붙여 읽은 경우)
        for n in self.lengths:
            if n >= 4 and n < len(k):
                for i in range(len(k) - n + 1):
                    if k[i:i + n] in self.items:
                        return Match("exact", self.items[k[i:i + n]], f"'{text}' 안에서 찾음")
        # OCR이 헷갈리기 쉬운 글자(0/O, 1/I, 5/S, 8/B ...)만 다른 경우
        same = self._confused.get(k.translate(_CONFUSABLE), [])
        if len(same) == 1:
            return Match("similar", self.items[same[0]], f"엑셀 '{self.items[same[0]].key}'와 글자 모양만 다름")
        # 한 글자만 다른 경우 (긴 품번만)
        if len(k) >= 6:
            near = [c for n in (len(k) - 1, len(k), len(k) + 1) for c in self._by_len.get(n, [])
                    if _within_one_edit(k, c)]
            if len(near) == 1:
                return Match("similar", self.items[near[0]], f"엑셀 '{self.items[near[0]].key}'와 한 글자 다름")
        return None

    def looks_like_key(self, text: str) -> bool:
        """엑셀 품번과 모양(숫자/영문 배치)이 같은 단어인지 — 엑셀에 없는 품번 줄을 찾는 데 쓴다."""
        k = norm_key(text)
        if len(k) < 4:
            return False
        if key_shape(k) in self.shapes:
            return True
        # 숫자로만 된 품번은 길이가 ±1까지 같으면 품번으로 본다
        return k.isdigit() and any(set(s) == {"9"} and abs(len(s) - len(k)) <= 1 for s in self.shapes)


# ───────────────────────── 엑셀/CSV 읽기 ─────────────────────────

def read_table(path: str) -> list[list[str]]:
    """첫 번째 시트(또는 CSV)를 문자열 표로 읽는다."""
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else _cell_text(v) for v in row])
        wb.close()
        return _trim(rows)
    if ext in (".csv", ".txt"):
        for enc in ("utf-8-sig", "cp949"):
            try:
                with open(path, newline="", encoding=enc) as f:
                    return _trim([list(r) for r in csv.reader(f)])
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 글자 인코딩을 알 수 없습니다 (UTF-8 또는 CP949로 저장하세요)")
    if ext == ".xls":
        raise ValueError("예전 엑셀 형식(.xls)은 읽을 수 없습니다. 엑셀에서 .xlsx로 다른 이름으로 저장해 주세요.")
    raise ValueError(f"지원하지 않는 파일 형식입니다: {ext}")


def _cell_text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _trim(rows: list[list[str]]) -> list[list[str]]:
    rows = [r for r in rows if any(c.strip() for c in r)]
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def _header_name(cell: str) -> str:
    return re.sub(r"[\s_.\-()]", "", cell).lower()


def guess_columns(rows: list[list[str]]) -> tuple[int, int, int]:
    """(머리글 행 번호 또는 -1, 품번 열, 가격 열 또는 -1)을 추정한다."""
    for hi, row in enumerate(rows[:10]):
        names = [_header_name(c) for c in row]
        key = next((i for i, n in enumerate(names) if n and any(n == h or n.startswith(h) for h in KEY_HEADERS)), -1)
        price = next((i for i, n in enumerate(names) if n and any(h in n for h in PRICE_HEADERS)), -1)
        if key >= 0:
            return hi, key, price
    # 머리글이 없으면: 숫자가 아닌 값이 많은 첫 열 = 품번, 숫자 값이 많은 다음 열 = 가격
    width = len(rows[0]) if rows else 0
    numeric = [sum(1 for r in rows if parse_number(r[c]) is not None and re.fullmatch(r"[\d,.\s₩원$-]+", r[c] or ""))
               for c in range(width)]
    key = 0
    price = next((c for c in range(width) if c != key and numeric[c] >= len(rows) * 0.6), -1)
    return -1, key, price


def build(rows: list[list[str]], header_row: int, key_col: int, price_col: int, source: str = "") -> BulkList:
    bulk = BulkList(source=source)
    for i, row in enumerate(rows):
        if i <= header_row:
            continue
        key = row[key_col] if key_col < len(row) else ""
        price = parse_number(row[price_col]) if 0 <= price_col < len(row) else None
        bulk.add(key, price, i + 1)
    return bulk.finish()


def load(path: str) -> BulkList:
    rows = read_table(path)
    header, key_col, price_col = guess_columns(rows)
    return build(rows, header, key_col, price_col, path)
