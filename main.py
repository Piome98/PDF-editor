import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def ocr_selftest(pdf_path: str, report_path: str) -> int:
    """exe 안에 OCR 모델이 제대로 들어갔는지 확인한다 (화면 없이 실행, 결과를 파일로 남김).

    PdfPriceEditor.exe --ocr-selftest 입력.pdf 결과.txt
    """
    import time
    import traceback

    try:
        import pymupdf
        from core.ocr import models_available, ocr_page
        t = time.time()
        words = ocr_page(pymupdf.open(pdf_path)[0])
        lines = [f"models_available={models_available()}", f"words={len(words)}",
                 f"seconds={time.time() - t:.1f}"] + [w.text for w in words]
        code = 0 if words else 1
    except Exception:  # noqa: BLE001
        lines, code = ["ERROR", traceback.format_exc()], 2
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return code


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--ocr-selftest":
        return ocr_selftest(sys.argv[2], sys.argv[3])
    app = QApplication(sys.argv)
    app.setApplicationName("PDF 가격 수정기")
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)   # 문서 작업용이라 항상 밝은 화면
    win = MainWindow()
    win.show()
    for path in sys.argv[1:]:          # exe에 PDF/템플릿을 끌어다 놓아 실행한 경우
        win.on_files_dropped([path])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
