"""PDF 텍스트 레이어와 OCR이 공통으로 쓰는 '단어' 자료형."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

import pymupdf


@dataclass
class Word:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    color: tuple[float, float, float]
    baseline: float
    line: int = 0                                    # 영역 안에서 몇 번째 줄인지 (위에서부터 0)
    bg: tuple[float, float, float] | None = None     # OCR 단어: 글자 뒤 배경색 (지울 때 이 색으로 채움)
    score: float = 1.0                               # OCR 인식 신뢰도 (텍스트 레이어는 1.0)
    ocr: bool = False
    # 텍스트 레이어 단어: PDF에 적힌 글꼴 이름(예: ArialMT)과 굵기/기울임
    font: str = ""
    bold: bool = False
    italic: bool = False
    invisible: bool = False                          # 화면에 안 보이는 글자 (스캐너 OCR이 이미지 위에 깔아 둔 글자 등)
    # OCR 단어: 글꼴·색 추정용 원본 글자 그림 (잉크 마스크, 컬러 픽셀)과 해상도
    patch: np.ndarray | None = field(default=None, repr=False, compare=False)
    patch_rgb: np.ndarray | None = field(default=None, repr=False, compare=False)
    px_scale: float = 0.0                            # OCR 그림의 1pt당 픽셀 수
    patch_xy: tuple[float, float] = (0.0, 0.0)       # patch_rgb 왼쪽 위 모서리의 PDF 좌표
    src_grid: tuple[float, float] = (0.0, 0.0)       # 원본 이미지의 왼쪽 위 PDF 좌표 (픽셀 격자 맞춤용)
    src_dpi: float = 0.0                             # 원본 이미지의 해상도


def order_words(words: list[Word]) -> list[Word]:
    """화면에 보이는 위치 기준으로 줄을 나누고 위→아래, 왼→오른쪽 순서로 정렬한다."""
    words = sorted(words, key=lambda w: (w.bbox[1] + w.bbox[3]) / 2)
    line_no, line_y = -1, None
    for w in words:
        yc, h = (w.bbox[1] + w.bbox[3]) / 2, w.bbox[3] - w.bbox[1]
        if line_y is None or abs(yc - line_y) > max(h, 1) * 0.5:
            line_no += 1
            line_y = yc
        w.line = line_no
    return sorted(words, key=lambda w: (w.line, w.bbox[0]))


def words_in_rect(words: list[Word], rect) -> list[Word]:
    """영역 안에 중심이 들어오는 단어만 골라 (복사본으로) 정렬해 돌려준다."""
    r = pymupdf.Rect(rect)
    inside = [replace(w) for w in words
              if r.contains(pymupdf.Point((w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2))]
    return order_words(inside)
