"""이미지 PDF(스캔본, PDF 프린터로 저장한 문서)의 글자를 오프라인 OCR로 읽는다.

- 인식: RapidOCR + PaddleOCR 한국어 모델 (onnxruntime, 인터넷 불필요)
- 단어 나누기: OCR이 띄어쓰기를 빠뜨리는 경우가 있어서, 실제 이미지에서 글자 사이 빈 간격을 재서 나눈다.
- 단어마다 잉크에 딱 맞는 사각형, 글자 크기·기준선·글자색·배경색을 추정해 둔다.
  (지울 때 배경색으로 채우고, 새 숫자를 원래 크기·색으로 쓰기 위해)
"""
from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

from .words import Word

MODEL_FILES = {
    "det": "PP-OCRv6_det_small.onnx",
    "cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "rec": "korean_PP-OCRv5_rec_mobile.onnx",
}
DPI = 300

# 잉크 높이(기준선 위) ÷ 글자 크기. 맑은 고딕 기준으로 맞춘 값
_HEIGHT_PER_SIZE_DIGIT = 0.74
_HEIGHT_PER_SIZE_HANGUL = 0.93
_HANGUL_BELOW_BASELINE = 0.08     # 한글 받침 획은 기준선보다 조금 내려온다 (글자 크기 대비)

# 글자 사이 빈 간격 ÷ 글자 높이: 이 이상이면 띄어쓰기로 본다.
# (실측: 단어 안 최대 0.34, 실제 띄어쓰기 최소 0.45)  OCR이 띄어쓰기를 찾은 곳은 더 작은 간격도 인정
_SPACE_GAP = 0.40
_SPACE_GAP_WITH_HINT = 0.22

_engine = None
_lock = threading.Lock()


def model_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)   # exe로 실행 중이면 압축이 풀린 임시 폴더
    return Path(base) / "models" if base else Path(__file__).resolve().parent.parent / "models"


def models_available() -> bool:
    return all((model_dir() / f).exists() for f in MODEL_FILES.values())


def needs_ocr(page: pymupdf.Page) -> bool:
    """글자 정보가 거의 없고 이미지가 페이지 대부분을 덮고 있으면 OCR이 필요한 페이지로 본다."""
    if len("".join(page.get_text("text").split())) >= 20:
        return False
    area = abs(page.rect)
    covered = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
    return area > 0 and covered / area > 0.5


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        d = model_dir()
        _engine = RapidOCR(params={
            "Global.log_level": "error",
            "Global.max_side_len": 4000,       # 작은 글자(9pt 이하)를 놓치지 않도록 크게 유지
            "Global.use_cls": False,           # 문서는 똑바로 놓여 있다고 가정 (속도)
            "Global.model_root_dir": str(d),   # 모델을 내려받으러 인터넷에 접속하지 않도록 고정
            "Det.model_path": str(d / MODEL_FILES["det"]),
            "Cls.model_path": str(d / MODEL_FILES["cls"]),
            "Rec.model_path": str(d / MODEL_FILES["rec"]),
            "Rec.lang_type": LangRec.KOREAN,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
        })
    return _engine


def _normalize(text: str) -> str:
    # 원화 기호(₩)를 "#"이나 "W"로 읽는 경우가 많다 (바로 뒤에 숫자가 올 때만 고침)
    return re.sub(r"^[#W](?=[\d(])", "₩", text)


@dataclass
class PageImage:
    """OCR할 페이지 그림. PyMuPDF는 여러 스레드에서 쓰면 안 되므로 그리기는 메인 스레드에서 한다."""
    rgb: np.ndarray
    scale: float
    to_page: pymupdf.Matrix     # 그림 픽셀 좌표 → PDF 좌표


def render_for_ocr(page: pymupdf.Page, dpi: int = DPI) -> PageImage:
    scale = dpi / 72
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    return PageImage(rgb, scale, pymupdf.Matrix(1 / scale, 1 / scale) * page.derotation_matrix)


def ocr_image(img: PageImage) -> list[Word]:
    """그려 둔 페이지 그림에서 단어를 읽는다. 백그라운드 스레드에서 불러도 된다."""
    import cv2

    gray = cv2.cvtColor(img.rgb, cv2.COLOR_RGB2GRAY)
    # 흑백으로 바꾸면 색 배경 위 글자와 작은 글자의 인식률이 올라간다
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    with _lock:
        res = _get_engine()(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR),
                            return_word_box=True, return_single_char_box=True)
    if res.boxes is None or res.txts is None:
        return []
    ink_all = binary == 0
    words: list[Word] = []
    for box, txt, score, chars in zip(res.boxes, res.txts, res.scores, res.word_results):
        words += _split_line(np.asarray(box), txt, float(score), chars, img.rgb, ink_all, img.scale, img.to_page)
    return words


def ocr_page(page: pymupdf.Page, dpi: int = DPI) -> list[Word]:
    return ocr_image(render_for_ocr(page, dpi))


def _ink_color(pixels: np.ndarray) -> tuple[float, float, float]:
    """글자색. 가장자리의 옅은 번짐을 빼고, 가장 진한 15% 픽셀의 평균을 쓴다."""
    if len(pixels) == 0:
        return (0.0, 0.0, 0.0)
    lum = pixels.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    dark = pixels[lum <= np.percentile(lum, 15)]
    return tuple(float(c) / 255 for c in dark.mean(axis=0))


