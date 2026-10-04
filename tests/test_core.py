from decimal import Decimal

import pymupdf
import pytest

from core.engine import apply, check_template, compute, read_region, verify
from core.formula import FormulaError, evaluate, expand_wildcards
from core.models import ERASE, VALUE, Region, Template
from core.numbers import detect_style, format_number, parse_number
from tests.sample import CONTACT_RECT, cell, make_sample


# ── 숫자 ──
@pytest.mark.parametrize("text,expected", [
    ("₩1,234,000", Decimal("1234000")), ("12.5%", Decimal("12.5")), ("-3,000원", Decimal("-3000")),
    ("△500", Decimal("-500")), ("(1,000)", Decimal("-1000")), ("없음", None), ("", None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_detect_and_format_roundtrip():
    st = detect_style("₩1,234,000원")
    assert (st.prefix, st.suffix, st.decimals, st.thousands) == ("₩", "원", 0, True)
    assert format_number(Decimal("2500000.4"), st.decimals, st.thousands, st.prefix, st.suffix) == "₩2,500,000원"
    assert format_number(Decimal("-12.345"), 2) == "-12.35"


# ── 수식 ──
def test_formula_basic_and_wildcard():
    vals = {"단가1": Decimal(100), "수량1": Decimal(3), "금액1": Decimal(300), "금액2": Decimal(50)}
    assert evaluate("단가1*수량1", vals.__getitem__) == 300
    expr = expand_wildcards("SUM(금액*)", list(vals))
    assert evaluate(expr, vals.__getitem__) == 350
    assert evaluate("ROUND(1234*0.1, -1)", vals.__getitem__) == Decimal("120")
    assert evaluate("ROUNDDOWN(10/3, 2)", vals.__getitem__) == Decimal("3.33")


@pytest.mark.parametrize("expr", ["__import__('os')", "a.b", "1 +", "[1,2]"])
def test_formula_rejects_unsafe(expr):
    with pytest.raises(FormulaError):
        evaluate(expr, lambda n: Decimal(1))


# ── 전체 파이프라인 ──
def build_template() -> Template:
    regions = []
    for i in range(1, 4):
        regions += [
            Region(0, cell(i, 1), VALUE, f"단가{i}"),
            Region(0, cell(i, 2), VALUE, f"수량{i}"),
            Region(0, cell(i, 3), VALUE, f"금액{i}", formula=f"단가{i}*수량{i}"),
        ]
    regions += [
        Region(0, cell(4, 3), VALUE, "공급가액", formula="SUM(금액*)", prefix="₩"),
        Region(0, cell(5, 3), VALUE, "부가세", formula="ROUND(공급가액*0.1)", prefix="₩"),
        Region(0, cell(6, 3), VALUE, "합계", formula="공급가액+부가세", prefix="₩"),
        Region(0, CONTACT_RECT, ERASE, "담당자", fill="#FFFFFF"),
    ]
    return Template(page_sizes=[[595, 842]], regions=regions)


@pytest.fixture
def sample(tmp_path):
    p = tmp_path / "quote.pdf"
    make_sample(str(p))
    return pymupdf.open(str(p))


def test_read_region_only_inside(sample):
    assert read_region(sample[0], cell(1, 1)).text == "320,000"
    assert read_region(sample[0], cell(6, 3)).text == "₩2,547,600"


def test_full_pipeline(sample):
    tpl = build_template()
    assert check_template(sample, tpl) == []
    ids = {r.name: r.id for r in tpl.regions}

    # 단가1만 새로 입력, 나머지 수량/단가는 원본 유지 → 금액/합계는 수식으로 재계산
    results = compute(sample, tpl, {ids["단가1"]: "300000"})
    assert all(not r.error for r in results.values()), [r.error for r in results.values()]
    assert results[ids["금액1"]].new_text == "1,500,000"
    assert results[ids["공급가액"]].new_text == "₩2,216,000"
    assert results[ids["부가세"]].new_text == "₩221,600"
    assert results[ids["합계"]].new_text == "₩2,437,600"
    assert not results[ids["수량1"]].changed          # 입력 없음 → 건드리지 않음
    assert not results[ids["금액2"]].changed          # 값이 같으면 건드리지 않음

    out = apply(sample, tpl, results)
    checks = verify(out, tpl, results)
    assert all(c.ok for c in checks), [c.message for c in checks if not c.ok]

    # 영역 밖 텍스트(품목명, 안내 문구)는 그대로 남아 있어야 한다
    page_text = out[0].get_text()
    assert "모니터 27인치" in page_text and "부가세 포함" in page_text
    assert "홍길동" not in page_text
    # 표 테두리 선이 유지되어야 한다
    lines = lambda pg: sorted(str(d["items"]) for d in pg.get_drawings() if d["type"] == "s")
    assert lines(out[0]) == lines(sample[0])


def test_errors_are_reported(sample):
    tpl = build_template()
    ids = {r.name: r.id for r in tpl.regions}
    tpl.regions[0].formula = "없는이름*2"
    results = compute(sample, tpl, {ids["수량1"]: "abc"})
    assert "없는이름" in results[ids["단가1"]].error
    assert "숫자가 아닙니다" in results[ids["수량1"]].error
    assert results[ids["금액1"]].error  # 단가1 오류가 금액1로 전파


def test_template_json_roundtrip(tmp_path):
    tpl = build_template()
    p = tmp_path / "t.json"
    tpl.save(str(p))
    loaded = Template.load(str(p))
    assert loaded.to_json() == tpl.to_json()


# ── 영역 안 텍스트 읽기 / 골라 지우기 ──
from core.engine import read_words  # noqa: E402
from core.models import ERASE_PICK  # noqa: E402


def test_read_words_in_reading_order(sample):
    words = [w.text for w in read_words(sample[0], CONTACT_RECT)]
    assert words == ["담당자:", "홍길동", "010-1234-5678", "gildong@example.com"]
    # 여러 줄: 위→아래, 왼→오른쪽
    two_lines = [w.text for w in read_words(sample[0], [50, 381, 545, 419])]
    assert two_lines[:2] == ["담당자:", "홍길동"] and two_lines[-1] == "금액입니다."


def pick_template() -> Template:
    r = Region(0, CONTACT_RECT, ERASE, "담당자", erase_mode=ERASE_PICK)
    r.set_erase("담당자:", False)       # 라벨은 남기고 나머지는 지움
    return Template(page_sizes=[[595, 842]], regions=[r])


def test_pick_erase_keeps_selected_words(sample):
    tpl = pick_template()
    results = compute(sample, tpl, {})
    res = results[tpl.regions[0].id]
    assert res.erase_flags == [False, True, True, True]
    out = apply(sample, tpl, results)
    assert all(c.ok for c in verify(out, tpl, results))
    assert read_region(out[0], CONTACT_RECT).text == "담당자:"
    assert "부가세 포함 금액입니다" in out[0].get_text()      # 바로 아래 줄은 그대로


def test_pick_template_reused_on_other_document(tmp_path):
    p = tmp_path / "other.pdf"
    make_sample(str(p), contact="담당자: 김철수  02-555-0000  chulsoo@example.com")
    doc = pymupdf.open(str(p))
    tpl = pick_template()
    assert check_template(doc, tpl) == []
    results = compute(doc, tpl, {})
    out = apply(doc, tpl, results)
    assert all(c.ok for c in verify(out, tpl, results))
    assert read_region(out[0], CONTACT_RECT).text == "담당자:"


def test_pick_template_warns_when_label_missing(tmp_path):
    p = tmp_path / "other.pdf"
    make_sample(str(p), contact="영업: 김철수  02-555-0000")
    warnings = check_template(pymupdf.open(str(p)), pick_template())
    assert any("남길 글자" in w for w in warnings)


def test_value_region_replaces_only_number(sample):
    # '합계' 라벨 칸까지 포함한 넓은 영역이어도 라벨은 남고 숫자만 바뀐다
    row = [cell(6, 0)[0], cell(6, 0)[1], cell(6, 3)[2], cell(6, 3)[3]]
    tpl = Template(regions=[Region(0, row, VALUE, "합계", prefix="₩")])
    rid = tpl.regions[0].id
    results = compute(sample, tpl, {rid: "3000000"})
    assert results[rid].base_text == "₩2,547,600"
    out = apply(sample, tpl, results)
    assert all(c.ok for c in verify(out, tpl, results))
    assert read_region(out[0], row).text == "합계 ₩3,000,000"
