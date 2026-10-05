"""PDF 페이지를 보여주고, 마우스로 영역을 그리고/옮기고/크기를 바꾸는 뷰.

장면(scene) 좌표는 PDF 포인트 단위와 같다. 확대/축소는 뷰 변환으로만 처리한다.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView)

from core.models import ERASE, Region

MODE_SELECT, MODE_ERASE, MODE_VALUE, MODE_LIST = "select", "erase", "value", "list"
COLORS = {ERASE: QColor("#E5484D"), "value": QColor("#2F6FEB"), "list": QColor("#8250DF")}
_MODE_KIND = {MODE_ERASE: ERASE, MODE_VALUE: "value", MODE_LIST: "list"}
HANDLE_PX = 8


class _Label(QGraphicsSimpleTextItem):
    """영역 왼쪽 위 안쪽에 붙는 이름표. 확대/축소와 상관없이 같은 크기로 보인다."""

    def paint(self, painter, option, widget=None):
        bg = QColor(255, 255, 255, 210)
        painter.fillRect(self.boundingRect().adjusted(-2, 0, 2, 0), bg)
        super().paint(painter, option, widget)


class RegionItem(QGraphicsRectItem):
    def __init__(self, region: Region, on_committed: Callable[[Region], None]):
        super().__init__()
        self.region = region
        self._on_committed = on_committed
        self._resizing = False
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        self.setAcceptHoverEvents(True)
        self.label = _Label(self)
        self.label.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        f = QFont()
        f.setPointSize(7)
        f.setBold(True)
        self.label.setFont(f)
        self.refresh()

    def refresh(self) -> None:
        x0, y0, x1, y1 = self.region.rect
        self.setPos(0, 0)
        self.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        color = COLORS[self.region.kind]
        self.label.setText(("✕ " if self.region.kind == ERASE else "") + self.region.name)
        self.label.setBrush(color.darker(130))
        self.label.setPos(x0 + 2 / self._zoom(), y0 + 1 / self._zoom())
        self._style()

    def _zoom(self) -> float:
        views = self.scene().views() if self.scene() else []
        return views[0].transform().m11() if views else 1.0

    def _style(self) -> None:
        color = COLORS[self.region.kind]
        pen = QPen(color, 2.5 if self.isSelected() else 1.5)
        pen.setCosmetic(True)
        if self.isSelected():
            pen.setStyle(Qt.DashLine)
        fill = QColor(color)
        fill.setAlpha(18 if self.isSelected() else 40)   # 선택하면 안쪽 단어 표시가 잘 보이도록 옅게
        self.setPen(pen)
        self.setBrush(QBrush(fill))
        self.label.setVisible(not self.isSelected())

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._style()
        return super().itemChange(change, value)

    def _near_corner(self, pos: QPointF) -> bool:
        tol = HANDLE_PX / self._zoom()
        br = self.rect().bottomRight()
        return abs(pos.x() - br.x()) <= tol and abs(pos.y() - br.y()) <= tol

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.isSelected():   # 크기 조절 손잡이
            s = HANDLE_PX / self._zoom()
            br = self.rect().bottomRight()
            painter.fillRect(QRectF(br.x() - s, br.y() - s, s, s), COLORS[self.region.kind])

    def hoverMoveEvent(self, event):
        self.setCursor(Qt.SizeFDiagCursor if self._near_corner(event.pos()) else Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._near_corner(event.pos()):
            self._resizing = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            r = QRectF(self.rect().topLeft(), event.pos())
            if r.width() > 3 and r.height() > 3:
                self.prepareGeometryChange()
                self.setRect(r)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._resizing = False
        r = self.mapRectToScene(self.rect())
        new = [round(r.left(), 2), round(r.top(), 2), round(r.right(), 2), round(r.bottom(), 2)]
        if new != self.region.rect:
            self.region.rect = new
            self.refresh()
            self._on_committed(self.region)


MARK_COLORS = {"erase": QColor("#CF222E"), "keep": QColor("#1A7F37"), "target": QColor("#2F6FEB")}


class TokenItem(QGraphicsRectItem):
    """영역 안에서 읽어 낸 단어 하나. 지울 단어는 빨간 취소선, 남길 단어는 초록 테두리로 보인다."""

    def __init__(self, rect: list[float], state: str, index: int, tip: str,
                 on_click: Callable[[int], None] | None):
        x0, y0, x1, y1 = rect
        super().__init__(QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-0.5, -0.5, 0.5, 0.5))
        self.state, self.index, self._on_click = state, index, on_click
        color = MARK_COLORS[state]
        pen = QPen(color, 1.6 if state == "target" else 1.2)
        pen.setCosmetic(True)
        fill = QColor(color)
        fill.setAlpha(55 if state == "erase" else 30)
        self.setPen(pen)
        self.setBrush(QBrush(fill))
        self.setZValue(5)
        self.setToolTip(tip)
        if on_click:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setAcceptedMouseButtons(Qt.NoButton)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.state == "erase":
            pen = QPen(MARK_COLORS["erase"], 1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            r = self.rect()
            painter.drawLine(QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y()))

    def mousePressEvent(self, event):
        if self._on_click and event.button() == Qt.LeftButton:
            self._on_click(self.index)
            event.accept()
            return
        super().mousePressEvent(event)


class PdfView(QGraphicsView):
    regionDrawn = Signal(str, list)          # 종류, [x0, y0, x1, y1]
    regionEdited = Signal(object)            # Region
    selectionIds = Signal(list)              # 선택된 영역 id 목록
    deleteRequested = Signal()
    zoomChanged = Signal(float)
    filesDropped = Signal(list)
    tokenClicked = Signal(int)               # 단어 번호

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#E9EBEF"))
        self.setDragMode(QGraphicsView.NoDrag)
        self.setAcceptDrops(True)
        self.mode = MODE_SELECT
        self.zoom = 1.0
        self.page_rect = QRectF()
        self._page_item: QGraphicsPixmapItem | None = None
        self._items: dict[str, RegionItem] = {}
        self._marks: list[TokenItem] = []
        self._draw_start: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.scene().selectionChanged.connect(self._emit_selection)

    # ── 페이지와 영역 ──
    def set_page(self, image: QImage | None, page_rect: QRectF, render_scale: float) -> None:
        if self._page_item is not None:
            self.scene().removeItem(self._page_item)
            self._page_item = None
        self.page_rect = page_rect
        if image is not None:
            self._page_item = QGraphicsPixmapItem(QPixmap.fromImage(image))
            self._page_item.setTransformationMode(Qt.SmoothTransformation)
            self._page_item.setScale(1 / render_scale)
            self._page_item.setZValue(-10)
            self.scene().addItem(self._page_item)
        self.scene().setSceneRect(page_rect.adjusted(-20, -20, 20, 20))

    def set_regions(self, regions: list[Region], visible: bool = True) -> None:
        selected = set(self.selected_ids())
        self.scene().blockSignals(True)
        for item in self._items.values():
            self.scene().removeItem(item)
        self._items = {}
        for r in regions:
            item = RegionItem(r, self.regionEdited.emit)
            self.scene().addItem(item)
            item.refresh()
            item.setVisible(visible)
            item.setSelected(r.id in selected)
            self._items[r.id] = item
        self.scene().blockSignals(False)

    def set_marks(self, marks: list[tuple[list[float], str, str]], clickable: bool) -> None:
        """선택한 영역 안의 단어 표시. marks = [(bbox, 상태, 툴팁), ...]"""
        for m in self._marks:
            self.scene().removeItem(m)
        self._marks = []
        for i, (bbox, state, tip) in enumerate(marks):
            item = TokenItem(bbox, state, i, tip, self.tokenClicked.emit if clickable else None)
            self.scene().addItem(item)
            self._marks.append(item)

    def refresh_region(self, region_id: str) -> None:
        if region_id in self._items:
            self._items[region_id].refresh()

    def selected_ids(self) -> list[str]:
        return [i.region.id for i in self.scene().selectedItems() if isinstance(i, RegionItem)]

    def select_ids(self, ids: list[str]) -> None:
        self.scene().blockSignals(True)
        for rid, item in self._items.items():
            item.setSelected(rid in ids)
        self.scene().blockSignals(False)
        if ids and ids[0] in self._items:
            self.ensureVisible(self._items[ids[0]], 40, 40)

    def _emit_selection(self) -> None:
        self.selectionIds.emit(self.selected_ids())

    # ── 확대/축소 ──
    def set_zoom(self, z: float) -> None:
        self.zoom = max(0.25, min(z, 6.0))
        self.setTransform(QTransform.fromScale(self.zoom, self.zoom))
        for item in self._items.values():
            item.refresh()
        self.zoomChanged.emit(self.zoom)

    def fit_width(self) -> None:
        if self.page_rect.width() > 0:
            self.set_zoom((self.viewport().width() - 40) / self.page_rect.width())

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
            return
        super().wheelEvent(event)

    # ── 마우스로 영역 그리기 ──
    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.viewport().setCursor(Qt.ArrowCursor if mode == MODE_SELECT else Qt.CrossCursor)
        for item in self._items.values():
            item.setFlag(QGraphicsItem.ItemIsMovable, mode == MODE_SELECT)

    def mousePressEvent(self, event):
        if self.mode != MODE_SELECT and event.button() == Qt.LeftButton and not self.page_rect.isEmpty():
            self._draw_start = self.mapToScene(event.position().toPoint())
            self._rubber = QGraphicsRectItem()
            pen = QPen(COLORS[_MODE_KIND[self.mode]], 1.5, Qt.DashLine)
            pen.setCosmetic(True)
            self._rubber.setPen(pen)
            self.scene().addItem(self._rubber)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._draw_start is not None and self._rubber is not None:
            p = self.mapToScene(event.position().toPoint())
            self._rubber.setRect(QRectF(self._draw_start, p).normalized().intersected(self.page_rect))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._draw_start is not None and self._rubber is not None:
            r = self._rubber.rect()
            self.scene().removeItem(self._rubber)
            self._rubber, self._draw_start = None, None
            if r.width() >= 4 and r.height() >= 4:
                kind = _MODE_KIND[self.mode]
                self.regionDrawn.emit(kind, [round(r.left(), 2), round(r.top(), 2),
                                             round(r.right(), 2), round(r.bottom(), 2)])
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected_ids():
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    # ── 파일 끌어다 놓기 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.filesDropped.emit([u.toLocalFile() for u in event.mimeData().urls()])
