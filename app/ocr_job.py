"""여러 쪽을 차례로 OCR하는 작업. 화면이 멈추지 않도록 인식은 백그라운드 스레드에서 한다.

페이지 그림 만들기(PyMuPDF)는 메인 스레드에서, 글자 인식(onnxruntime)만 백그라운드에서 한다.
"""
from __future__ import annotations

import threading

import pymupdf
from PySide6.QtCore import QObject, Signal

from core.ocr import ocr_image, render_for_ocr


class OcrJob(QObject):
    pageDone = Signal(int, list)      # 쪽 번호, 단어 목록
    progress = Signal(int, int)       # 끝난 쪽 수, 전체 쪽 수
    finished = Signal(bool)           # True = 끝까지 완료, False = 취소/실패
    failed = Signal(str)
    _workerDone = Signal(int, list)   # 백그라운드 → 메인 스레드
    _workerFailed = Signal(str)

    def __init__(self, doc: pymupdf.Document, pages: list[int], parent=None):
        super().__init__(parent)
        self.doc, self.pages = doc, list(pages)
        self.results: dict[int, list] = {}
        self._index = 0
        self._cancelled = False
        self._workerDone.connect(self._on_page_done)
        self._workerFailed.connect(self._on_failed)

    def start(self) -> None:
        self.progress.emit(0, len(self.pages))
        self._next()

    def cancel(self) -> None:
        self._cancelled = True

    def _next(self) -> None:
        if self._cancelled:
            self.finished.emit(False)
            return
        if self._index >= len(self.pages):
            self.finished.emit(True)
            return
        page_no = self.pages[self._index]
        try:
            img = render_for_ocr(self.doc[page_no])
        except Exception as e:  # noqa: BLE001
            self._on_failed(f"{page_no + 1}쪽을 그리지 못했습니다: {e}")
            return
        threading.Thread(target=self._work, args=(page_no, img), daemon=True).start()

    def _work(self, page_no: int, img) -> None:
        try:
            self._workerDone.emit(page_no, ocr_image(img))
        except Exception as e:  # noqa: BLE001
            self._workerFailed.emit(f"{page_no + 1}쪽 글자 인식 실패: {e}")

    def _on_page_done(self, page_no: int, words: list) -> None:
        if self._cancelled:
            self.finished.emit(False)
            return
        self.results[page_no] = words
        self.pageDone.emit(page_no, words)
        self._index += 1
        self.progress.emit(self._index, len(self.pages))
        self._next()

    def _on_failed(self, message: str) -> None:
        self.failed.emit(message)
        self.finished.emit(False)
