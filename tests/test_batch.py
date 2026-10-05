"""여러 파일 일괄 처리 테스트."""
import csv
import os

import pytest

from core.batch import (BatchFile, missing_everywhere, ocr_sync, open_file, output_name, process, safe_name, save,
                        unique_path, verify_saved, write_batch_report)
from core.engine import read_region
from core.models import LIST, Region, Template
from core.ocr import models_available, ocr_page
from tests.test_bulk import TABLE, excel, make_list_pdf, to_image

EXCEL = [("A1234-01", "1200"), ("B5678-02", "350"), ("X9999-99", "500"), ("Z1111-11", "700")]
FILE2 = [("X9999-99", "볼트", "1", "500"), ("E0000-01", "핀", "3", "90")]
FILE3 = [("A1234-01", "볼트 M6", "10", "1,200")]


def test_output_name_rules(tmp_path):
    assert output_name(3, 12, ["A1234-01", "B5678-02"], "x") == "03. A1234-01, B5678-02.pdf"
    assert output_name(7, 150, [], "견적서 원본") == "007. 견적서 원본.pdf"         # 품번이 없으면 원래 이름
    assert safe_name('A/B:C*D?"E<F>G|') == "A_B_C_D__E_F_G_"
    long = output_name(1, 2, [f"PART-{i:05d}" for i in range(40)], "x")
    assert len(long) <= 130 and long.endswith("개.pdf") and " 외 " in long
    taken: set[str] = set()
    a = unique_path(str(tmp_path), "01. A.pdf", taken)
    b = unique_path(str(tmp_path), "01. A.pdf", taken)
    assert os.path.basename(b) == "01. A (2).pdf" and a != b


@pytest.fixture
def files(tmp_path):
    paths = []
    for i, (items, image) in enumerate([(None, False), (FILE2, False), (FILE3, True)], start=1):
        doc = make_list_pdf(items) if items else make_list_pdf()
        if image:
            doc = to_image(doc)
        p = tmp_path / f"면장_{i}.pdf"
        doc.save(str(p))
        paths.append(str(p))
    return paths


@pytest.mark.skipif(not models_available(), reason="OCR 모델 없음")
def test_batch_end_to_end(files, tmp_path):
    tpl = Template(regions=[Region(0, TABLE, LIST, "품번1")])
    bulk = excel(EXCEL)
    batch = [BatchFile(p, n) for n, p in enumerate(files, start=1)]
    for bf in batch:
        open_file(bf)
        ocr_sync(bf)
        process(bf, tpl, bulk, total=len(batch))

    assert batch[2].ocr_pages == [0] and not batch[0].ocr_pages        # 세 번째만 이미지 PDF
    assert [bf.out_name for bf in batch] == ["01. A1234-01, B5678-02.pdf", "02. X9999-99.pdf", "03. A1234-01.pdf"]
    assert missing_everywhere(batch, bulk) == ["Z1111-11"]
    s1 = batch[0].summary()
    assert (s1["유지"], s1["삭제"], s1["가격수정"]) == (2, 2, 1)
    assert batch[1].summary()["삭제"] == 1                               # E0000-01 줄 삭제

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    taken: set[str] = set()
    for bf in batch:
        out = save(bf, tpl, str(out_dir), taken)
        ocr_out = {p: ocr_page(out[p]) for p in bf.ocr} if bf.ocr else None
        verify_saved(bf, tpl, out, ocr_out)
        assert all(c.ok for c in bf.checks), (bf.name, [c.message for c in bf.checks])
    assert sorted(os.listdir(out_dir)) == ["01. A1234-01, B5678-02.pdf", "02. X9999-99.pdf", "03. A1234-01.pdf"]
    assert os.path.exists(files[0])                                      # 원본은 그대로

    import pymupdf
    text2 = read_region(pymupdf.open(str(out_dir / "02. X9999-99.pdf"))[0], TABLE).text
    assert "X9999-99" in text2 and "E0000-01" not in text2

    report = tmp_path / "일괄처리_결과.csv"
    write_batch_report(str(report), batch, bulk)
    rows = list(csv.reader(open(report, encoding="utf-8-sig")))
    assert any(r[3] == "Z1111-11" and r[7] == "어느 파일에도 없음" for r in rows)
    assert any(r[0] == "1" and r[3] == "B5678-02" and "가격" in r[7] for r in rows)
    assert all(r[8] == "OK" for r in rows[1:] if r[0] != "-")


def test_broken_file_is_reported(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf")
    bf = BatchFile(str(p), 1)
    open_file(bf)
    assert bf.error and bf.doc is None
