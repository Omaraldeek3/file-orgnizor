"""Qt preview model: editing a client is an explicit confirmation."""

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate

from .core import Client, ScanResult


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{size:,} B"
        value /= 1024
    return ""


class PreviewModel(QAbstractTableModel):
    selection_changed = Signal()
    HEADERS = ("", "الملف", "النوع", "العميل", "الشهر", "الحجم", "المطابقة", "المسار المقترح")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scan: ScanResult | None = None
        self.clients: dict[int, Client] = {}
        self.checked: set[int] = set()
        self.manual: set[int] = set()

    def load(self, scan: ScanResult | None, clients: list[Client]):
        self.beginResetModel()
        self.scan = scan
        self.clients = {client.id: client for client in clients}
        self.checked = (
            {i for i, item in enumerate(scan.items) if item.client_id is not None} if scan else set()
        )
        self.manual = set()
        self.endResetModel()
        self.selection_changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.scan is None else len(self.scan.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.scan is None:
            return None
        row, col = index.row(), index.column()
        item = self.scan.items[row]
        client = self.clients.get(item.client_id)
        suggested = self.clients.get(item.suggested_client_id)
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if row in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.EditRole and col == 3:
            return item.client_id
        if role == Qt.ItemDataRole.UserRole:
            return str(item.source)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 1:
                return str(item.source)
            if col == 3:
                return "انقر لاختيار العميل أو تصحيح المطابقة"
            if col == 6:
                return f"{item.reason}\nدرجة التشابه: {item.confidence:.0%} (ليست احتمالاً إحصائياً)"
            if col == 7 and client:
                return str(self.scan.destination / client.name / item.month / item.source.name)
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 2:
                return QColor(
                    {".psd": "#93c6f4", ".psb": "#93c6f4", ".ai": "#f0c88a"}.get(
                        item.source.suffix.lower(), "#a9db9a"
                    )
                )
            if col in (3, 6):
                return QColor("#b0d99b" if client else "#e3bf85")
            if col == 7:
                return QColor("#91a7b0")
        if role == Qt.ItemDataRole.FontRole and col in (1, 2):
            font = QFont()
            font.setWeight(QFont.Weight.DemiBold)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (2, 4, 5):
                return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.DisplayRole:
            match_text = "اختيار يدوي" if row in self.manual else item.reason
            if not client and suggested:
                match_text = f"اقتراح: {suggested.name}"
            return (
                "",
                item.source.name,
                item.source.suffix[1:].upper(),
                client.name if client else "اختر العميل...",
                item.month,
                format_size(item.size),
                match_text,
                str(Path(client.name) / item.month / item.source.name) if client else "بانتظار تحديد العميل",
            )[col]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if not index.isValid() or self.scan is None:
            return flags
        if index.column() == 0 and self.scan.items[index.row()].client_id in self.clients:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() == 3:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or self.scan is None:
            return False
        row = index.row()
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            if self.scan.items[row].client_id not in self.clients:
                return False
            if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
                self.checked.add(row)
            else:
                self.checked.discard(row)
        elif role == Qt.ItemDataRole.EditRole and index.column() == 3:
            if value is not None and value not in self.clients:
                return False
            self.scan.items[row].client_id = value
            self.manual.add(row)
            if value is None:
                self.checked.discard(row)
            else:
                self.checked.add(row)
        else:
            return False
        self.dataChanged.emit(self.index(row, 0), self.index(row, 7))
        self.selection_changed.emit()
        return True

    def select_ready(self, selected: bool):
        self.checked = (
            {row for row, item in enumerate(self.scan.items) if item.client_id in self.clients}
            if selected and self.scan
            else set()
        )
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 0))
        self.selection_changed.emit()


class PreviewFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.kind = "all"
        self.status = "all"

    def set_filters(self, query: str, kind: str, status: str):
        self.beginFilterChange()
        self.query, self.kind, self.status = query.casefold().strip(), kind, status
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        if model.scan is None:
            return False
        item = model.scan.items[row]
        if self.kind != "all" and item.source.suffix.lower() not in self.kind.split(","):
            return False
        if self.status == "review" and item.client_id is not None:
            return False
        if self.status == "ready" and item.client_id is None:
            return False
        return (
            not self.query
            or self.query
            in " ".join(str(model.data(model.index(row, col)) or "") for col in (1, 3, 4, 6, 7)).casefold()
        )


class ClientDelegate(QStyledItemDelegate):
    editing_started = Signal()

    def __init__(self, model: PreviewModel, parent=None):
        super().__init__(parent)
        self.preview_model = model

    def createEditor(self, parent, option, index):
        self.editing_started.emit()
        combo = QComboBox(parent)
        combo.addItem("بدون عميل / تجاهل", None)
        for client in self.preview_model.clients.values():
            combo.addItem(client.name, client.id)
        combo.activated.connect(lambda: self.commitData.emit(combo))
        combo.activated.connect(lambda: self.closeEditor.emit(combo))
        return combo

    def setEditorData(self, editor, index):
        editor.setCurrentIndex(max(0, editor.findData(index.data(Qt.ItemDataRole.EditRole))))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.ItemDataRole.EditRole)
