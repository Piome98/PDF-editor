"""이미지 PDF(스캔본·PDF 프린터 출력) 처리 테스트. OCR 모델이 없으면 건너뛴다."""
import re

import pymupdf
import pytest

from core.engine import apply, check_template, compute, read_region, read_words, verify
from core.models import ERASE, ERASE_PICK, VALUE, Region, Template
from core.ocr import models_available, needs_ocr, ocr_page
from tests.sample import CONTACT_RECT, cell, make_sample

pytestmark = pytest.mark.skipif(not models_available(), reason="OCR 모델 없음 (tools/fetch_models.py 실행)")


def to_image_pdf(src: pymupdf.Document, dpi: int = 150) -> pymupdf.Document:
    """'PDF 프린터로 저장'한 것처럼 글자 정보 없이 이미지만 있는 PDF로 바꾼다."""
    out = pymupdf.open()
    for p in src:
        page = out.new_page(width=p.rect.width, height=p.rect.height)
        page.insert_image(page.rect, pixmap=p.get_pixmap(dpi=dpi))
    return pymupdf.open("pdf", out.tobytes())


@pytest.fixture(scope="module")
def text_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("ocr") / "quote.pdf"
    make_sample(str(p))
    return pymupdf.open(str(p))


@pytest.fixture(scope="module")
def image_pdf(text_pdf):
    return to_image_pdf(text_pdf)


@pytest.fixture(scope="module")
def ocr(image_pdf):
    return {0: ocr_page(image_pdf[0])}


def reocr(out, ocr):
    """결과 PDF에는 새로 쓴 숫자가 글자로 들어가므로, 원본에서 OCR한 쪽을 그대로 다시 OCR한다."""
    return {i: ocr_page(out[i]) for i in ocr}


def test_detects_image_pdf(text_pdf, image_pdf):
    assert not needs_ocr(text_pdf[0])
    assert needs_ocr(image_pdf[0])


def test_ocr_matches_text_layer(text_pdf, ocr):
    ref = read_words(text_pdf[0], text_pdf[0].rect)
    got = {w.text: w for w in ocr[0]}
    missing = [w.text for w in ref if w.text not in got]
    assert not missing, missing
    for w in ref:   # 숫자 위치·크기는 새 값을 같은 자리에 쓰는 데 쓰이므로 정확해야 한다
        if re.fullmatch(r"₩?[\d,.\-]+", w.text):
            o = got[w.text]
            assert abs(o.bbox[2] - w.bbox[2]) < 1.0 and abs(o.baseline - w.baseline) < 0.6
            assert abs(o.size - w.size) < 0.8


def test_pick_erase_on_image_pdf(image_pdf, ocr):
    r = Region(0, CONTACT_RECT, ERASE, "담당자", erase_mode=ERASE_PICK)
    r.set_erase("담당자:", False)
    tpl = Template(regions=[r])
    assert check_template(image_pdf, tpl, ocr) == []
    results = compute(image_pdf, tpl, {}, ocr)
    assert results[r.id].erase_flags == [False, True, True, True]

    out = apply(image_pdf, tpl, results, ocr)
    checks = verify(out, tpl, results, reocr(out, ocr))
    assert all(c.ok for c in checks), [c.message for c in checks]
    # 덮기만 한 것이 아니라 이미지 픽셀이 실제로 지워져서, 덮개를 치워도 이름이 남아 있지 않아야 한다
    clean = pymupdf.open("pdf", out.tobytes())
    for annot in clean[0].annots():
        clean[0].delete_annot(annot)
    assert "홍길동" not in read_region(clean[0], CONTACT_RECT, ocr_page(clean[0])).text


def test_value_replace_on_image_pdf(image_pdf, ocr):
    regions = []
    for i in range(1, 4):
        regions += [Region(0, cell(i, 1), VALUE, f"단가{i}"), Region(0, cell(i, 2), VALUE, f"수량{i}"),
                    Region(0, cell(i, 3), VALUE, f"금액{i}", formula=f"단가{i}*수량{i}")]
    regions += [Region(0, cell(4, 3), VALUE, "공급가액", formula="SUM(금액*)", prefix="₩"),
                Region(0, cell(5, 3), VALUE, "부가세", formula="ROUND(공급가액*0.1)", prefix="₩"),
                Region(0, cell(6, 3), VALUE, "합계", formula="공급가액+부가세", prefix="₩")]
    tpl = Template(regions=regions)
    ids = {r.name: r.id for r in regions}
    results = compute(image_pdf, tpl, {ids["단가1"]: "300000"}, ocr)
    assert all(not r.error for r in results.values()), [r.error for r in results.values()]
    assert results[ids["합계"]].new_text == "₩2,437,600"

    out = apply(image_pdf, tpl, results, ocr)
    checks = verify(out, tpl, results, reocr(out, ocr))
    assert all(c.ok for c in checks), [c.message for c in checks if not c.ok]


def test_text_target_on_image_pdf(image_pdf, ocr):
    from core.models import TEXT
    r = Region(0, CONTACT_RECT, VALUE, "담당자", target=TEXT, align="left")
    tpl = Template(regions=[r])
    results = compute(image_pdf, tpl, {r.id: "담당자: 김철수 02-555-0000"}, ocr)
    assert results[r.id].changed and results[r.id].style.source == "matched"
    out = apply(image_pdf, tpl, results, ocr)
    checks = verify(out, tpl, results, reocr(out, ocr))
    assert all(c.ok for c in checks), [c.message for c in checks]
