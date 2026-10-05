"""여러 파일 일괄 처리 창: 같은 양식의 PDF 여러 개에 지금 템플릿·엑셀 대조를 한꺼번에 적용한다."""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from core.batch import (BatchFile, missing_everywhere, open_file, process, save, verify_saved, write_batch_report)
from core.models import LIST

from .ocr_job import OcrJob

COLS = ["번호", "파일", "상태", "남는 품번 (저장 이름에 들어감)", "가격", "지운 줄", "확인 필요", "저장할 이름"]
OK, WARN, ERR, GRAY = QColor("#1A7F37"), QColor("#9A6700"), QColor("#CF222E"), QColor("#57606A")


class BatchDialog(QDialog):
    def __init__(self, main, paths: list[str]):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("여러 파일 일괄 처리")
        self.resize(1150, 620)
        self.setAcceptDrops(True)
        self.files: list[BatchFile] = []
        self._busy = False
        self._stop = False
        self._job: OcrJob | None = None
        self._tpl = None
        self._bulk = None

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.use_inputs = QCheckBox("지금 문서의 '새 값' 입력도 모든 파일에 똑같이 적용")
        self.use_inputs.setToolTip("끄면 템플릿의 수식·지우기 규칙과 엑셀 대조만 적용합니다 (파일마다 다른 값은 입력하지 않음).")
        self.verify = QCheckBox("저장한 파일을 다시 읽어 검증 (이미지 PDF는 쪽마다 10초 정도 더 걸림)")
        self.verify.setChecked(True)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _: self.open_selected())

        self.summary = QLabel("PDF 파일을 추가한 뒤 '대조 실행'을 누르세요. 파일을 이 창에 끌어다 놓아도 됩니다.")
        self.summary.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.btn_add = QPushButton("파일 추가…")
        self.btn_remove = QPushButton("선택 빼기")
        self.btn_run = QPushButton("대조 실행")
        self.btn_open = QPushButton("선택 파일 열어 보기")
        self.btn_save = QPushButton("모두 저장…")
        self.btn_stop = QPushButton("중지")
        self.btn_close = QPushButton("닫기")
        self.btn_run.setDefault(True)
        self.btn_stop.setVisible(False)
        self.btn_add.clicked.connect(self.add_dialog)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_run.clicked.connect(self.run)
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_save.clicked.connect(self.save_all)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_close.clicked.connect(self.close)

        top = QHBoxLayout()
        for b in (self.btn_add, self.btn_remove):
            top.addWidget(b)
        top.addStretch(1)
        top.addWidget(self.use_inputs)
        bottom = QHBoxLayout()
        bottom.addWidget(self.verify)
        bottom.addStretch(1)
        for b in (self.btn_stop, self.btn_run, self.btn_open, self.btn_save, self.btn_close):
            bottom.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.summary)
        lay.addWidget(self.progress)
        lay.addLayout(bottom)
        self.add_files(paths)
        self._update_info()

    # ── 파일 목록 ──
    def _update_info(self) -> None:
        tpl, bulk = self.main.template, self.main.bulk
        lists = sum(1 for r in tpl.regions if r.kind == LIST)
        text = f"<b>적용할 템플릿</b>: 영역 {len(tpl.regions)}개"
        if lists:
            text += f" (엑셀 품번 대조 {lists}개)"
        text += " · <b>엑셀</b>: " + (f"{os.path.basename(bulk.source)} — 품번 {len(bulk.items)}개" if bulk else "없음")
        text += "<br>저장 이름: <b>번호. 남는 품번1, 품번2 ….pdf</b> (품번이 없으면 번호. 원래 이름.pdf)"
        if lists and not bulk:
            text += "<br><span style='color:#CF222E'>엑셀 품번 대조 영역이 있지만 엑셀을 불러오지 않았습니다.</span>"
        self.info.setText(text)

    def add_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "PDF 추가", self.main.last_dir, "PDF (*.pdf)")
        self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        if self._busy:
            return
        known = {os.path.normcase(os.path.abspath(f.path)) for f in self.files}
        for p in paths:
            if p.lower().endswith(".pdf") and os.path.normcase(os.path.abspath(p)) not in known:
                self.files.append(BatchFile(p, 0))
                known.add(os.path.normcase(os.path.abspath(p)))
        self._renumber()
        self._fill()

    def _renumber(self) -> None:
        for i, bf in enumerate(self.files, start=1):
            bf.number = i

    def remove_selected(self) -> None:
        if self._busy:
            return
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.files = [bf for i, bf in enumerate(self.files) if i not in rows]
        self._renumber()
        self._fill()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_files([u.toLocalFile() for u in event.mimeData().urls()])

    # ── 표 ──
    def _fill(self) -> None:
        self.table.setRowCount(len(self.files))
        for row, bf in enumerate(self.files):
            self._fill_row(row, bf)

    def _fill_row(self, row: int, bf: BatchFile) -> None:
        if bf.error:
            status, color = bf.error, ERR
        elif not bf.processed:
            status, color = "대기", GRAY
        elif bf.checks:
            failed = [c for c in bf.checks if not c.ok]
            status, color = (f"저장됨, 검증 {len(failed)}곳 확인 필요", WARN) if failed else ("저장됨, 검증 OK", OK)
        elif bf.out_path:
            status, color = "저장됨", OK
        else:
            status, color = "대조 완료" + (f" (OCR {len(bf.ocr_pages)}쪽)" if bf.ocr_pages else ""), OK
        sm = bf.summary() if bf.processed else {}
        price_ok = sum(1 for r in bf.report if "가격 일치" in r.status)
        price_diff = sum(1 for r in bf.report if "가격 다름" in r.status)
        issues = sm.get("확인", 0) + sm.get("오류", 0) + len(bf.warnings)
        issue_text = ""
        if bf.processed:
            notes = [w for w in bf.warnings] + [f"{r.pdf_key}: {r.status}" for r in bf.report
                                                if r.level == "warn" and r.key]
            notes += [f"{res.error}" for res in bf.results.values() if res.error]
            issue_text = f"{issues}  " + " / ".join(notes[:3]) if issues else "없음"
        cells = [
            str(bf.number), bf.name, status,
            ", ".join(bf.keys) if bf.processed else "",
            (f"일치 {price_ok}" + (f" · 수정/다름 {price_diff}" if price_diff else "")) if bf.processed else "",
            str(sm.get("삭제", "")) if bf.processed else "",
            issue_text,
            os.path.basename(bf.out_path) if bf.out_path else bf.out_name,
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if col == 2:
                item.setForeground(QBrush(color))
            if col == 6 and bf.processed and issues:
                item.setForeground(QBrush(WARN))
            self.table.setItem(row, col, item)

    def _set_busy(self, busy: bool, total: int = 0) -> None:
        self._busy = busy
        self._stop = False
        for b in (self.btn_add, self.btn_remove, self.btn_run, self.btn_open, self.btn_save, self.btn_close):
            b.setEnabled(not busy)
        self.btn_stop.setVisible(busy)
        self.progress.setVisible(busy)
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)

    def stop(self) -> None:
        self._stop = True
        if self._job is not None:
            self._job.cancel()

    def closeEvent(self, event):
        if self._busy:
            self.stop()
        super().closeEvent(event)

    # ── 대조 실행 ──
    def run(self) -> None:
        if not self.files:
            QMessageBox.information(self, self.windowTitle(), "먼저 PDF 파일을 추가하세요.")
            return
        if not self.main.template.regions:
            QMessageBox.information(self, self.windowTitle(),
                                    "적용할 영역이 없습니다. 먼저 PDF 하나에서 영역을 만들거나 템플릿을 불러오세요.")
            return
        self._tpl = copy.deepcopy(self.main.template)     # 처리 중에 메인 창에서 고쳐도 영향 없도록
        self._bulk = self.main.bulk
        self._inputs = dict(self.main.inputs) if self.use_inputs.isChecked() else {}
        for bf in self.files:
            bf.processed, bf.error, bf.out_path, bf.checks, bf.ocr = False, "", "", [], {}
        self._fill()
        self._update_info()
        self._set_busy(True, len(self.files))
        self._index = -1
        self._next_file()

    def _next_file(self) -> None:
        self._index += 1
        self.progress.setValue(self._index)
        if self._stop or self._index >= len(self.files):
            self._finish_run()
            return
        bf = self.files[self._index]
        self.summary.setText(f"{bf.number}/{len(self.files)} {bf.name} 처리 중...")
        open_file(bf, self.main.ocr_on_open)
        if bf.error or not bf.ocr_pages:
            self._after_ocr(bf)
            return
        self.summary.setText(f"{bf.number}/{len(self.files)} {bf.name} — 글자 인식(OCR) 중 "
                             f"({len(bf.ocr_pages)}쪽, 한 쪽에 10초 정도)...")
        job = OcrJob(bf.doc, bf.ocr_pages, self)
        job.pageDone.connect(lambda p, words, bf=bf: bf.ocr.__setitem__(p, words))
        job.failed.connect(lambda msg, bf=bf: setattr(bf, "error", msg))
        job.finished.connect(lambda _ok, bf=bf: self._after_ocr(bf))
        self._job = job
        job.start()

    def _after_ocr(self, bf: BatchFile) -> None:
        self._job = None
        if not bf.error and not self._stop:
            try:
                process(bf, self._tpl, self._bulk, self._inputs, len(self.files))
            except Exception as e:  # noqa: BLE001
                bf.error = f"처리 오류: {e}"
        self._fill_row(self._index, bf)
        QTimer.singleShot(0, self._next_file)

    def _finish_run(self) -> None:
        self._set_busy(False)
        done = [bf for bf in self.files if bf.processed]
        errors = [bf for bf in self.files if bf.error]
        text = f"파일 {len(self.files)}개 중 {len(done)}개 대조 완료"
        if errors:
            text += f", 열 수 없거나 오류 {len(errors)}개"
        if self._bulk:
            missing = missing_everywhere(done, self._bulk)
            found = len(self._bulk.items) - len(missing)
            text += f" · 엑셀 품번 {len(self._bulk.items)}개 중 {found}개를 찾음"
            if missing:
                shown = ", ".join(missing[:10]) + (" 외" if len(missing) > 10 else "")
                text += f"<br><span style='color:#CF222E'>어느 파일에도 없는 엑셀 품번 {len(missing)}개: {shown}</span>"
        issues = sum(1 for bf in done if bf.summary()["확인"] or bf.warnings)
        if issues:
            text += f"<br><span style='color:#9A6700'>확인이 필요한 파일 {issues}개 — 두 번 클릭해 열어 보세요.</span>"
        if self._stop:
            text = "중지했습니다. " + text
        self.summary.setText(text)

    # ── 열어 보기 ──
    def open_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        bf = self.files[rows[0]]
        self.main.open_pdf(bf.path, ocr=bf.ocr if bf.processed else None)
        self.main.raise_()
        self.main.activateWindow()

    # ── 모두 저장 ──
    def save_all(self) -> None:
        ready = [bf for bf in self.files if bf.processed and not bf.error]
        if not ready:
            QMessageBox.information(self, self.windowTitle(), "저장할 파일이 없습니다. 먼저 '대조 실행'을 하세요.")
            return
        default = os.path.join(os.path.dirname(ready[0].path), "수정본")
        folder = QFileDialog.getExistingDirectory(self, "저장할 폴더 선택 (원본 파일은 바뀌지 않습니다)", default)
        if not folder:
            return
        originals = {os.path.normcase(os.path.dirname(os.path.abspath(bf.path))) for bf in ready}
        if os.path.normcase(os.path.abspath(folder)) in originals and QMessageBox.question(
                self, self.windowTitle(), "원본과 같은 폴더입니다. 새 이름으로 저장되므로 원본은 그대로 남습니다. "
                "계속할까요?") != QMessageBox.Yes:
            return
        os.makedirs(folder, exist_ok=True)
        self._folder, self._taken, self._save_queue = folder, set(), ready
        self._set_busy(True, len(ready))
        self._index = -1
        self._next_save()

    def _next_save(self) -> None:
        self._index += 1
        self.progress.setValue(self._index)
        if self._stop or self._index >= len(self._save_queue):
            self._finish_save()
            return
        bf = self._save_queue[self._index]
        self.summary.setText(f"{self._index + 1}/{len(self._save_queue)} {bf.out_name} 저장 중...")
        try:
            out = save(bf, self._tpl, self._folder, self._taken)
        except Exception as e:  # noqa: BLE001
            bf.error = f"저장 실패: {e}"
            self._refresh_file(bf)
            QTimer.singleShot(0, self._next_save)
            return
        if not self.verify.isChecked():
            self._refresh_file(bf)
            QTimer.singleShot(0, self._next_save)
            return
        if not bf.ocr:
            verify_saved(bf, self._tpl, out, None)
            self._refresh_file(bf)
            QTimer.singleShot(0, self._next_save)
            return
        self.summary.setText(f"{self._index + 1}/{len(self._save_queue)} {bf.out_name} — 다시 읽어 검증 중...")
        job = OcrJob(out, sorted(bf.ocr), self)

        def done(_ok, bf=bf, out=out, job=job):
            self._job = None
            verify_saved(bf, self._tpl, out, job.results)
            self._refresh_file(bf)
            QTimer.singleShot(0, self._next_save)

        job.finished.connect(done)
        self._job = job
        job.start()

    def _refresh_file(self, bf: BatchFile) -> None:
        self._fill_row(self.files.index(bf), bf)

    def _finish_save(self) -> None:
        self._set_busy(False)
        saved = [bf for bf in self._save_queue if bf.out_path]
        report = os.path.join(self._folder, "일괄처리_결과.csv")
        try:
            write_batch_report(report, self.files, self._bulk)
            report_note = f"전체 결과: {os.path.basename(report)}"
        except Exception as e:  # noqa: BLE001
            report_note = f"결과 CSV를 저장하지 못했습니다: {e}"
        failed = [bf for bf in saved if bf.checks and not all(c.ok for c in bf.checks)]
        text = f"{len(saved)}개 파일을 저장했습니다.\n폴더: {self._folder}\n{report_note}"
        if failed:
            text += f"\n\n검증에서 확인이 필요한 파일 {len(failed)}개가 있습니다. 표의 상태 열을 보세요."
        if self._stop:
            text = "중지했습니다.\n" + text
        self.summary.setText(text.replace("\n", "<br>"))
        self._done_box(text, bool(failed))

    def _done_box(self, text: str, warn: bool) -> None:
        box = QMessageBox(QMessageBox.Warning if warn else QMessageBox.Information, self.windowTitle(), text,
                          parent=self)
        open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._folder))

