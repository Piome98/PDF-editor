"""엑셀 품번 목록 대조 테스트."""
import pymupdf
import pytest

from core.bulk import BulkList, build, guess_columns, load, norm_key, read_table
from core.engine import apply, bulk_report, compute, read_region, verify
from core.models import LIST, Region, Template
from core.ocr import models_available, ocr_page

FONT = r"C:\Windows\Fonts\malgun.ttf"
ITEMS = [("A1234-01", "볼트 M6", "10", "1,200"), ("B5678-02", "너트 M6", "20", "300"),
         ("C9012-03", "와셔", "50", "100"), ("D3456-04", "스프링", "5", "2,500")]
COLS = [50, 170, 330, 400, 540]          # 품번 | 품명 | 수량 | 단가
TOP, ROW_H = 100, 24
TABLE = [45, 95, 545, TOP + ROW_H * 6 + 5]


def make_list_pdf(items=ITEMS) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=400)
    page.insert_font(fontname="k", fontfile=FONT)
    font = pymupdf.Font(fontfile=FONT)

    def put(col, row, text, right=False):
        y = TOP + row * ROW_H + 16
        x = COLS[col + 1] - 6 - font.text_length(text, fontsize=10) if right else COLS[col] + 6
        page.insert_text((x, y), text, fontname="k", fontsize=10)

    for c, h in enumerate(["품번", "품명", "수량", "단가"]):
        put(c, 0, h)
    for r, (key, name, qty, price) in enumerate(items, start=1):
        put(0, r, key)
        put(1, r, name)
        put(2, r, qty, right=True)
        put(3, r, price, right=True)
    put(0, len(items) + 1, "비고: 납기 2주")
    return pymupdf.open("pdf", doc.tobytes())


def to_image(doc, dpi=200):
    out = pymupdf.open()
    for p in doc:
        page = out.new_page(width=p.rect.width, height=p.rect.height)
        page.insert_image(page.rect, pixmap=p.get_pixmap(dpi=dpi))
    return pymupdf.open("pdf", out.tobytes())


def excel(rows) -> BulkList:
    table = [["품번", "품명", "단가"]] + [[k, "", p] for k, p in rows]
    return build(table, *guess_columns(table))


EXCEL = [("A1234-01", "1200"), ("B5678-02", "350"), ("X9999-99", "500")]


# ── 엑셀 읽기 ──
def test_read_xlsx_and_csv(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["번호", "품번", "품명", "단가(원)"])
    ws.append([1, "A1234-01", "볼트", 1200])
    ws.append([2, "B5678-02", "너트", 350.0])
    ws.append([3, None, "빈 품번", 100])
    path = tmp_path / "list.xlsx"
    wb.save(path)
    bulk = load(str(path))
    assert set(bulk.items) == {"A123401", "B567802"} and bulk.skipped == 1
    assert bulk.items["B567802"].price == 350

    csv_path = tmp_path / "list.csv"
    csv_path.write_text("품번,가격\nA1234-01,\"1,200\"\n", encoding="cp949")
    assert load(str(csv_path)).items["A123401"].price == 1200


def test_matching_rules():
    bulk = excel([("A1234-01", "1"), ("209400860", "1")])
    assert bulk.match("a1234 01").kind == "exact"            # 대소문자·구분 기호 무시
    assert bulk.match("품번:A1234-01").kind == "exact"        # 단어 안에 들어 있음
    assert bulk.match("A1Z34-01").kind == "similar"           # OCR 헷갈림(2/Z)
    assert bulk.match("209400B60").kind == "similar"
    assert bulk.match("209400861").kind == "similar"          # 긴 품번 한 글자 다름
    assert bulk.match("C9012-03") is None
    assert bulk.looks_like_key("C9012-03") and bulk.looks_like_key("123456789")
    assert not bulk.looks_like_key("볼트") and norm_key(" ab-12 ") == "AB12"


# ── PDF 대조 ──
def run(doc, bulk, ocr=None, **kw):
    tpl = Template(regions=[Region(0, TABLE, LIST, "품번1", **kw)])
    rid = tpl.regions[0].id
    res = compute(doc, tpl, {}, ocr, bulk)
    assert not res[rid].error, res[rid].error
    return tpl, res, res[rid]


def check_list_result(doc, tpl, res, r, ocr=None):
    by_key = {row.key: row for row in r.rows if row.key}
    assert by_key["A1234-01"].status == "matched" and by_key["A1234-01"].price_status == "ok"
    assert by_key["B5678-02"].price_status == "diff" and by_key["B5678-02"].new_price == "350"
    assert by_key["C9012-03"].status == "unmatched" and by_key["D3456-04"].status == "unmatched"
    others = [row.text for row in r.rows if row.status == "other"]
    assert any("품번" in t for t in others) and any("비고" in t for t in others)   # 머리글·비고는 유지

    out = apply(doc, tpl, res, ocr)
    ocr_out = {i: ocr_page(out[i]) for i in ocr} if ocr else None
    checks = verify(out, tpl, res, ocr_out)
    assert all(c.ok for c in checks), [c.message for c in checks]
    text = read_region(out[0], TABLE, (ocr_out or {}).get(0)).text
    assert "A1234-01" in text and "1,200" in text and "350" in text and "비고" in text
    assert "C9012-03" not in text and "와셔" not in text and "D3456-04" not in text and "2,500" not in text
    return out


def test_list_on_text_pdf():
    doc = make_list_pdf()
    tpl, res, r = run(doc, excel(EXCEL))
    check_list_result(doc, tpl, res, r)

    report = {(x.key or x.pdf_key): x for x in bulk_report(tpl, res, excel(EXCEL))}
    assert report["A1234-01"].level == "ok"
    assert "가격" in report["B5678-02"].status
    assert report["X9999-99"].status == "PDF에 없음"
    assert report["C9012-03"].level == "del" and "삭제" in report["C9012-03"].status


def test_list_options_check_only():
    doc = make_list_pdf()
    tpl, res, r = run(doc, excel(EXCEL), list_unmatched="keep", list_price="check")
    assert not r.changed            # 표시만 → PDF는 그대로
    assert any("가격 다름" in row.action for row in r.rows)


def test_similar_key_is_kept_and_flagged():
    doc = make_list_pdf()
    tpl, res, r = run(doc, excel([("A1234-01", "1200"), ("C9O12-03", "100")]))
    row = next(x for x in r.rows if x.key == "C9012-03")
    assert row.status == "similar" and "확인" in row.action
    assert not any(r.erase_flags[i] for i, w in enumerate(r.original.words) if w.text == "C9012-03")


@pytest.mark.skipif(not models_available(), reason="OCR 모델 없음")
def test_list_on_image_pdf():
    doc = to_image(make_list_pdf())
    ocr = {0: ocr_page(doc[0])}
    tpl, res, r = run(doc, excel(EXCEL), ocr)
    check_list_result(doc, tpl, res, r, ocr)


def test_read_table_rejects_xls(tmp_path):
    p = tmp_path / "old.xls"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        read_table(str(p))