def _space_hints(txt: str, chars) -> list[bool] | None:
    """OCR이 읽은 줄 텍스트에서, 각 글자 앞에 띄어쓰기가 있었는지. 글자 수가 안 맞으면 None."""
    flags, prev_space = [], False
    for c in txt:
        if c.isspace():
            prev_space = True
            continue
        flags.append(prev_space)
        prev_space = False
    return flags if len(flags) == len(chars) else None


def _split_line(box, txt, score, chars, rgb, ink_all, scale, to_page) -> list[Word]:
    h, w = ink_all.shape
    x0, x1 = max(int(box[:, 0].min()) - 2, 0), min(int(box[:, 0].max()) + 3, w)
    y0, y1 = max(int(box[:, 1].min()) - 2, 0), min(int(box[:, 1].max()) + 3, h)
    ink = ink_all[y0:y1, x0:x1].copy()
    if ink.size == 0:
        return []
    # 표 테두리처럼 상자를 가로지르는 선은 글자가 아니므로 뺀다
    ink[ink.mean(axis=1) > 0.85, :] = False
    ink[:, ink.mean(axis=0) > 0.95] = False
    rows = np.flatnonzero(ink.any(axis=1))
    if len(rows) == 0:
        return []
    ink_h = rows[-1] - rows[0] + 1
    hint_gap = max(2, round(ink_h * _SPACE_GAP_WITH_HINT))

    # 1) 작은 간격 기준으로 잘게 나눈 뒤
    pieces: list[list[int]] = []
    last, blank = -1, 0
    for i, has_ink in enumerate(ink.any(axis=0)):
        if has_ink:
            if not pieces or blank >= hint_gap:
                pieces.append([i, i + 1])
            else:
                pieces[-1][1] = i + 1
            last, blank = i, 0
        elif last >= 0:
            blank += 1
    if not pieces:
        return []

    # 2) OCR 글자를 조각에 배정하고
    hints = _space_hints(txt, chars)
    piece_chars: list[list[int]] = [[] for _ in pieces]
    for idx, ch in enumerate(chars):
        cb = np.asarray(ch[2])
        cx = (cb[:, 0].min() + cb[:, 0].max()) / 2 - x0
        dist = [0 if a <= cx < b else min(abs(cx - a), abs(cx - b)) for a, b in pieces]
        piece_chars[int(np.argmin(dist))].append(idx)

    # 3) 간격이 충분히 넓거나, OCR이 그 자리에 띄어쓰기를 읽은 경우에만 단어를 나눈다
    segments: list[list[int]] = []
    seg_idx: list[list[int]] = []
    for (a, b), idxs in zip(pieces, piece_chars):
        if segments:
            gap = a - segments[-1][1]
            hinted = bool(hints and idxs and hints[idxs[0]])
            if gap < ink_h * _SPACE_GAP and not hinted:
                segments[-1][1] = b
                seg_idx[-1] += idxs
                continue
        segments.append([a, b])
        seg_idx.append(list(idxs))
    seg_text = [[chars[i][0] for i in idxs] for idxs in seg_idx]
    seg_score = [[float(chars[i][1]) if len(chars[i]) > 1 and chars[i][1] is not None else score
                  for i in idxs] for idxs in seg_idx]

    words = []
    for (sx0, sx1), texts, scores in zip(segments, seg_text, seg_score):
        if not texts:
            continue            # OCR이 글자로 보지 않은 잉크(얼룩, 선 조각)
        seg = ink[:, sx0:sx1]
        srows = np.flatnonzero(seg.any(axis=1))
        ty0, ty1 = int(srows[0]), int(srows[-1]) + 1
        profile = seg.sum(axis=1)
        base_row = int(np.flatnonzero(profile >= profile.max() * 0.25)[-1]) + 1  # 쉼표 꼬리 등은 무시

        px0, py0, px1, py1 = x0 + sx0, y0 + ty0, x0 + sx1, y0 + ty1
        mask = seg[ty0:ty1]
        patch = rgb[py0:py1, px0:px1]
        color = _ink_color(patch[mask])
        bx0, by0 = max(px0 - 3, 0), max(py0 - 3, 0)
        bx1, by1 = min(px1 + 3, rgb.shape[1]), min(py1 + 3, rgb.shape[0])
        around = rgb[by0:by1, bx0:bx1][~ink_all[by0:by1, bx0:bx1]]
        bg = tuple(float(c) / 255 for c in np.median(around, axis=0)) if len(around) else (1.0, 1.0, 1.0)

        text = _normalize("".join(texts))
        hangul = any("가" <= c <= "힣" for c in text)
        height_pt = (y0 + base_row - py0) / scale
        size = height_pt / (_HEIGHT_PER_SIZE_HANGUL if hangul else _HEIGHT_PER_SIZE_DIGIT)
        rect = pymupdf.Rect(px0, py0, px1, py1) * to_page
        baseline = (pymupdf.Point(px0, y0 + base_row) * to_page).y
        if hangul:
            baseline -= size * _HANGUL_BELOW_BASELINE
        words.append(Word(text, tuple(rect), round(size, 1), color, baseline,
                          bg=bg, score=round(min(scores), 2), ocr=True))
    return words
