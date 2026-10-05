"""같은 양식의 PDF 여러 개에 템플릿·엑셀 대조를 한꺼번에 적용하기."""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field

import pymupdf

from .bulk import BulkList, norm_key
from .engine import (BulkRow, Check, RegionResult, apply, bulk_report, check_template, compute, list_summary,
                     verify)
from .models import LIST, Template
from .ocr import needs_ocr, ocr_page

MAX_NAME = 120                     # 파일 이름(확장자 제외) 최대 길이 — Windows 경로 길이 제한 대비
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


@dataclass
class BatchFile:
    path: str
    number: int                                   # 1부터, 저장할 파일 이름 앞의 번호
    doc: pymupdf.Document | None = None
    ocr: dict = field(default_factory=dict)       # 쪽 번호 → OCR 단어
    ocr_pages: list[int] = field(default_factory=list)   # OCR이 필요한 쪽
    results: dict[str, RegionResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    keys: list[str] = field(default_factory=list)        # 결과 파일에 남는 엑셀 품번 (문서 순서)
    report: list[BulkRow] = field(default_factory=list)  # 이 파일의 엑셀 대조 결과 (이 파일에서 찾은 것 + 엑셀에 없던 것)
    out_name: str = ""
    out_path: str = ""
    checks: list[Check] = field(default_factory=list)
    processed: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def summary(self) -> dict[str, int]:
        total = {"유지": 0, "삭제": 0, "가격수정": 0, "확인": 0}
        for res in self.results.values():
            if res.rows:
                for k, v in list_summary(res).items():
                    total[k] += v
        total["오류"] = sum(1 for res in self.results.values() if res.error)
        return total


def open_file(bf: BatchFile) -> None:
    try:
        doc = pymupdf.open(bf.path)
        if not doc.is_pdf:
            raise ValueError("PDF 파일이 아닙니다")
        if doc.needs_pass:
            raise ValueError("암호가 걸린 PDF입니다")
    except Exception as e:  # noqa: BLE001
        bf.error = f"열 수 없음: {e}"
        return
    bf.doc = doc
    bf.ocr_pages = [i for i in range(len(doc)) if needs_ocr(doc[i])]


def process(bf: BatchFile, tpl: Template, bulk: BulkList | None, inputs: dict[str, str] | None = None,
            total: int = 1) -> None:
    """(OCR이 끝난 뒤) 템플릿·엑셀을 적용해 결과를 계산하고 저장할 이름을 정한다."""
    if bf.doc is None:
        return
    bf.warnings = check_template(bf.doc, tpl, bf.ocr)
    bf.results = compute(bf.doc, tpl, inputs or {}, bf.ocr, bulk)
    bf.keys = kept_keys(tpl, bf.results)
    if bulk:
        bf.report = [row for row in bulk_report(tpl, bf.results, bulk) if row.level != "err"]
    bf.out_name = output_name(bf.number, total, bf.keys, os.path.splitext(bf.name)[0])
    bf.processed = True


def ocr_sync(bf: BatchFile) -> None:
    """화면 없이 쓸 때(테스트·명령줄): OCR이 필요한 쪽을 바로 읽는다."""
    if bf.doc is not None:
        for p in bf.ocr_pages:
            bf.ocr[p] = ocr_page(bf.doc[p])


def kept_keys(tpl: Template, results: dict[str, RegionResult]) -> list[str]:
    """결과 파일에 남는 품번 (엑셀에 있는 품번 줄). 문서에 나온 순서대로, 중복 없이."""
    keys: list[str] = []
    seen: set[str] = set()
    for r in tpl.regions:
        res = results.get(r.id)
        if r.kind != LIST or res is None or res.error:
            continue
        for row in res.rows:
            if row.item is not None and row.status in ("matched", "similar"):
                k = norm_key(row.item.key)
                if k not in seen:
                    seen.add(k)
                    keys.append(row.item.key)
    return keys


def safe_name(text: str) -> str:
    text = _BAD_CHARS.sub("_", text).strip().rstrip(".")
    return re.sub(r"\s+", " ", text)


def output_name(number: int, total: int, keys: list[str], fallback: str) -> str:
    """'01. A1234-01, B5678-02.pdf' — 품번이 많아 이름이 너무 길면 '... 외 N개'."""
    prefix = f"{number:0{max(2, len(str(total)))}d}. "
    if not keys:
        return prefix + safe_name(fallback) + ".pdf"
    parts: list[str] = []
    for i, key in enumerate(keys):
        candidate = ", ".join(parts + [safe_name(key)])
        rest = len(keys) - i - 1
        tail = f" 외 {rest}개" if rest else ""
        if parts and len(prefix + candidate + tail) > MAX_NAME:
            return prefix + ", ".join(parts) + f" 외 {len(keys) - len(parts)}개.pdf"
        parts.append(safe_name(key))
    return prefix + ", ".join(parts) + ".pdf"


def unique_path(folder: str, name: str, taken: set[str]) -> str:
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.normcase(path) in taken or os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    taken.add(os.path.normcase(path))
    return path


def save(bf: BatchFile, tpl: Template, folder: str, taken: set[str]) -> pymupdf.Document | None:
    """수정한 결과를 새 파일로 저장한다 (원본은 그대로). 바꿀 것이 없는 파일도 같은 이름 규칙으로 저장한다."""
    if bf.doc is None or not bf.processed:
        return None
    out = apply(bf.doc, tpl, bf.results, bf.ocr)
    bf.out_path = unique_path(folder, bf.out_name, taken)
    out.save(bf.out_path, garbage=3, deflate=True)
    return out


def verify_saved(bf: BatchFile, tpl: Template, out: pymupdf.Document, ocr_out: dict | None) -> None:
    bf.checks = verify(out, tpl, bf.results, ocr_out)


def missing_everywhere(files: list[BatchFile], bulk: BulkList | None) -> list[str]:
    """엑셀 품번 중 어느 파일에서도 찾지 못한 것."""
    if not bulk:
        return []
    found = {norm_key(row.key) for bf in files for row in bf.report if row.key}
    return [item.key for k, item in bulk.items.items() if k not in found]


def write_batch_report(path: str, files: list[BatchFile], bulk: BulkList | None) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["번호", "원본 파일", "저장 파일", "엑셀 품번", "엑셀 가격", "PDF 품번", "PDF 가격", "결과", "검증"])
        for bf in files:
            checked = ("오류: " + bf.error if bf.error else
                       "" if not bf.checks else
                       "OK" if all(c.ok for c in bf.checks) else
                       "확인 필요: " + "; ".join(c.message for c in bf.checks if not c.ok))
            saved = os.path.basename(bf.out_path) if bf.out_path else ""
            if not bf.report:
                w.writerow([bf.number, bf.name, saved, "", "", "", "", bf.error or "엑셀 대조 없음", checked])
            for row in bf.report:
                w.writerow([bf.number, bf.name, saved, row.key, row.excel_price, row.pdf_key, row.pdf_price,
                            row.status, checked])
        for key in missing_everywhere(files, bulk):
            item = bulk.items[norm_key(key)]
            price = str(item.price) if item.price is not None else ""
            w.writerow(["-", "", "", key, price, "", "", "어느 파일에도 없음", ""])
