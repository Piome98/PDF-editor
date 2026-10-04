import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def main() -> int:
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
