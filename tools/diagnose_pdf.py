"""PDF에서 글자를 왜 못 읽는지 진단한다.

사용법:  .venv\\Scripts\\python tools\\diagnose_pdf.py <파일.pdf> [--sample]
  --sample  각 쪽에서 읽은 글자 앞부분을 함께 출력 (개인정보가 보일 수 있음)
"""
from __future__ import annotations

import sys
import unicodedata

import pymupdf


def classify_chars(text: str) -> dict[str, int]:
    counts = {"total": 0, "hangul": 0, "ascii": 0, "replacement": 0, "private_use": 0, "control": 0}
    for ch in text:
        if ch.isspace():
            continue
        counts["total"] += 1
        o = ord(ch)
        if ch == "�":
            counts["replacement"] += 1
        elif 0xE000 <= o <= 0xF8FF:
            counts["private_use"] += 1
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            counts["control"] += 1
        elif 0xAC00 <= o <= 0xD7A3 or 0x3131 <= o <= 0x318E:
            counts["hangul"] += 1
        elif o < 128:
            counts["ascii"] += 1
    return counts


def font_report(doc: pymupdf.Document, page: pymupdf.Page) -> list[str]:
    lines = []
    for xref, ext, ftype, basefont, _name, encoding, *_ in page.get_fonts(full=True):
        to_unicode = doc.xref_get_key(xref, "ToUnicode")[0] != "null"
        flag = "" if to_unicode else "   ← ToUnicode 없음 (글자가 깨지거나 빈칸으로 읽힐 수 있음)"
        if ftype == "Type3":
            flag = "   ← Type3 글꼴 (글자 모양을 직접 그린 글꼴, 읽기 어려움)"
        lines.append(f"    - {basefont or '(이름 없음)'} | {ftype} | 인코딩 {encoding or '-'} | "
                     f"ToUnicode {'있음' if to_unicode else '없음'}{flag}")
    return lines


def image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect)
    covered = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        covered += abs(r)
    return min(covered / area, 1.0) if area else 0.0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_sample = "--sample" in sys.argv
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    doc = pymupdf.open(path)
    meta = doc.metadata or {}
    print(f"파일: {path}")
    print(f"쪽수: {len(doc)}   암호: {'있음' if doc.needs_pass else '없음'}")
    print(f"만든 프로그램(Creator): {meta.get('creator') or '-'}")
    print(f"PDF 변환기(Producer): {meta.get('producer') or '-'}")
    print()

    verdicts: set[str] = set()
    for page in doc:
        text = page.get_text("text")
        c = classify_chars(text)
        garbled = c["replacement"] + c["private_use"] + c["control"]
        cov = image_coverage(page)
        drawings = page.get_drawings()
        filled_paths = sum(1 for d in drawings if d.get("fill") is not None)
        widgets = list(page.widgets())
        annots = [a for a in page.annots() if a.type[1] in ("FreeText", "Stamp", "Text")]

        print(f"── {page.number + 1}쪽  (크기 {page.rect.width:.0f}×{page.rect.height:.0f}, 회전 {page.rotation}°)")
        print(f"  읽힌 글자 수: {c['total']}  (한글 {c['hangul']}, 영문/숫자 {c['ascii']}, "
              f"깨진 글자 {garbled})")
        print(f"  이미지가 덮은 면적: {cov:.0%}   채워진 도형 수: {filled_paths}   전체 그리기 명령: {len(drawings)}")
        print(f"  양식 필드: {len(widgets)}개   글자 주석: {len(annots)}개")
        fonts = font_report(doc, page)
        print(f"  글꼴 {len(fonts)}개:")
        for line in fonts:
            print(line)
        if widgets:
            filled = [w for w in widgets if (w.field_value or "").strip()]
            print(f"  값이 들어 있는 양식 필드: {len(filled)}개")
        if show_sample and text.strip():
            sample = " ".join(text.split())[:150]
            print(f"  읽은 글자 앞부분: {sample!r}")

        if c["total"] < 20 and cov > 0.7:
            verdicts.add("SCAN")
        elif c["total"] < 20 and filled_paths > 200:
            verdicts.add("OUTLINED")
        elif c["total"] and garbled / c["total"] > 0.2:
            verdicts.add("GARBLED")
        if widgets:
            verdicts.add("WIDGETS")
        if annots:
            verdicts.add("ANNOTS")
        if page.rotation:
            verdicts.add("ROTATED")
        print()

    messages = {
        "SCAN": "스캔본(이미지) PDF입니다. 텍스트 레이어가 없어 OCR이 필요합니다.",
        "OUTLINED": "글자가 도형(선)으로 그려진 PDF로 보입니다. 텍스트 레이어가 없어 OCR이 필요합니다.",
        "GARBLED": "글자는 있지만 문자 대응표(ToUnicode)가 없거나 잘못되어 깨져서 읽힙니다.",
        "WIDGETS": "양식 필드(입력 칸)가 있습니다. 필드에 들어간 값은 본문 글자와 따로 처리해야 합니다.",
        "ANNOTS": "주석으로 얹힌 글자가 있습니다. 본문 글자와 따로 처리해야 합니다.",
        "ROTATED": "회전된 쪽이 있습니다. 좌표 변환이 필요합니다.",
    }
    print("══ 진단 결과")
    if not verdicts:
        print("  텍스트 레이어가 정상입니다. 영역 좌표나 단어 분리 쪽 문제일 가능성이 큽니다.")
    for v in ("SCAN", "OUTLINED", "GARBLED", "WIDGETS", "ANNOTS", "ROTATED"):
        if v in verdicts:
            print(f"  • {messages[v]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
