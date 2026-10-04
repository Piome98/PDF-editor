"""숫자 텍스트 파싱과 출력 형식."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# 음수 표기: -1,000 / −1,000 / △1,000 / ▲1,000 / (1,000)
_NUM = re.compile(
    r"(?P<neg>[-−△▲]|\()?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<close>\))?"
)


@dataclass
class NumberStyle:
    prefix: str = ""
    suffix: str = ""
    decimals: int = 0
    thousands: bool = True


def parse_number(text: str) -> Decimal | None:
    """텍스트에서 첫 번째 숫자를 찾아 Decimal로 돌려준다. 없으면 None."""
    if not text:
        return None
    m = _NUM.search(text)
    if not m:
        return None
    try:
        value = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    neg = m.group("neg")
    if neg and (neg != "(" or m.group("close")):
        value = -value
    return value


def detect_style(text: str) -> NumberStyle:
    """원본 텍스트의 표기 방식(₩, 원, 소수 자리, 천 단위 구분)을 추정한다."""
    m = _NUM.search(text or "")
    if not m:
        return NumberStyle()
    num = m.group("num")
    decimals = len(num.split(".")[1]) if "." in num else 0
    int_part = num.split(".")[0]
    thousands = "," in int_part or len(int_part) <= 3
    prefix = text[:m.start()].strip()
    suffix = text[m.end():].strip()
    if m.group("neg") and m.group("neg") != "(":
        prefix = text[:m.start("neg")].strip()
    return NumberStyle(prefix=prefix, suffix=suffix, decimals=decimals, thousands=thousands)


def format_number(value: Decimal, decimals: int = 0, thousands: bool = True,
                  prefix: str = "", suffix: str = "") -> str:
    q = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    body = f"{abs(q):,.{decimals}f}" if thousands else f"{abs(q):.{decimals}f}"
    sign = "-" if q < 0 else ""
    return f"{prefix}{sign}{body}{suffix}"
