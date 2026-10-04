"""테스트와 시연에 쓰는 견적서 PDF 생성기."""
from __future__ import annotations

import pymupdf

FONT = r"C:\Windows\Fonts\malgun.ttf"

ITEMS = [("모니터 27인치", 320000, 5), ("무선 키보드", 45000, 10), ("USB-C 허브", 38000, 7)]
COLS = [50, 230, 340, 420, 545]           # 품목 | 단가 | 수량 | 금액
TOP, ROW_H = 160, 28


def cell(row: int, col: int) -> list[float]:
    """row 0 = 머리글, 1~3 = 품목, 4 = 공급가액, 5 = 부가세, 6 = 합계"""
    y0 = TOP + row * ROW_H
    return [COLS[col] + 2, y0 + 2, COLS[col + 1] - 2, y0 + ROW_H - 2]


CONTACT = "담당자: 홍길동  010-1234-5678  gildong@example.com"


def make_sample(path: str, items=ITEMS, contact: str = CONTACT) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="k", fontfile=FONT)

    def text(rect, s, size=10, align="left", color=(0, 0, 0)):
        f = pymupdf.Font(fontfile=FONT)
        r = pymupdf.Rect(rect)
        w = f.text_length(s, fontsize=size)
        x = {"left": r.x0 + 4, "right": r.x1 - 4 - w, "center": r.x0 + (r.width - w) / 2}[align]
        y = r.y0 + r.height / 2 + size * 0.35
        page.insert_text((x, y), s, fontname="k", fontsize=size, color=color)

    text([50, 60, 545, 100], "견 적 서", size=22, align="center")
    text([50, 110, 545, 130], "견적일: 2026-10-04    유효기간: 30일", size=9)

    rows = 7
    for i in range(rows + 1):
        y = TOP + i * ROW_H
        page.draw_line((COLS[0], y), (COLS[-1], y), width=0.6)
    for x in COLS:
        page.draw_line((x, TOP), (x, TOP + rows * ROW_H), width=0.6)
    page.draw_rect(pymupdf.Rect(COLS[0], TOP, COLS[-1], TOP + ROW_H), fill=(0.9, 0.93, 0.97), width=0)
    for c, h in enumerate(["품목", "단가", "수량", "금액"]):
        text(cell(0, c), h, align="center")

    supply = 0
    for i, (name, price, qty) in enumerate(items, start=1):
        amount = price * qty
        supply += amount
        text(cell(i, 0), name)
        text(cell(i, 1), f"{price:,}", align="right")
        text(cell(i, 2), f"{qty}", align="right")
        text(cell(i, 3), f"{amount:,}", align="right")
    vat = round(supply * 0.1)
    for row, label, val in [(4, "공급가액", supply), (5, "부가세(10%)", vat), (6, "합계", supply + vat)]:
        text(cell(row, 0), label)
        text(cell(row, 3), f"₩{val:,}", align="right", color=(0.1, 0.2, 0.6) if row == 6 else (0, 0, 0))

    text([50, 380, 545, 400], contact, size=9)
    text([50, 400, 545, 420], "※ 상기 금액은 부가세 포함 금액입니다.", size=9)
    doc.save(path)


CONTACT_RECT = [50, 381, 545, 399]

if __name__ == "__main__":
    import sys
    make_sample(sys.argv[1] if len(sys.argv) > 1 else "sample_quote.pdf")
