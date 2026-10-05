from __future__ import annotations

import dataclasses
import os

import pymupdf
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFont, QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QProgressDialog,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QToolBar, QVBoxLayout, QWidget)

from core.bulk import BulkList, read_table
from core.engine import (RegionResult, apply, bulk_report, check_template, compute, list_summary, number_target,
                         page_sizes, read_region, region_style, word_style, write_bulk_report,
                         verify, write_log)
from core.fonts import font_choices
from core.models import ERASE, ERASE_ALL, ERASE_PICK, LIST, VALUE, Region, Template
from core.numbers import detect_style
from core.ocr import models_available, needs_ocr

from .bulk_dialogs import BulkReportDialog, ExcelImportDialog
from .ocr_job import OcrJob
from .pdf_view import MODE_ERASE, MODE_LIST, MODE_SELECT, MODE_VALUE, PdfView

APP_TITLE = "PDF 가격 수정기"
COL_PAGE, COL_KIND, COL_NAME, COL_ORIG, COL_INPUT, COL_RESULT, COL_STATUS = range(7)
OK_COLOR, WARN_COLOR, ERR_COLOR = QColor("#1A7F37"), QColor("#9A6700"), QColor("#CF222E")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1400, 880)
        self.doc: pymupdf.Document | None = None
        self.doc_path = ""
        self.template = Template()
        self.template_path = ""
        self.inputs: dict[str, str] = {}             # 영역 id → 이 문서에서 입력한 새 값
        self.results: dict[str, RegionResult] = {}
        self.preview_doc: pymupdf.Document | None = None
        self.ocr: dict[int, list] = {}               # 쪽 번호 → OCR로 읽은 단어 (이미지 PDF)
        self._ocr_job: OcrJob | None = None
        self.bulk: BulkList | None = None              # 엑셀 품번 목록 (작업마다 다름, 템플릿에는 저장 안 함)
        self.page_no = 0
        self.last_dir = os.path.expanduser("~")
        self._syncing = False

        self.view = PdfView()
        self._build_toolbar()
        self._build_side_panel()
        splitter = QSplitter()
        splitter.addWidget(self.view)
        splitter.addWidget(self.side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 580])
        self.setCentralWidget(splitter)

        self.view.regionDrawn.connect(self.on_region_drawn)
        self.view.regionEdited.connect(self.on_region_moved)
        self.view.selectionIds.connect(self.on_view_selection)
        self.view.deleteRequested.connect(self.delete_selected)
        self.view.zoomChanged.connect(lambda _: self.render_page())
        self.view.filesDropped.connect(self.on_files_dropped)
        self.view.tokenClicked.connect(self.on_token_clicked)
        self.update_title()
        self.set_mode(MODE_SELECT)
        self.statusBar().showMessage("PDF를 열거나 창에 끌어다 놓으세요. 템플릿(.json)도 끌어다 놓을 수 있습니다.")

    # ───────────────────────── UI 구성 ─────────────────────────
    def _build_toolbar(self) -> None:
        tb = QToolBar("도구")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None, tip=None, checkable=False):
            a = QAction(text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(f"{tip or text}" + (f"  ({shortcut})" if shortcut else ""))
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        act("PDF 열기", self.open_pdf_dialog, "Ctrl+O")
        tb.addSeparator()
        act("새 템플릿", self.new_template, tip="영역을 모두 지우고 새로 시작")
        act("템플릿 열기", self.open_template_dialog, "Ctrl+Shift+O")
        act("템플릿 저장", self.save_template, "Ctrl+Shift+S", tip="영역 설정을 템플릿(.json)으로 저장")
        tb.addSeparator()

        self.mode_group = QActionGroup(self)
        self.mode_actions = {}
        for mode, text, key, tip in [
            (MODE_SELECT, "선택/이동", "V", "영역을 선택하고 옮기거나, 오른쪽 아래 모서리를 끌어 크기 조절"),
            (MODE_VALUE, "＋ 숫자 영역", "N", "드래그해서 새 숫자로 바꿀 영역 지정"),
            (MODE_ERASE, "＋ 지울 영역", "E", "드래그해서 지울 영역 지정"),
            (MODE_LIST, "＋ 품번 대조 영역", "L", "드래그해서 엑셀 품번과 대조할 표(목록) 영역 지정"),
        ]:
            a = act(text, lambda _=False, m=mode: self.set_mode(m), key, tip, checkable=True)
            self.mode_group.addAction(a)
            self.mode_actions[mode] = a
        tb.addSeparator()

        act("◀", lambda: self.goto_page(self.page_no - 1), "PgUp", "이전 쪽")
        self.page_label = QLabel(" - / - ")
        tb.addWidget(self.page_label)
        act("▶", lambda: self.goto_page(self.page_no + 1), "PgDown", "다음 쪽")
        tb.addSeparator()
        act("－", lambda: self.view.set_zoom(self.view.zoom / 1.2), "Ctrl+-", "축소")
        act("＋", lambda: self.view.set_zoom(self.view.zoom * 1.2), "Ctrl+=", "확대")
        act("폭 맞춤", self.view.fit_width, "Ctrl+0")
        tb.addSeparator()
        self.preview_action = act("결과 미리보기", self.toggle_preview, "F5",
                                  "수정 결과를 미리 보기 (다시 누르면 원본)", checkable=True)
        act("결과 PDF 저장", self.export_pdf, "Ctrl+S")
        tb.addSeparator()
        act("엑셀 품번 불러오기", self.open_excel_dialog, "Ctrl+E",
            "품번·가격이 적힌 엑셀(.xlsx/.csv)을 불러와 '품번 대조 영역'과 비교")
        act("대조 결과", self.show_bulk_report, "Ctrl+R", "엑셀 품번 하나하나가 PDF에 있는지, 가격이 맞는지")
        tb.addSeparator()
        act("글자 인식(OCR)", self.run_ocr_all, tip="모든 쪽을 OCR로 다시 읽기 "
            "(글자가 깨져 읽히거나, 일부만 이미지인 PDF에 사용)")

    def _build_side_panel(self) -> None:
        self.side = QWidget()
        lay = QVBoxLayout(self.side)
        lay.setContentsMargins(6, 6, 6, 6)

        lay.addWidget(QLabel("<b>영역 목록</b>  — '새 값' 칸에 입력하세요. 비워두면 원본 값을 그대로 씁니다."))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["쪽", "종류", "이름", "원본", "새 값", "결과", "상태"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        h = self.table.horizontalHeader()
        for c, mode in [(COL_PAGE, QHeaderView.ResizeToContents), (COL_KIND, QHeaderView.ResizeToContents),
                        (COL_NAME, QHeaderView.Interactive), (COL_ORIG, QHeaderView.Stretch),
                        (COL_INPUT, QHeaderView.Interactive), (COL_RESULT, QHeaderView.Stretch),
                        (COL_STATUS, QHeaderView.Interactive)]:
            h.setSectionResizeMode(c, mode)
        self.table.setColumnWidth(COL_NAME, 80)
        self.table.setColumnWidth(COL_INPUT, 90)
        self.table.setColumnWidth(COL_STATUS, 110)
        self.table.itemChanged.connect(self.on_table_edited)
        self.table.itemSelectionChanged.connect(self.on_table_selection)
        lay.addWidget(self.table, 3)

        # 선택한 영역 설정
        self.props = QGroupBox("선택한 영역 설정 (템플릿에 저장됨)")
        form = QFormLayout(self.props)
        self.p_name = QLineEdit()
        self.p_kind = QComboBox()
        self.p_kind.addItem("숫자 변경", VALUE)
        self.p_kind.addItem("지우기", ERASE)
        self.p_kind.addItem("품번 대조 (엑셀)", LIST)
        self.p_list_unmatched = QComboBox()
        self.p_list_unmatched.addItem("엑셀에 없는 품번 줄은 지우기", "erase")
        self.p_list_unmatched.addItem("표시만 하고 지우지 않기", "keep")
        self.p_list_price = QComboBox()
        self.p_list_price.addItem("가격이 다르면 엑셀 가격으로 바꾸기", "replace")
        self.p_list_price.addItem("가격이 달라도 표시만 하기", "check")
        self.p_erase_mode = QComboBox()
        self.p_erase_mode.addItem("글자 골라 지우기 (남길 글자 선택)", ERASE_PICK)
        self.p_erase_mode.addItem("영역 전체 덮기 (도장·이미지 포함)", ERASE_ALL)
        self.p_unknown = QComboBox()
        self.p_unknown.addItem("처음 보는 글자는 지움", "erase")
        self.p_unknown.addItem("처음 보는 글자는 남김", "keep")
        self.p_unknown.setToolTip("템플릿을 다른 PDF에 쓸 때, 아래 목록에서 정하지 않은 글자"
                                  "(예: 다른 담당자 이름)를 어떻게 할지 정합니다.")
        self.p_number_only = QCheckBox("숫자가 든 단어만 교체 (라벨·단위 글자는 유지)")
        self.p_formula = QLineEdit()
        self.p_formula.setPlaceholderText("비우면 직접 입력.  예) 단가1*수량1   SUM(금액*)   ROUND(공급가액*0.1)")
        self.p_prefix, self.p_suffix = QLineEdit(), QLineEdit()
        self.p_prefix.setPlaceholderText("앞 (예: ₩)")
        self.p_suffix.setPlaceholderText("뒤 (예: 원)")
        self.p_decimals = QSpinBox()
        self.p_decimals.setRange(0, 6)
        self.p_thousands = QCheckBox("천 단위 쉼표")
        self.p_align = QComboBox()
        for text, val in [("오른쪽", "right"), ("가운데", "center"), ("왼쪽", "left")]:
            self.p_align.addItem(text, val)
        self.p_size = QDoubleSpinBox()
        self.p_size.setRange(0, 72)
        self.p_size.setDecimals(1)
        self.p_size.setSpecialValueText("자동 (원본과 같게)")
        # 글꼴·글자색 (비우면 원본에서 자동 감지)
        self.p_style_label = QLabel()
        self.p_style_label.setWordWrap(True)
        self.p_style_label.setStyleSheet("color:#57606A")
        self.p_font = QComboBox()
        self.p_font.addItem("자동 (원본과 같게)", "")
        self.p_font.setMaxVisibleItems(20)
        self._fonts_loaded = False
        self.p_color_btn = QPushButton()
        self.p_color_btn.setToolTip("새 글자의 색을 직접 정합니다")
        self.p_color_auto = QPushButton("자동")
        self.p_color_auto.setToolTip("원본 글자색을 그대로 씁니다")
        self.p_fill = QComboBox()
        self.p_fill_btn = QPushButton("색…")
        self.p_fill_btn.setFixedWidth(40)
        self._fill_custom = ""

        fmt_row = QHBoxLayout()
        for w in (self.p_prefix, self.p_suffix):
            fmt_row.addWidget(w)
        fmt_row.addWidget(QLabel("소수"))
        fmt_row.addWidget(self.p_decimals)
        fmt_row.addWidget(self.p_thousands)
        fill_row = QHBoxLayout()
        fill_row.addWidget(self.p_fill, 1)
        fill_row.addWidget(self.p_fill_btn)
        self._formula_label = QLabel("수식")
        self._fmt_label = QLabel("표기")
        self._align_label = QLabel("정렬 / 크기")
        align_row = QHBoxLayout()
        align_row.addWidget(self.p_align)
        align_row.addWidget(self.p_size)
        font_row = QHBoxLayout()
        font_row.addWidget(self.p_font, 1)
        font_row.addWidget(self.p_color_btn)
        font_row.addWidget(self.p_color_auto)

        # 영역 안에서 읽어 낸 텍스트
        self.p_words = QListWidget()
        self.p_words.setMaximumHeight(130)
        self.p_words.itemChanged.connect(self.on_word_toggled)
        self.p_words_label = QLabel()
        self.p_words_label.setWordWrap(True)
        words_btns = QHBoxLayout()
        self.p_all_erase = QPushButton("모두 지움")
        self.p_all_keep = QPushButton("모두 남김")
        self.p_copy = QPushButton("텍스트 복사")
        for b in (self.p_all_erase, self.p_all_keep, self.p_copy):
            words_btns.addWidget(b)
        words_btns.addStretch(1)
        self.p_all_erase.clicked.connect(lambda: self.set_all_words(True))
        self.p_all_keep.clicked.connect(lambda: self.set_all_words(False))
        self.p_copy.clicked.connect(self.copy_region_text)
        words_box = QVBoxLayout()
        words_box.setContentsMargins(0, 0, 0, 0)
        words_box.addWidget(self.p_words_label)
        words_box.addWidget(self.p_words)
        words_box.addLayout(words_btns)
        self._words_widget = QWidget()
        self._words_widget.setLayout(words_box)

        form.addRow("이름", self.p_name)
        form.addRow("종류", self.p_kind)
        form.addRow("지우는 방식", self.p_erase_mode)
        form.addRow("다른 문서에서", self.p_unknown)
        form.addRow("엑셀에 없는 품번", self.p_list_unmatched)
        form.addRow("가격 비교", self.p_list_price)
        form.addRow("", self.p_number_only)
        form.addRow(self._formula_label, self.p_formula)
        self.p_fmt_row_widget, self.p_align_row_widget, self.p_font_row_widget = QWidget(), QWidget(), QWidget()
        for w, row in ((self.p_fmt_row_widget, fmt_row), (self.p_align_row_widget, align_row),
                       (self.p_font_row_widget, font_row)):
            row.setContentsMargins(0, 0, 0, 0)
            w.setLayout(row)
        form.addRow(self._fmt_label, self.p_fmt_row_widget)
        form.addRow(self._align_label, self.p_align_row_widget)
        form.addRow("원본 서식", self.p_style_label)
        form.addRow("글꼴 / 글자색", self.p_font_row_widget)
        form.addRow("배경 처리", fill_row)
        form.addRow("영역 안 글자", self._words_widget)
        self.form = form
        lay.addWidget(self.props)

        for w in (self.p_name, self.p_formula, self.p_prefix, self.p_suffix):
            w.editingFinished.connect(self.on_props_edited)
        for w in (self.p_kind, self.p_align, self.p_fill, self.p_erase_mode, self.p_unknown,
                  self.p_list_unmatched, self.p_list_price):
            w.currentIndexChanged.connect(self.on_props_edited)
        self.p_number_only.toggled.connect(self.on_props_edited)
        self.p_decimals.valueChanged.connect(self.on_props_edited)
        self.p_size.valueChanged.connect(self.on_props_edited)
        self.p_thousands.toggled.connect(self.on_props_edited)
        self.p_fill_btn.clicked.connect(self.pick_fill_color)
        self.p_font.currentIndexChanged.connect(self.on_props_edited)
        self.p_color_btn.clicked.connect(self.pick_text_color)
        self.p_color_auto.clicked.connect(self.reset_text_color)
        self.props.setEnabled(False)

        lay.addWidget(QLabel("<b>확인 메시지</b>"))
        self.messages = QListWidget()
        self.messages.setWordWrap(True)
        lay.addWidget(self.messages, 1)

    def _set_fill_options(self, current: str) -> None:
        self.p_fill.blockSignals(True)
        self.p_fill.clear()
        self.p_fill.addItem("배경 유지 (글자만 지움)", "")
        self.p_fill.addItem("흰색으로 덮기", "#FFFFFF")
        if current and current.upper() != "#FFFFFF":
            self.p_fill.addItem(f"{current} 으로 덮기", current)
        idx = self.p_fill.findData(current.upper() if current.upper() == "#FFFFFF" else current)
        self.p_fill.setCurrentIndex(max(idx, 0))
        self.p_fill.blockSignals(False)

    # ───────────────────────── 파일 ─────────────────────────
    def on_files_dropped(self, paths: list[str]) -> None:
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            if ext == ".pdf":
                self.open_pdf(p)
            elif ext == ".json":
                self.open_template(p)
            elif ext in (".xlsx", ".xlsm", ".csv", ".xls"):
                self.load_excel(p)

    def open_pdf_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "PDF 열기", self.last_dir, "PDF (*.pdf)")
        if path:
            self.open_pdf(path)

    def open_pdf(self, path: str) -> None:
        try:
            doc = pymupdf.open(path)
            if not doc.is_pdf:
                raise ValueError("PDF 파일이 아닙니다")
            if doc.needs_pass:
                raise ValueError("암호가 걸린 PDF는 열 수 없습니다")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"PDF를 열 수 없습니다.\n{e}")
            return
        self._cancel_ocr()
        self.doc, self.doc_path = doc, path
        self.ocr = {}
        self.last_dir = os.path.dirname(path)
        self.inputs = {}
        self.page_no = 0
        if not self.template.regions:
            self.template.page_sizes = page_sizes(doc)
        self.messages.clear()
        rotated = [str(i + 1) for i, p in enumerate(doc) if p.rotation]
        if rotated:
            self.add_message(f"회전된 쪽({', '.join(rotated)}쪽)은 영역 위치가 어긋날 수 있습니다. "
                             "결과 미리보기로 꼭 확인하세요.", WARN_COLOR)
        self.run_template_check()
        self.recompute()
        self.update_title()
        QTimer.singleShot(0, self.view.fit_width)
        image_pages = [i for i in range(len(doc)) if needs_ocr(doc[i])]
        if image_pages:
            QTimer.singleShot(50, lambda: self.start_ocr(image_pages, auto=True))

    # ───────────────────────── OCR ─────────────────────────
    def start_ocr(self, pages: list[int], auto: bool = False) -> None:
        if not self.doc or not pages:
            return
        if not models_available():
            QMessageBox.warning(self, APP_TITLE, "OCR 모델 파일이 없어 이미지 PDF의 글자를 읽을 수 없습니다.\n"
                                "개발 환경이라면 tools/fetch_models.py를 먼저 실행하세요.")
            return
        self._cancel_ocr()
        if auto:
            self.add_message(f"이미지로 된 PDF입니다({len(pages)}쪽). OCR로 글자를 읽습니다. "
                             "인식이 불확실한 글자는 '영역 안 글자' 목록에 ⚠로 표시됩니다.", WARN_COLOR)
        job = OcrJob(self.doc, pages, self)
        dlg = QProgressDialog("글자를 인식하는 중입니다 (한 쪽에 10초 정도)...", "취소", 0, len(pages), self)
        dlg.setWindowTitle(APP_TITLE)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.canceled.connect(job.cancel)
        job.progress.connect(lambda done, total: (dlg.setValue(done),
                                                  dlg.setLabelText(f"글자를 인식하는 중입니다... ({done}/{total}쪽)")))
        job.failed.connect(lambda msg: self.add_message("✖ " + msg, ERR_COLOR))

        def on_page(page_no: int, words: list) -> None:
            if self._ocr_job is job:
                self.ocr[page_no] = words

        def on_finished(completed: bool) -> None:
            dlg.close()
            if self._ocr_job is not job:
                return
            self._ocr_job = None
            done = len(job.results)
            if completed:
                low = sum(1 for ws in job.results.values() for w in ws if w.score < 0.8)
                msg = f"OCR 완료: {done}쪽에서 단어 {sum(len(ws) for ws in job.results.values())}개를 읽었습니다."
                if low:
                    msg += f" 그중 {low}개는 인식이 불확실합니다."
                self.add_message(msg, OK_COLOR)
            else:
                self.add_message(f"OCR이 중단되었습니다 ({done}/{len(pages)}쪽만 읽음).", WARN_COLOR)
            self.run_template_check()
            ids = self.view.selected_ids()
            self.recompute()
            self.load_props(self.template.region(ids[0]) if ids else None)
            self._report_bulk_issues()

        job.pageDone.connect(on_page)
        job.finished.connect(on_finished)
        self._ocr_job = job
        job.start()

    def _cancel_ocr(self) -> None:
        if self._ocr_job is not None:
            self._ocr_job.cancel()
            self._ocr_job = None

    # ───────────────────────── 엑셀 품번 목록 ─────────────────────────
    def open_excel_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 품번 목록 열기", self.last_dir,
                                              "엑셀/CSV (*.xlsx *.xlsm *.csv)")
        if path:
            self.load_excel(path)

    def load_excel(self, path: str) -> None:
        try:
            rows = read_table(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"엑셀을 읽을 수 없습니다.\n{e}")
            return
        if not rows:
            QMessageBox.warning(self, APP_TITLE, "엑셀에 내용이 없습니다.")
            return
        dlg = ExcelImportDialog(rows, path, self)
        if dlg.exec() != ExcelImportDialog.Accepted:
            return
        bulk = dlg.result_list()
        if not bulk.items:
            QMessageBox.warning(self, APP_TITLE, "품번을 하나도 찾지 못했습니다. 품번 열을 확인하세요.")
            return
        self.bulk = bulk
        self.last_dir = os.path.dirname(path)
        priced = sum(1 for it in bulk.items.values() if it.price is not None)
        self.add_message(f"엑셀 불러옴: {os.path.basename(path)} — 품번 {len(bulk.items)}개 (가격 {priced}개)",
                         OK_COLOR)
        if not any(r.kind == LIST for r in self.template.regions):
            self.add_message("이제 '＋ 품번 대조 영역'(L)으로 PDF의 품목 표를 드래그하세요.", WARN_COLOR)
        ids = self.view.selected_ids()
        self.recompute()
        self.load_props(self.template.region(ids[0]) if ids else None)
        self.update_title()
        self._report_bulk_issues()

    def _report_bulk_issues(self) -> None:
        if not self.bulk or not any(r.kind == LIST for r in self.template.regions):
            return
        rows = bulk_report(self.template, self.results, self.bulk)
        missing = [x.key for x in rows if x.level == "err"]
        warn = sum(1 for x in rows if x.level == "warn")
        if missing:
            shown = ", ".join(missing[:8]) + (" 외" if len(missing) > 8 else "")
            self.add_message(f"엑셀 품번 중 PDF에서 찾지 못한 것 {len(missing)}개: {shown}", ERR_COLOR)
        if warn:
            self.add_message(f"엑셀 대조에서 확인이 필요한 항목 {warn}개 — '대조 결과'(Ctrl+R)에서 보세요.", WARN_COLOR)
        if not missing and not warn:
            self.add_message("엑셀 품번이 모두 PDF에 있고 문제가 없습니다.", OK_COLOR)

    def show_bulk_report(self) -> None:
        if not self.bulk:
            QMessageBox.information(self, APP_TITLE, "먼저 '엑셀 품번 불러오기'로 엑셀을 불러오세요.")
            return
        if not any(r.kind == LIST for r in self.template.regions):
            QMessageBox.information(self, APP_TITLE, "'＋ 품번 대조 영역'으로 PDF의 품목 표를 먼저 지정하세요.")
            return
        base = os.path.splitext(self.doc_path)[0] if self.doc_path else os.path.join(self.last_dir, "대조결과")
        BulkReportDialog(bulk_report(self.template, self.results, self.bulk), base + "_엑셀대조.csv", self).exec()

    def run_ocr_all(self) -> None:
        if self.doc:
            self.start_ocr(list(range(len(self.doc))))

    def new_template(self) -> None:
        if self.template.regions and QMessageBox.question(
                self, APP_TITLE, "지정한 영역을 모두 지우고 새 템플릿을 시작할까요?") != QMessageBox.Yes:
            return
        self.template = Template(page_sizes=page_sizes(self.doc) if self.doc else [])
        self.template_path = ""
        self.inputs = {}
        self.messages.clear()
        self.recompute()
        self.update_title()

    def open_template_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "템플릿 열기", self.last_dir, "템플릿 (*.json)")
        if path:
            self.open_template(path)

    def open_template(self, path: str) -> None:
        try:
            tpl = Template.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"템플릿을 읽을 수 없습니다.\n{e}")
            return
        self.template, self.template_path = tpl, path
        self.inputs = {}
        self.messages.clear()
        self.run_template_check()
        self.recompute()
        self.update_title()

    def save_template(self) -> None:
        default = self.template_path or os.path.join(self.last_dir, "견적서_템플릿.json")
        path, _ = QFileDialog.getSaveFileName(self, "템플릿 저장", default, "템플릿 (*.json)")
        if not path:
            return
        self.template.name = os.path.splitext(os.path.basename(path))[0]
        if self.doc:
            self.template.page_sizes = page_sizes(self.doc)
        self.template.save(path)
        self.template_path = path
        self.update_title()
        self.statusBar().showMessage(f"템플릿 저장됨: {path}", 5000)

    def export_pdf(self) -> None:
        if not self.doc:
            return
        errors = [(r, self.results[r.id].error) for r in self.template.regions
                  if self.results.get(r.id) and self.results[r.id].error]
        if errors:
            lines = "\n".join(f"• {r.name}: {e}" for r, e in errors[:10])
            QMessageBox.warning(self, APP_TITLE, f"오류가 있는 영역을 먼저 해결하세요.\n\n{lines}")
            return
        if not any(res.changed for res in self.results.values()):
            QMessageBox.information(self, APP_TITLE, "바뀌는 내용이 없습니다.")
            return
        base, _ = os.path.splitext(self.doc_path)
        path, _ = QFileDialog.getSaveFileName(self, "결과 PDF 저장", f"{base}_수정본.pdf", "PDF (*.pdf)")
        if not path:
            return
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.doc_path)):
            QMessageBox.warning(self, APP_TITLE, "원본 파일에 덮어쓸 수 없습니다. 다른 이름을 지정하세요.")
            return
        out = apply(self.doc, self.template, self.results, self.ocr)
        try:
            out.save(path, garbage=3, deflate=True)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"저장하지 못했습니다.\n{e}")
            return
        if not self.ocr:
            self._finish_export(path, out, None)
            return
        # 이미지 PDF는 결과를 다시 OCR해서 검증한다
        job = OcrJob(out, sorted(self.ocr), self)
        dlg = QProgressDialog("저장한 PDF를 다시 읽어 검증하는 중입니다...", "", 0, len(self.ocr), self)
        dlg.setCancelButton(None)
        dlg.setWindowTitle(APP_TITLE)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        job.progress.connect(lambda done, _total: dlg.setValue(done))
        job.failed.connect(lambda msg: self.add_message("✖ " + msg, ERR_COLOR))

        def finished(_completed: bool) -> None:
            dlg.close()
            self._finish_export(path, out, job.results)
            self._verify_job = None

        job.finished.connect(finished)
        self._verify_job = job
        job.start()

    def _finish_export(self, path: str, out: pymupdf.Document, ocr_out: dict | None) -> None:
        checks = verify(out, self.template, self.results, ocr_out)
        log_path = os.path.splitext(path)[0] + "_변경내역.csv"
        try:
            write_log(log_path, self.template, self.results, checks)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"변경 내역을 저장하지 못했습니다.\n{e}")
            return

        self.messages.clear()
        names = {r.id: r.name for r in self.template.regions}
        failed = [c for c in checks if not c.ok]
        for c in checks:
            self.add_message(f"{'✔' if c.ok else '✖'} {names.get(c.region_id, '')}: {c.message}",
                             OK_COLOR if c.ok else ERR_COLOR)
        changed = sum(1 for res in self.results.values() if res.changed)
        summary = (f"저장 완료: {os.path.basename(path)}\n변경 {changed}곳, "
                   f"자동 검증 {len(checks) - len(failed)}/{len(checks)} 통과\n"
                   f"변경 내역: {os.path.basename(log_path)}")
        if self.bulk and any(r.kind == LIST for r in self.template.regions):
            rows = bulk_report(self.template, self.results, self.bulk)
            report_path = os.path.splitext(path)[0] + "_엑셀대조.csv"
            try:
                write_bulk_report(report_path, rows)
                summary += f"\n엑셀 대조 결과: {os.path.basename(report_path)}"
            except Exception as e:  # noqa: BLE001
                summary += f"\n엑셀 대조 결과를 저장하지 못했습니다: {e}"
            issues = sum(1 for x in rows if x.level in ("warn", "err"))
            if issues:
                summary += f"\n\n엑셀 대조에서 확인이 필요한 항목이 {issues}개 있습니다. '대조 결과'를 보세요."
        if failed:
            QMessageBox.warning(self, APP_TITLE, summary + "\n\n검증에 실패한 영역이 있습니다. 확인 메시지를 보세요.")
        else:
            QMessageBox.information(self, APP_TITLE, summary)

    # ───────────────────────── 계산과 표시 ─────────────────────────
    def run_template_check(self) -> None:
        if self.doc and self.template.regions:
            for w in check_template(self.doc, self.template, self.ocr):
                self.add_message("⚠ " + w, WARN_COLOR)

    def add_message(self, text: str, color: QColor) -> None:
        item = QListWidgetItem(text)
        item.setForeground(QBrush(color))
        self.messages.addItem(item)

    def recompute(self) -> None:
        self.results = compute(self.doc, self.template, self.inputs, self.ocr, self.bulk) if self.doc else {}
        self.preview_doc = None
        self.fill_table()
        self.render_page()
        self.update_status()

    def update_status(self) -> None:
        if not self.doc:
            return
        changed = sum(1 for r in self.results.values() if r.changed and not r.error)
        errors = sum(1 for r in self.results.values() if r.error)
        msg = f"영역 {len(self.template.regions)}개 · 변경 예정 {changed}곳"
        if errors:
            msg += f" · 오류 {errors}곳"
        self.statusBar().showMessage(msg)

    def fill_table(self) -> None:
        self._syncing = True
        selected = self.view.selected_ids()
        self.table.setRowCount(len(self.template.regions))
        for row, r in enumerate(self.template.regions):
            res = self.results.get(r.id)
            editable_input = r.kind == VALUE and not r.formula.strip()
            if r.kind == LIST:
                if res and res.rows and not res.error:
                    sm = list_summary(res)
                    result_text = (f"유지 {sm['유지']} · 삭제 {sm['삭제']} · 가격수정 {sm['가격수정']}"
                                   + (f" · 확인 {sm['확인']}" if sm["확인"] else ""))
                else:
                    result_text = ""
            elif r.kind == ERASE:
                if r.erase_mode != ERASE_PICK:
                    result_text = "(영역 전체 삭제)"
                elif res and not res.changed:
                    result_text = "(지울 글자 없음)"
                else:
                    kept = " ".join(w.text for w in res.kept_words()) if res else ""
                    result_text = f"남김: {kept}" if kept else "(글자 모두 삭제)"
            elif res and res.error:
                result_text = ""
            elif res:
                result_text = res.new_text if res.changed else f"{res.new_text} (유지)"
            else:
                result_text = ""
            if not self.doc:
                status, color = "PDF 없음", WARN_COLOR
            elif res and res.error:
                status, color = "오류: " + res.error, ERR_COLOR
            elif res and res.changed:
                status, color = "변경", OK_COLOR
            else:
                status, color = "유지", QColor("#57606A")
            cells = [
                (str(r.page + 1), False), ({ERASE: "지우기", LIST: "품번대조"}.get(r.kind, "숫자"), False),
                (r.name, True), (res.original.text if res else "", False),
                (self.inputs.get(r.id, "") if editable_input else
                 ("=" + r.formula if r.formula else
                  ((f"엑셀 {len(self.bulk.items)}개" if self.bulk else "엑셀 없음") if r.kind == LIST else "")),
                 editable_input),
                (result_text, False), (status, False),
            ]
            for col, (text, editable) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, r.id)
                flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
                if editable:
                    flags |= Qt.ItemIsEditable
                else:
                    item.setForeground(QBrush(QColor("#57606A")))
                item.setFlags(flags)
                if col == COL_INPUT and editable:
                    item.setBackground(QBrush(QColor("#FFF8C5")))
                if col == COL_STATUS:
                    item.setForeground(QBrush(color))
                    item.setToolTip(status)
                if col == COL_RESULT and res and res.changed:
                    item.setForeground(QBrush(QColor("#0B5CD5")))
                self.table.setItem(row, col, item)
        self._select_rows(selected)
        self._syncing = False

    def render_page(self) -> None:
        if not self.doc:
            self.view.set_page(None, QRectF(), 1)
            self.view.set_regions([])
            self.page_label.setText(" - / - ")
            return
        self.page_no = max(0, min(self.page_no, len(self.doc) - 1))
        preview = self.preview_action.isChecked()
        src = self.doc
        if preview:
            if self.preview_doc is None:
                self.preview_doc = apply(self.doc, self.template, self.results, self.ocr)
            src = self.preview_doc
        page = src[self.page_no]
        scale = self.view.zoom * self.devicePixelRatioF()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        self.view.set_page(img, QRectF(0, 0, page.rect.width, page.rect.height), scale)
        self.view.set_regions([r for r in self.template.regions if r.page == self.page_no], visible=not preview)
        self.page_label.setText(f" {self.page_no + 1} / {len(self.doc)} ")
        self.update_marks()

    def goto_page(self, n: int) -> None:
        if self.doc and 0 <= n < len(self.doc) and n != self.page_no:
            self.page_no = n
            self.render_page()

    def toggle_preview(self) -> None:
        if self.preview_action.isChecked():
            self.set_mode(MODE_SELECT)
            self.preview_action.setText("원본 보기")
        else:
            self.preview_action.setText("결과 미리보기")
        self.render_page()

    def set_mode(self, mode: str) -> None:
        if mode != MODE_SELECT and self.preview_action.isChecked():
            self.preview_action.setChecked(False)
            self.toggle_preview()
        self.mode_actions[mode].setChecked(True)
        self.view.set_mode(mode)

    def update_title(self) -> None:
        parts = [APP_TITLE]
        if self.doc_path:
            parts.append(os.path.basename(self.doc_path))
        parts.append("템플릿: " + (os.path.basename(self.template_path) if self.template_path else "(저장 안 됨)"))
        if self.bulk:
            parts.append(f"엑셀: {os.path.basename(self.bulk.source)} ({len(self.bulk.items)}개)")
        self.setWindowTitle(" — ".join(parts))

    # ───────────────────────── 영역 편집 ─────────────────────────
    def on_region_drawn(self, kind: str, rect: list[float]) -> None:
        if not self.doc:
            return
        info = read_region(self.doc[self.page_no], rect, self.ocr.get(self.page_no))
        region = Region(page=self.page_no, rect=rect, kind=kind, sample_text=info.text)
        if kind == ERASE:
            region.name = self.template.next_name("삭제")
            self._default_erase_mode(region, info)
        elif kind == LIST:
            region.name = self.template.next_name("품번")
            region.align = "right"
            if not self.bulk:
                self.add_message("품번 대조 영역을 만들었습니다. '엑셀 품번 불러오기'로 엑셀을 불러오면 대조합니다.",
                                 WARN_COLOR)
        else:
            region.name = self.template.next_name("값")
            self._default_value_style(region, info)
        self.template.regions.append(region)
        self.recompute()
        self.view.select_ids([region.id])
        self.on_view_selection([region.id])
        if kind == LIST:
            self._report_bulk_issues()
        if kind == VALUE:
            self.p_name.setFocus()
            self.p_name.selectAll()

    @staticmethod
    def _default_erase_mode(region: Region, info) -> None:
        # 글자가 있으면 단어를 골라 지우는 방식(배경 유지), 없으면 영역 전체를 흰색으로 덮는다
        if info.words:
            region.erase_mode, region.fill = ERASE_PICK, ""
        else:
            region.erase_mode, region.fill = ERASE_ALL, "#FFFFFF"

    def _default_value_style(self, region: Region, info) -> None:
        t = number_target(region, info.words)
        word = info.words[t] if t is not None else None
        st = detect_style(word.text if word else info.text)
        region.prefix, region.suffix = st.prefix, st.suffix
        region.decimals, region.thousands = st.decimals, st.thousands
        region.align = self._guess_align(region.rect, word.bbox if word else info.bbox)

    @staticmethod
    def _guess_align(rect, bbox) -> str:
        if not bbox:
            return "right"
        left_gap, right_gap = bbox[0] - rect[0], rect[2] - bbox[2]
        if abs(left_gap - right_gap) < (rect[2] - rect[0]) * 0.15:
            return "center"
        return "left" if left_gap < right_gap else "right"

    def on_region_moved(self, region: Region) -> None:
        if self.doc and region.page < len(self.doc):
            region.sample_text = read_region(self.doc[region.page], region.rect, self.ocr.get(region.page)).text
        self.recompute()

    def delete_selected(self) -> None:
        ids = set(self.view.selected_ids())
        if not ids:
            return
        self.template.regions = [r for r in self.template.regions if r.id not in ids]
        for rid in ids:
            self.inputs.pop(rid, None)
        self.recompute()
        self.on_view_selection([])

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.table.hasFocus() and self.view.selected_ids():
            self.delete_selected()
            return
        super().keyPressEvent(event)

    # 표 ↔ 화면 ↔ 설정 패널 동기화
    def _select_rows(self, ids: list[str]) -> None:
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row, r in enumerate(self.template.regions):
            if r.id in ids:
                self.table.selectRow(row)
        self.table.blockSignals(False)

    def on_view_selection(self, ids: list[str]) -> None:
        self._select_rows(ids)
        self.load_props(self.template.region(ids[0]) if ids else None)

    def on_table_selection(self) -> None:
        if self._syncing:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        regions = [self.template.regions[r] for r in rows if r < len(self.template.regions)]
        if not regions:
            return
        if regions[0].page != self.page_no:
            self.page_no = regions[0].page
            self.render_page()
        self.view.select_ids([r.id for r in regions])
        self.load_props(regions[0])

    def on_table_edited(self, item: QTableWidgetItem) -> None:
        if self._syncing:
            return
        region = self.template.region(item.data(Qt.UserRole))
        if not region:
            return
        text = item.text().strip()
        if item.column() == COL_NAME:
            if text and text != region.name:
                region.name = text
        elif item.column() == COL_INPUT:
            self.inputs[region.id] = text
        else:
            return
        QTimer.singleShot(0, self._after_edit)

    def _after_edit(self) -> None:
        ids = self.view.selected_ids()
        self.recompute()
        self.view.select_ids(ids)
        self.load_props(self.template.region(ids[0]) if ids else None)

    def load_props(self, region: Region | None) -> None:
        self._syncing = True
        self.props.setEnabled(region is not None)
        self._current = region
        if region:
            self.p_name.setText(region.name)
            self.p_kind.setCurrentIndex(self.p_kind.findData(region.kind))
            self.p_erase_mode.setCurrentIndex(self.p_erase_mode.findData(region.erase_mode))
            self.p_unknown.setCurrentIndex(self.p_unknown.findData(region.unknown_action))
            self.p_number_only.setChecked(region.number_only)
            self.p_formula.setText(region.formula)
            self.p_prefix.setText(region.prefix)
            self.p_suffix.setText(region.suffix)
            self.p_decimals.setValue(region.decimals)
            self.p_thousands.setChecked(region.thousands)
            self.p_align.setCurrentIndex(self.p_align.findData(region.align))
            self.p_size.setValue(region.font_size)
            self.p_list_unmatched.setCurrentIndex(self.p_list_unmatched.findData(region.list_unmatched))
            self.p_list_price.setCurrentIndex(self.p_list_price.findData(region.list_price))
            if region.kind in (VALUE, LIST):
                self._load_font_choices()
                idx = self.p_font.findData(region.font)
                if idx < 0 and region.font:          # 이 PC에 없는 글꼴이 지정된 템플릿
                    self.p_font.addItem(f"{region.font} (설치 안 됨)", region.font)
                    idx = self.p_font.count() - 1
                self.p_font.setCurrentIndex(max(idx, 0))
                self._show_style(region)
            self._set_fill_options(region.fill)
            is_value = region.kind == VALUE
            is_list = region.kind == LIST
            pick = region.kind == ERASE and region.erase_mode == ERASE_PICK
            for w in (self.p_number_only, self.p_formula, self.p_fmt_row_widget):
                self.form.setRowVisible(w, is_value)
            for w in (self.p_align_row_widget, self.p_style_label, self.p_font_row_widget):
                self.form.setRowVisible(w, is_value or is_list)
            for w in (self.p_list_unmatched, self.p_list_price):
                self.form.setRowVisible(w, is_list)
            self.form.setRowVisible(self.p_erase_mode, region.kind == ERASE)
            self.form.setRowVisible(self.p_unknown, pick)
            for b in (self.p_all_erase, self.p_all_keep):
                b.setVisible(pick)
            self._fill_words(region)
        else:
            self.p_words.clear()
        self._syncing = False
        self.update_marks()

    # ── 영역 안 글자 목록 ──
    def _word_states(self, region: Region) -> list[tuple[str, str]]:
        """단어별 (상태, 설명). 상태: erase | keep | target"""
        res = self.results.get(region.id)
        if not res:
            return []
        words = res.original.words
        if region.kind == LIST:
            out: list = [None] * len(words)
            edit_text = {e.word: e.text for e in res.edits}
            for row in res.rows:
                for i, w in enumerate(words):
                    if w.line != row.line:
                        continue
                    if res.erase_flags and res.erase_flags[i]:
                        out[i] = ("erase", row.action)
                    elif i in edit_text:
                        out[i] = ("target", f"가격 → {edit_text[i]}")
                    elif row.key and w.text == row.key:
                        out[i] = ("keep", row.action)
            return out
        if region.kind == ERASE:
            if region.erase_mode == ERASE_PICK:
                return [("erase", "지움") if e else ("keep", "남김") for e in res.erase_flags]
            return [("erase", "지움 (영역 전체)")] * len(words)
        if res.target is None:
            label = f"교체 → {res.new_text}" if res.changed else "교체 대상"
            return [("target", label)] * len(words)
        out = []
        for i in range(len(words)):
            if i == res.target:
                out.append(("target", f"교체 → {res.new_text}" if res.changed else "교체 대상 (값 같음)"))
            else:
                out.append(("keep", "유지"))
        return out

    def _fill_rows(self, region: Region) -> None:
        """품번 대조 영역: 줄마다 판정 결과를 보여 준다."""
        res = self.results.get(region.id)
        self.p_words.blockSignals(True)
        self.p_words.clear()
        if not self.bulk:
            self.p_words_label.setText("'엑셀 품번 불러오기'로 품번·가격 엑셀을 먼저 불러오세요.")
        elif not res or not res.rows:
            self.p_words_label.setText("영역 안에서 읽은 글자가 없습니다.")
        else:
            self.p_words_label.setText("줄마다 엑셀과 대조한 결과입니다. 빨강 = 지움, 파랑 = 가격 바꿈, "
                                       "주황 = 확인 필요. PDF 위에도 같은 색으로 표시됩니다.")
            for row in res.rows:
                if row.status == "unmatched" and "삭제" in row.action:
                    color, icon = ERR_COLOR, "✖"
                elif row.status == "similar" or row.price_status == "unknown" or (
                        row.price_status == "diff" and region.list_price != "replace"):
                    color, icon = WARN_COLOR, "⚠"
                elif row.price_status == "diff":
                    color, icon = QColor("#0B5CD5"), "✎"
                elif row.status == "matched":
                    color, icon = OK_COLOR, "✔"
                else:
                    color, icon = QColor("#57606A"), "·"
                item = QListWidgetItem(f"{icon} {row.line + 1}줄  {row.text}    — {row.action}")
                item.setToolTip(row.note or row.action)
                item.setForeground(QBrush(color))
                if row.status == "unmatched" and "삭제" in row.action:
                    f = QFont()
                    f.setStrikeOut(True)
                    item.setFont(f)
                item.setFlags(Qt.ItemIsEnabled)
                self.p_words.addItem(item)
        self.p_words.blockSignals(False)

    def _fill_words(self, region: Region) -> None:
        if region.kind == LIST:
            self._fill_rows(region)
            return
        res = self.results.get(region.id)
        words = res.original.words if res else []
        states = self._word_states(region)
        pick = region.kind == ERASE and region.erase_mode == ERASE_PICK
        if not self.doc:
            self.p_words_label.setText("PDF를 열면 영역 안의 글자를 읽어 옵니다.")
        elif not words:
            self.p_words_label.setText("영역 안에서 읽은 글자가 없습니다 (이미지·스캔이거나 빈 칸).")
        elif pick:
            self.p_words_label.setText(("[OCR로 읽은 글자] " if words[0].ocr else "") +
                                       "체크한 글자만 지웁니다. PDF 위의 글자를 눌러도 바뀝니다.\n"
                                       "같은 글자는 다른 문서에서도 같은 규칙으로 처리됩니다.")
        elif region.kind == ERASE:
            self.p_words_label.setText("영역 전체를 덮습니다. 일부만 지우려면 '글자 골라 지우기'를 선택하세요.")
        else:
            self.p_words_label.setText("파란색 단어가 새 값으로 바뀌고, 나머지는 그대로 남습니다.")
        self.p_words.blockSignals(True)
        self.p_words.clear()
        line = -1
        for i, (w, (state, desc)) in enumerate(zip(words, states)):
            prefix = f"{w.line + 1}줄  " if w.line != line else "      "
            line = w.line
            warn = f"   ⚠ 인식 불확실({w.score:.2f}) — 원본과 맞는지 확인" if w.ocr and w.score < 0.8 else ""
            item = QListWidgetItem(f"{prefix}{w.text}    — {desc}{warn}")
            item.setData(Qt.UserRole, i)
            item.setForeground(QBrush({"erase": ERR_COLOR, "keep": OK_COLOR}.get(state, QColor("#0B5CD5"))))
            if state == "erase":
                f = QFont()
                f.setStrikeOut(True)
                item.setFont(f)
            if pick:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if state == "erase" else Qt.Unchecked)
            else:
                item.setFlags(Qt.ItemIsEnabled)
            self.p_words.addItem(item)
        self.p_words.blockSignals(False)

    def update_marks(self) -> None:
        region = getattr(self, "_current", None)
        if (region is None or not self.doc or self.preview_action.isChecked()
                or region.page != self.page_no or region.id not in self.results):
            self.view.set_marks([], False)
            return
        words = self.results[region.id].original.words
        marks = [(list(w.bbox), sd[0], f"{w.text}: {sd[1]}")
                 for w, sd in zip(words, self._word_states(region)) if sd is not None]
        self.view.set_marks(marks, region.kind == ERASE and region.erase_mode == ERASE_PICK)

    def _toggle_word(self, index: int, erase: bool) -> None:
        region = getattr(self, "_current", None)
        res = self.results.get(region.id) if region else None
        if not res or index >= len(res.original.words):
            return
        region.set_erase(res.original.words[index].text, erase)
        QTimer.singleShot(0, self._after_edit)

    def on_token_clicked(self, index: int) -> None:
        region = getattr(self, "_current", None)
        res = self.results.get(region.id) if region else None
        if res and index < len(res.erase_flags):
            self._toggle_word(index, not res.erase_flags[index])

    def on_word_toggled(self, item: QListWidgetItem) -> None:
        if not self._syncing:
            self._toggle_word(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def set_all_words(self, erase: bool) -> None:
        region = getattr(self, "_current", None)
        res = self.results.get(region.id) if region else None
        if not res:
            return
        for w in res.original.words:
            region.set_erase(w.text, erase)
        QTimer.singleShot(0, self._after_edit)

    def copy_region_text(self) -> None:
        region = getattr(self, "_current", None)
        res = self.results.get(region.id) if region else None
        if res:
            QGuiApplication.clipboard().setText("\n".join(res.original.lines()))
            self.statusBar().showMessage("영역 안의 텍스트를 복사했습니다.", 3000)

    def on_props_edited(self, *_):
        region = getattr(self, "_current", None)
        if self._syncing or region is None:
            return
        fields_ = ("name", "kind", "formula", "prefix", "suffix", "decimals", "thousands", "align",
                   "font_size", "fill", "erase_mode", "unknown_action", "number_only", "font",
                   "list_unmatched", "list_price")
        before = tuple(getattr(region, f) for f in fields_)
        region.name = self.p_name.text().strip() or region.name
        new_kind = self.p_kind.currentData()
        new_mode = self.p_erase_mode.currentData()
        if new_kind != region.kind:
            region.kind = new_kind
            if new_kind == ERASE:
                res = self.results.get(region.id)
                info = res.original if res else read_region(self.doc[region.page], region.rect,
                                                            self.ocr.get(region.page))
                self._default_erase_mode(region, info)
            else:
                region.fill = ""
        elif region.kind == ERASE and new_mode != region.erase_mode:
            region.erase_mode = new_mode
            region.fill = "" if new_mode == ERASE_PICK else "#FFFFFF"
        else:
            region.fill = self.p_fill.currentData() or ""
        region.unknown_action = self.p_unknown.currentData()
        region.number_only = self.p_number_only.isChecked()
        region.formula = self.p_formula.text().strip().lstrip("=")
        region.prefix, region.suffix = self.p_prefix.text(), self.p_suffix.text()
        region.decimals = self.p_decimals.value()
        region.thousands = self.p_thousands.isChecked()
        region.align = self.p_align.currentData()
        region.font_size = self.p_size.value()
        if region.kind in (VALUE, LIST) and self._fonts_loaded:
            region.font = self.p_font.currentData() or ""
        region.list_unmatched = self.p_list_unmatched.currentData()
        region.list_price = self.p_list_price.currentData()
        if tuple(getattr(region, f) for f in fields_) != before:
            QTimer.singleShot(0, self._after_edit)

    # ── 글꼴·글자색 ──
    def _load_font_choices(self) -> None:
        if self._fonts_loaded:
            return
        self.statusBar().showMessage("설치된 글꼴 목록을 읽는 중...")
        QGuiApplication.processEvents()
        for label in font_choices():
            self.p_font.addItem(label, label)
        self._fonts_loaded = True
        self.statusBar().clearMessage()

    def _detected_style(self, region: Region):
        """사용자 지정값을 빼고, 원본에서 자동으로 알아낸 서식."""
        res = self.results.get(region.id)
        if not self.doc or not res or res.error or region.kind not in (VALUE, LIST) or not res.original.words:
            return None
        if region.kind == LIST and not res.edits:
            return None
        probe = dataclasses.replace(region, font="", font_size=0, color="")
        try:
            if region.kind == LIST:
                e = res.edits[0]
                return word_style(self.doc, probe, res.original.words, e.word, e.text, self.ocr)
            return region_style(self.doc, probe, res, self.ocr)
        except Exception:  # noqa: BLE001
            return None

    def _show_style(self, region: Region) -> None:
        style = self._detected_style(region)
        if style is None and region.kind == LIST:
            self.p_style_label.setText("가격을 바꿀 줄이 생기면 그 글자의 글꼴·크기·색이 표시됩니다.")
        elif style is None:
            self.p_style_label.setText("영역 안 글자를 읽으면 원본의 글꼴·크기·색이 표시됩니다.")
        else:
            self.p_style_label.setText("감지: " + style.describe())
        if region.color:
            swatch, text = region.color, "직접 지정"
        elif style is not None:
            swatch, text = "#%02X%02X%02X" % tuple(round(c * 255) for c in style.color), "원본색"
        else:
            swatch, text = "#000000", "원본색"
        light = QColor(swatch).lightness() > 140
        self.p_color_btn.setText(f"■ {text}")
        self.p_color_btn.setStyleSheet(f"color:{swatch}; font-weight:bold;"
                                       + ("background:#555;" if light else ""))
        self.p_color_auto.setEnabled(bool(region.color))

    def pick_text_color(self) -> None:
        region = getattr(self, "_current", None)
        if region is None or region.kind not in (VALUE, LIST):
            return
        style = self._detected_style(region)
        start = region.color or ("#%02X%02X%02X" % tuple(round(c * 255) for c in style.color) if style else "#000000")
        c = QColorDialog.getColor(QColor(start), self, "새 글자색 선택")
        if c.isValid():
            region.color = c.name().upper()
            QTimer.singleShot(0, self._after_edit)

    def reset_text_color(self) -> None:
        region = getattr(self, "_current", None)
        if region is not None and region.color:
            region.color = ""
            QTimer.singleShot(0, self._after_edit)

    def pick_fill_color(self) -> None:
        region = getattr(self, "_current", None)
        if region is None:
            return
        c = QColorDialog.getColor(QColor(region.fill or "#FFFFFF"), self, "덮을 색 선택")
        if c.isValid():
            region.fill = c.name().upper()
            self._set_fill_options(region.fill)
            QTimer.singleShot(0, self._after_edit)
