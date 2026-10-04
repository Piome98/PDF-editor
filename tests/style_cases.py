"""글꼴·크기·색이 다른 문서를 만들어, 숫자를 바꾼 결과가 '처음부터 그 숫자로 만든 문서'와 같은지 비교하는 도구."""
from __future__ import annotations

import numpy as np
import pymupdf

from core.fonts import face_bytes, find_face

# (설명, 글꼴 이름, 굵게, 크기 pt, 색)
CASES = [
    ("바탕 11pt 빨강", "Batang", False, 11.0, (0.80, 0.10, 0.10)),
    ("굴림 9pt 파랑", "Gulim", False, 9.0, (0.10, 0.20, 0.60)),
    ("돋움 10pt 초록", "Dotum", False, 10.0, (0.05, 0.45, 0.20)),
    ("Arial Bold 12pt 검정", "Arial", True, 12.0, (0.0, 0.0, 0.0)),
    ("맑은 고딕 10pt 회색", "Malgun Gothic", False, 10.0, (0.35, 0.35, 0.35)),
]
OLD, NEW = "1,234,000", "987,600"
LABEL_AT, NUM_RIGHT, BASE = 60, 300, 100
REGION = [200, 80, 305, 110]


def make_doc(font: str, bold: bool, size: float, color, number: str) -> pymupdf.Document:
    face = find_face(font, bold)
    buf = face_bytes(face)
    f = pymupdf.Font(fontbuffer=buf)
    doc = pymupdf.open()
    page = doc.new_page(width=360, height=150)
    page.insert_font(fontname="F", fontbuffer=buf)
    page.insert_text((LABEL_AT, BASE), "금액", fontname="F", fontsize=size, color=color)
    page.insert_text((NUM_RIGHT - f.text_length(number, fontsize=size), BASE), number,
                     fontname="F", fontsize=size, color=color)
    page.draw_rect(pymupdf.Rect(40, 70, 320, 120), color=(0.5, 0.5, 0.5), width=0.5)
    return pymupdf.open("pdf", doc.tobytes())


def to_image(doc: pymupdf.Document, dpi: int) -> pymupdf.Document:
    out = pymupdf.open()
    for p in doc:
        page = out.new_page(width=p.rect.width, height=p.rect.height)
        page.insert_image(page.rect, pixmap=p.get_pixmap(dpi=dpi))
    return pymupdf.open("pdf", out.tobytes())


def render(doc: pymupdf.Document, clip, dpi: int = 300) -> np.ndarray:
    pix = doc[0].get_pixmap(dpi=dpi, clip=pymupdf.Rect(clip), alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).astype(np.float32)


def _blur(img: np.ndarray) -> np.ndarray:
    """0.1pt 정도의 미세한 위치 차이(사람 눈에 안 보임)가 큰 차이로 잡히지 않도록 살짝 흐리게."""
    p = np.pad(img, ((2, 2), (2, 2), (0, 0)), mode="edge")
    return sum(p[dy:dy + img.shape[0], dx:dx + img.shape[1]] for dy in range(5) for dx in range(5)) / 25


def difference(a: pymupdf.Document, b: pymupdf.Document, clip=REGION) -> float:
    """두 문서의 영역 그림 차이 (글자 주변 픽셀의 평균 색 차이, 0~255). 10 이하면 눈으로 구분이 어렵다."""
    x, y = _blur(render(a, clip)), _blur(render(b, clip))
    ink = (x.mean(axis=2) < 200) | (y.mean(axis=2) < 200)
    return float(np.abs(x - y)[ink].mean()) if ink.any() else 0.0
