"""엑셀 비슷한 간단한 수식 계산기.

예) 단가1*수량1, SUM(금액*), ROUND(합계*0.1, 0)
- 이름은 영역 이름(한글 가능)을 그대로 쓴다.
- `금액*` 처럼 이름 뒤에 *를 붙여 함수 인자로 넣으면 '금액'으로 시작하는 모든 영역을 뜻한다.
- eval()을 쓰지 않고 AST를 직접 계산하므로 임의 코드가 실행되지 않는다.
"""
from __future__ import annotations

import ast
import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, DivisionByZero, InvalidOperation
from typing import Callable

FUNCTIONS = ("SUM", "ROUND", "ROUNDUP", "ROUNDDOWN", "MIN", "MAX", "ABS")

_WILDCARD = re.compile(r"([^\W\d][\w]*)\*(?=\s*[,)])")


class FormulaError(Exception):
    pass


def is_valid_name(name: str) -> bool:
    return name.isidentifier() and name.upper() not in FUNCTIONS


def expand_wildcards(expr: str, names: list[str]) -> str:
    def repl(m: re.Match) -> str:
        prefix = m.group(1)
        matched = [n for n in names if n.startswith(prefix)]
        if not matched:
            raise FormulaError(f"'{prefix}*'에 해당하는 영역이 없습니다")
        return ", ".join(matched)

    return _WILDCARD.sub(repl, expr)


def _round(x: Decimal, digits: Decimal, mode: str) -> Decimal:
    exp = Decimal(1).scaleb(-int(digits))
    return x.quantize(exp, rounding=mode)


def evaluate(expr: str, resolve: Callable[[str], Decimal]) -> Decimal:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        raise FormulaError("수식 문법이 올바르지 않습니다") from None

    def ev(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name):
            return resolve(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                if b == 0:
                    raise FormulaError("0으로 나눌 수 없습니다")
                return a / b
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            fn = node.func.id.upper()
            args = [ev(a) for a in node.args]
            if fn == "SUM":
                return sum(args, Decimal(0))
            if fn in ("MIN", "MAX") and args:
                return min(args) if fn == "MIN" else max(args)
            if fn == "ABS" and len(args) == 1:
                return abs(args[0])
            if fn in ("ROUND", "ROUNDUP", "ROUNDDOWN") and len(args) in (1, 2):
                digits = args[1] if len(args) == 2 else Decimal(0)
                mode = {"ROUND": ROUND_HALF_UP, "ROUNDUP": ROUND_UP, "ROUNDDOWN": ROUND_DOWN}[fn]
                return _round(args[0], digits, mode)
            raise FormulaError(f"함수 {node.func.id}의 사용법이 올바르지 않습니다")
        raise FormulaError("지원하지 않는 수식 요소가 있습니다")

    try:
        return ev(tree)
    except (InvalidOperation, DivisionByZero):
        raise FormulaError("계산할 수 없는 값입니다") from None
