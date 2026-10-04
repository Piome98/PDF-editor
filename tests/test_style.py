"""바꾼 글자가 원본과 같은 글꼴·크기·색으로 나오는지 확인한다.

정답 = 처음부터 새 숫자로 만든 문서. 결과를 정답과 그림으로 비교한다.
"""
import pymupdf
import pytest

from core.engine import apply, compute, read_words
from core.fonts import _base
from core.models import VALUE, Region, Template
from core.ocr import models_available, ocr_page
from tests.style_cases import CASES, NEW, OLD, REGION, difference, make_doc, to_image

IDS = [c[0] for c in CASES]


def run(src, ocr=None, **region_kw):
    tpl = Template(regions=[Region(0, REGION, VALUE, "금액", **region_kw)])
    rid = tpl.regions[0].id
    res = compute(src, tpl, {rid: NEW}, ocr)
    assert not res[rid].error, res[rid].error
    return res[rid], apply(src, tpl, res, ocr)


@pytest.mark.parametrize("label,font,bold,size,color", CASES, ids=IDS)
def test_text_pdf_reuses_original_font(label, font, bold, size, color):
    src, truth = make_doc(font, bold, size, color, OLD), make_doc(font, bold, size, color, NEW)
    res, out = run(src)
    assert res.style.source == "pdf"                 # PDF 안의 원본 글꼴을 그대로 사용
    got = [w for w in read_words(out[0], REGION) if w.text == NEW][0]
    want = [w for w in read_words(truth[0], REGION) if w.text == NEW][0]
    assert _base(got.font) == _base(want.font) and got.bold == want.bold
    assert got.size == pytest.approx(want.size, abs=0.01)
    assert got.color == pytest.approx(want.color, abs=0.01)
    assert got.bbox == pytest.approx(want.bbox, abs=0.15)
    assert difference(out, truth) < 8


@pytest.mark.skipif(not models_available(), reason="OCR 모델 없음")
@pytest.mark.parametrize("dpi", [150, 200])
@pytest.mark.parametrize("label,font,bold,size,color", CASES, ids=IDS)
def test_image_pdf_matches_font_size_color(label, font, bold, size, color, dpi):
    src, truth = make_doc(font, bold, size, color, OLD), make_doc(font, bold, size, color, NEW)
    isrc, itruth = to_image(src, dpi), to_image(truth, dpi)
    ocr = {0: ocr_page(isrc[0])}
    res, out = run(isrc, ocr)
    st = res.style
    assert st.source == "matched"
    assert _base(st.face.family) == _base(font) and st.face.bold == bold, st.describe()
    assert st.size == pytest.approx(size, abs=0.2), st.describe()
    # 150dpi에서 9pt 글자의 획은 1픽셀 남짓이라 색 추정 오차가 조금 더 크다
    assert st.color == pytest.approx(color, abs=0.07 if dpi >= 200 else 0.09), st.describe()
    assert difference(out, itruth) < 25


@pytest.mark.skipif(not models_available(), reason="OCR 모델 없음")
def test_number_is_not_split_into_pieces():
    """돋움처럼 쉼표 간격이 넓은 글꼴에서도 1,234,000이 한 단어로 읽혀야 숫자 일부만 바뀌는 사고가 없다."""
    src = to_image(make_doc("Dotum", False, 10.0, (0, 0, 0), OLD), 150)
    words = [w.text for w in ocr_page(src[0])]
    assert OLD in words, words


def test_user_overrides_win():
    src = make_doc("Gulim", False, 9.0, (0.1, 0.2, 0.6), OLD)
    res, out = run(src, font="바탕", font_size=12, color="#008000")
    assert res.style.source == "user" and res.style.size == 12
    got = [w for w in read_words(out[0], REGION) if w.text == NEW][0]
    assert _base(got.font) == "batang" and got.size == pytest.approx(12, abs=0.01)
    assert got.color == pytest.approx((0, 128 / 255, 0), abs=0.01)
