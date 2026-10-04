from __future__ import annotations

import os

import pymupdf
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFont, QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QToolBar, QVBoxLayout, QWidget)

from core.engine import (RegionResult, apply, check_template, compute, number_target, page_sizes, read_region,
                         verify, write_log)
from core.models import ERASE, ERASE_ALL, ERASE_PICK, VALUE, Region, Template
from core.numbers import detect_style

from .pdf_view import MODE_ERASE, MODE_SELECT, MODE_VALUE, PdfView

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
        form.addRow("", self.p_number_only)
        form.addRow(self._formula_label, self.p_formula)
        self.p_fmt_row_widget, self.p_align_row_widget = QWidget(), QWidget()
        for w, row in ((self.p_fmt_row_widget, fmt_row), (self.p_align_row_widget, align_row)):
            row.setContentsMargins(0, 0, 0, 0)
            w.setLayout(row)
        form.addRow(self._fmt_label, self.p_fmt_row_widget)
        form.addRow(self._align_label, self.p_align_row_widget)
        form.addRow("배경 처리", fill_row)
        form.addRow("영역 안 글자", self._words_widget)
        self.form = form
        lay.addWidget(self.props)

        for w in (self.p_name, self.p_formula, self.p_prefix, self.p_suffix):
            w.editingFinished.connect(self.on_props_edited)
        for w in (self.p_kind, self.p_align, self.p_fill, self.p_erase_mode, self.p_unknown):
            w.currentIndexChanged.connect(self.on_props_edited)
        self.p_number_only.toggled.connect(self.on_props_edited)
        self.p_decimals.valueChanged.connect(self.on_props_edited)
        self.p_size.valueChanged.connect(self.on_props_edited)
        self.p_thousands.toggled.connect(self.on_props_edited)
        self.p_fill_btn.clicked.connect(self.pick_fill_color)
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
        self.doc, self.doc_path = doc, path
        self.last_dir = os.path.dirname(path)
        self.inputs = {}
        self.page_no = 0
        if not self.template.regions:
            self.template.page_sizes = page_sizes(doc)
        self.messages.clear()
        if not any(p.get_text("text").strip() for p in doc):
            self.add_message("이 PDF에는 글자 정보가 없습니다(스캔 이미지). 원본 값을 읽을 수 없으니 "
                             "모든 숫자 영역에 새 값을 직접 입력해야 합니다.", WARN_COLOR)
        rotated = [str(i + 1) for i, p in enumerate(doc) if p.rotation]
        if rotated:
            self.add_message(f"회전된 쪽({', '.join(rotated)}쪽)은 영역 위치가 어긋날 수 있습니다. "
                             "결과 미리보기로 꼭 확인하세요.", WARN_COLOR)
        self.run_template_check()
        self.recompute()
        self.update_title()
        QTimer.singleShot(0, self.view.fit_width)

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
        out = apply(self.doc, self.template, self.results)
        checks = verify(out, self.template, self.results)
        try:
            out.save(path, garbage=3, deflate=True)
            log_path = os.path.splitext(path)[0] + "_변경내역.csv"
            write_log(log_path, self.template, self.results, checks)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, f"저장하지 못했습니다.\n{e}")
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
        if failed:
            QMessageBox.warning(self, APP_TITLE, summary + "\n\n검증에 실패한 영역이 있습니다. 확인 메시지를 보세요.")
        else:
            QMessageBox.information(self, APP_TITLE, summary)

    # ───────────────────────── 계산과 표시 ─────────────────────────
    def run_template_check(self) -> None:
        if self.doc and self.template.regions:
            for w in check_template(self.doc, self.template):
                self.add_message("⚠ " + w, WARN_COLOR)

    def add_message(self, text: str, color: QColor) -> None:
        item = QListWidgetItem(text)
        item.setForeground(QBrush(color))
        self.messages.addItem(item)

    def recompute(self) -> None:
        self.results = compute(self.doc, self.template, self.inputs) if self.doc else {}
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
            if r.kind == ERASE:
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
                (str(r.page + 1), False), ("지우기" if r.kind == ERASE else "숫자", False),
                (r.name, True), (res.original.text if res else "", False),
                (self.inputs.get(r.id, "") if editable_input else ("=" + r.formula if r.formula else ""),
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
                self.preview_doc = apply(self.doc, self.template, self.results)
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
        self.setWindowTitle(" — ".join(parts))

    # ───────────────────────── 영역 편집 ─────────────────────────
    def on_region_drawn(self, kind: str, rect: list[float]) -> None:
        if not self.doc:
            return
        info = read_region(self.doc[self.page_no], rect)
        region = Region(page=self.page_no, rect=rect, kind=kind, sample_text=info.text)
        if kind == ERASE:
            region.name = self.template.next_name("삭제")
            self._default_erase_mode(region, info)
        else:
            region.name = self.template.next_name("값")
            self._default_value_style(region, info)
        self.template.regions.append(region)
        self.recompute()
        self.view.select_ids([region.id])
        self.on_view_selection([region.id])
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
            region.sample_text = read_region(self.doc[region.page], region.rect).text
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
            self._set_fill_options(region.fill)
            is_value = region.kind == VALUE
            pick = region.kind == ERASE and region.erase_mode == ERASE_PICK
            for w in (self.p_number_only, self.p_formula, self.p_fmt_row_widget, self.p_align_row_widget):
                self.form.setRowVisible(w, is_value)
            self.form.setRowVisible(self.p_erase_mode, not is_value)
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

    def _fill_words(self, region: Region) -> None:
        res = self.results.get(region.id)
        words = res.original.words if res else []
        states = self._word_states(region)
        pick = region.kind == ERASE and region.erase_mode == ERASE_PICK
        if not self.doc:
            self.p_words_label.setText("PDF를 열면 영역 안의 글자를 읽어 옵니다.")
        elif not words:
            self.p_words_label.setText("영역 안에서 읽은 글자가 없습니다 (이미지·스캔이거나 빈 칸).")
        elif pick:
            self.p_words_label.setText("체크한 글자만 지웁니다. PDF 위의 글자를 눌러도 바뀝니다.\n"
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
            item = QListWidgetItem(f"{prefix}{w.text}    — {desc}")
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
        marks = [(list(w.bbox), state, f"{w.text}: {desc}")
                 for w, (state, desc) in zip(words, self._word_states(region))]
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
                   "font_size", "fill", "erase_mode", "unknown_action", "number_only")
        before = tuple(getattr(region, f) for f in fields_)
        region.name = self.p_name.text().strip() or region.name
        new_kind = self.p_kind.currentData()
        new_mode = self.p_erase_mode.currentData()
        if new_kind != region.kind:
            region.kind = new_kind
            if new_kind == ERASE:
                res = self.results.get(region.id)
                info = res.original if res else read_region(self.doc[region.page], region.rect)
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
        if tuple(getattr(region, f) for f in fields_) != before:
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
