"""엑셀 품번 목록 불러오기 / 대조 결과 창."""
from __future__ import annotations

import os

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from core.bulk import BulkList, build, guess_columns
from core.engine import BulkRow, write_bulk_report

LEVEL_COLORS = {"ok": QColor("#1A7F37"), "warn": QColor("#9A6700"), "err": QColor("#CF222E"),
                "del": QColor("#57606A")}
PREVIEW_ROWS = 30


def _col_name(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


class ExcelImportDialog(QDialog):
    """엑셀에서 어느 열이 품번이고 어느 열이 가격인지 확인/선택한다."""

    def __init__(self, rows: list[list[str]], path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("엑셀 품번 목록 불러오기")
        self.resize(820, 560)
        self.rows, self.path = rows, path
        header, key_col, price_col = guess_columns(rows)
        width = max((len(r) for r in rows), default=0)

        self.header = QComboBox()
        self.header.addItem("머리글 없음 (첫 행부터 자료)", -1)
        for i in range(min(10, len(rows))):
            self.header.addItem(f"{i + 1}행: " + " | ".join(c for c in rows[i][:6] if c)[:60], i)
        self.key = QComboBox()
        self.price = QComboBox()
        self.price.addItem("가격 없음 (품번만 확인)", -1)
        for c in range(width):
            self.key.addItem(_col_name(c), c)
            self.price.addItem(_col_name(c), c)
        self.header.setCurrentIndex(self.header.findData(header))
        self.key.setCurrentIndex(self.key.findData(key_col))
        self.price.setCurrentIndex(self.price.findData(price_col))

        form = QFormLayout()
        form.addRow("머리글 행", self.header)
        form.addRow("품번 열", self.key)
        form.addRow("가격 열", self.price)

        self.preview = QTableWidget(min(len(rows), PREVIEW_ROWS), width)
        self.preview.setHorizontalHeaderLabels([_col_name(c) for c in range(width)])
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r, row in enumerate(rows[:PREVIEW_ROWS]):
            for c, v in enumerate(row):
                self.preview.setItem(r, c, QTableWidgetItem(v))
        self.summary = QLabel()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("불러오기")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{os.path.basename(path)}</b> — 자동으로 고른 열이 맞는지 확인하세요. "
                             "(품번 열 = 초록, 가격 열 = 파랑)"))
        lay.addLayout(form)
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.summary)
        lay.addWidget(buttons)
        for w in (self.header, self.key, self.price):
            w.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def result_list(self) -> BulkList:
        return build(self.rows, self.header.currentData(), self.key.currentData(), self.price.currentData(), self.path)

    def _refresh(self) -> None:
        header, key, price = self.header.currentData(), self.key.currentData(), self.price.currentData()
        for r in range(self.preview.rowCount()):
            for c in range(self.preview.columnCount()):
                item = self.preview.item(r, c)
                if item is None:
                    continue
                if r <= header:
                    color = QColor("#EAEEF2")
                elif c == key:
                    color = QColor("#DAFBE1")
                elif c == price:
                    color = QColor("#DDF4FF")
                else:
                    color = QColor("white")
                item.setBackground(QBrush(color))
        bulk = self.result_list()
        priced = sum(1 for it in bulk.items.values() if it.price is not None)
        text = f"품번 {len(bulk.items)}개 · 가격이 있는 품번 {priced}개"
        if bulk.duplicates:
            text += f" · 중복 품번 {len(bulk.duplicates)}개(마지막 값 사용)"
        if bulk.skipped:
            text += f" · 품번이 빈 행 {bulk.skipped}개 건너뜀"
        self.summary.setText(text)


class BulkReportDialog(QDialog):
    """엑셀 품번 하나하나의 대조 결과."""

    def __init__(self, rows: list[BulkRow], default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("엑셀 대조 결과")
        self.resize(900, 600)
        self.rows, self.default_path = rows, default_path
        ok = sum(1 for r in rows if r.level == "ok")
        warn = sum(1 for r in rows if r.level == "warn")
        err = sum(1 for r in rows if r.level == "err")
        deleted = sum(1 for r in rows if r.level == "del")
        head = QLabel(f"<b>엑셀 품번 {sum(1 for r in rows if r.key)}개</b> — "
                      f"<span style='color:#1A7F37'>문제없음 {ok}</span> · "
                      f"<span style='color:#9A6700'>확인 필요 {warn}</span> · "
                      f"<span style='color:#CF222E'>PDF에 없음 {err}</span>"
                      + (f" &nbsp;|&nbsp; <span style='color:#57606A'>엑셀에 없어 지운 줄 {deleted}</span>"
                         if deleted else ""))
        self.only_issues = QCheckBox("확인이 필요한 것만 보기")
        self.only_issues.toggled.connect(self._fill)
        save = QPushButton("CSV로 저장")
        save.clicked.connect(self._save)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["엑셀 품번", "엑셀 가격", "PDF 품번", "PDF 가격", "결과"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)

        top = QHBoxLayout()
        top.addWidget(head, 1)
        top.addWidget(self.only_issues)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(save)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        self._fill()

    def _fill(self) -> None:
        rows = [r for r in self.rows if not self.only_issues.isChecked() or r.level in ("warn", "err")]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, v in enumerate([r.key, r.excel_price, r.pdf_key, r.pdf_price, r.status]):
                item = QTableWidgetItem(v)
                item.setForeground(QBrush(LEVEL_COLORS[r.level]) if c == 4 else QBrush(QColor("#24292F")))
                self.table.setItem(i, c, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "대조 결과 저장", self.default_path, "CSV (*.csv)")
        if not path:
            return
        try:
            write_bulk_report(path, self.rows)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "엑셀 대조 결과", f"저장하지 못했습니다.\n{e}")
            return
        QMessageBox.information(self, "엑셀 대조 결과", f"저장했습니다: {os.path.basename(path)}")

