"""모든 파일에 OCR을 돌렸을 때: 텍스트 레이어와 OCR을 합쳐 쓰는지."""
import pymupdf
import pytest

from core.engine import apply, compute, merge_words, read_region, read_words, verify
from core.models import VALUE, Region, Template
from core.ocr import models_available, ocr_page
from core.words import Word
from tests.sample import cell, make_sample
from tests.test_core import build_template


def w(text, bbox, ocr=False, score=1.0, invisible=False):
    return Word(text, bbox, 10, (0, 0, 0), bbox[3], ocr=ocr, score=score, invisible=invisible)


def test_merge_rules():
    tl = [w("1,234", (10, 10, 40, 20)), w("\ue001\ue002", (50, 10, 70, 20)), w("ABCD", (80, 10, 110, 20)),
          w("숨김", (120, 10, 140, 20), invisible=True)]
    oc = [w("1,234", (10, 10, 40, 20), ocr=True), w("단가", (50, 10, 70, 20), ocr=True),
          w("5,678", (80, 10, 110, 20), ocr=True, score=0.95), w("숨김", (120, 10, 140, 20), ocr=True),
          w("그림글자", (200, 10, 240, 20), ocr=True)]
    got = {x.text: x.ocr for x in merge_words(tl, oc)}
    assert got == {"1,234": False,      # 제대로 읽히는 텍스트 레이어는 그대로
                   "단가": True,        # 깨진 글자 → OCR
                   "5,678": True,       # OCR이 확실히 다르게 읽음 → 보이는 모양(OCR)을 믿음
                   "숨김": True,        # 안 보이는 글자 → 이미지 글자를 고쳐야 하므로 OCR
                   "그림글자": True}    # 텍스트가 없는 곳 → OCR


pytestmark = pytest.mark.skipif(not models_available(), reason="OCR 모델 없음")


@pytest.fixture(scope="module")
def text_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("hy") / "quote.pdf"
    make_sample(str(p))
    return pymupdf.open(str(p))


def test_text_pdf_with_ocr_keeps_text_layer_quality(text_pdf):
    ocr = {0: ocr_page(text_pdf[0])}
    tpl = build_template()
    ids = {r.name: r.id for r in tpl.regions}
    res = compute(text_pdf, tpl, {ids["단가1"]: "300000"}, ocr)
    target = res[ids["합계"]]
    assert not target.original.words[target.target].ocr          # 텍스트 레이어 단어를 씀
    assert target.style.source == "pdf"                            # 원본 글꼴 재사용
    out = apply(text_pdf, tpl, res, ocr)
    assert not out[0].get_images()                                 # 그림으로 넣지 않음
    checks = verify(out, tpl, res, {0: ocr_page(out[0])})
    assert all(c.ok for c in checks), [c.message for c in checks if not c.ok]


def scanned_with_hidden_text(src: pymupdf.Document) -> pymupdf.Document:
    """스캐너가 OCR해 둔 PDF처럼: 페이지는 그림이고, 그 위에 안 보이는 글자가 깔려 있다."""
    out = pymupdf.open()
    page = out.new_page(width=src[0].rect.width, height=src[0].rect.height)
    page.insert_image(page.rect, pixmap=src[0].get_pixmap(dpi=200))
    page.insert_font(fontname="k", fontfile=r"C:\Windows\Fonts\malgun.ttf")
    for t in read_words(src[0], src[0].rect):
        page.insert_text((t.bbox[0], t.baseline), t.text, fontname="k", fontsize=t.size, render_mode=3)
    return pymupdf.open("pdf", out.tobytes())


def test_hidden_text_layer_is_replaced_by_ocr(text_pdf):
    doc = scanned_with_hidden_text(text_pdf)
    region = cell(1, 1)
    assert read_region(doc[0], region).words[0].invisible            # 텍스트 레이어만 보면 안 보이는 글자
    ocr = {0: ocr_page(doc[0])}
    tpl = Template(regions=[Region(0, region, VALUE, "단가1")])
    rid = tpl.regions[0].id
    res = compute(doc, tpl, {rid: "300000"}, ocr)
    assert res[rid].original.words[res[rid].target].ocr             # 보이는 그림 글자(OCR)를 고친다
    out = apply(doc, tpl, res, ocr)
    seen = read_region(out[0], region, ocr_page(out[0])).text        # 화면에 보이는 것
    assert "300,000" in seen and "320,000" not in seen
