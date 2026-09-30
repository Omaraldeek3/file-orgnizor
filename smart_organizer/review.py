"""Small, explicit confirmation steps that teach future automatic runs."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .automation import suggest_alias


def text(value, name="muted"):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


class ResolveDialog(QDialog):
    def __init__(self, store, scan, item, parent=None):
        super().__init__(parent)
        self.store = store
        self.client_id = None
        self.learned = False
        self.setWindowTitle("تأكيد العميل وتعلّم الاسم")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 24, 25, 24)
        layout.setSpacing(15)
        layout.addWidget(text("قرار واحد. ترتيب أذكى في المرة القادمة.", "sectionTitle"))
        layout.addWidget(text(item.source.name))
        form = QFormLayout()
        form.setSpacing(12)
        self.client_combo = QComboBox()
        for client in store.clients():
            self.client_combo.addItem(client.name, client.id)
        suggested = item.client_id or item.suggested_client_id
        self.client_combo.setCurrentIndex(self.client_combo.findData(suggested))
        self.client_combo.setPlaceholderText("اختر العميل الصحيح")
        form.addRow("العميل", self.client_combo)
        self.alias_input = QLineEdit(suggest_alias(item, scan.source))
        self.alias_input.setPlaceholderText("الجزء الذي يدل على العميل، وليس اسم التصميم")
        self.alias_input.setMaxLength(240)
        form.addRow("الاسم الذي نتذكره", self.alias_input)
        layout.addLayout(form)
        self.learn_check = QCheckBox("تذكّر هذا الاسم ونظّم الملفات المطابقة له مستقبلاً")
        self.learn_check.setChecked(bool(self.alias_input.text()))
        layout.addWidget(self.learn_check)
        layout.addWidget(
            text(
                "راجع الاسم المقترح. سيصبح قاعدة مطابقة تلقائية؛ تجنب كلمات عامة مثل logo أو تصميم.",
                "notice",
            )
        )
        self.error = text("", "notice")
        self.error.setProperty("warning", True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        accept = buttons.addButton("تأكيد الاختيار", QDialogButtonBox.ButtonRole.AcceptRole)
        accept.setProperty("primary", True)
        buttons.addButton("إلغاء", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        client_id = self.client_combo.currentData()
        if client_id is None:
            self.error.setText("اختر العميل أولاً.")
            self.error.show()
            return
        try:
            if self.learn_check.isChecked():
                alias = self.alias_input.text().strip()
                if len(alias) < 3 or not any(char.isalpha() for char in alias):
                    raise ValueError("اكتب اسماً مميزاً من ثلاثة أحرف على الأقل، أو ألغِ تذكر الاسم.")
                self.store.add_alias(client_id, alias)
                self.learned = True
            self.client_id = client_id
        except Exception as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


class DiscoveryDialog(QDialog):
    def __init__(self, store, candidates, parent=None):
        super().__init__(parent)
        self.store = store
        self.saved_count = 0
        self.saved_rows = set()
        self.original_names = [candidate.name for candidate in candidates]
        self.setWindowTitle("اكتشاف العملاء من ملفاتك")
        self.resize(800, 550)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(text("أسماء وجدناها في مساحة عملك", "sectionTitle"))
        layout.addWidget(
            text(
                "هذه اقتراحات من المجلدات وأسماء الملفات، وليست عملاء مؤكدين. "
                "اختر عميلاً موجوداً لربط الاسم به، أو اترك «عميل جديد» لإنشائه. "
                "راجع الأسماء وألغِ غير الصحيح ثم احفظ. لن يُنقل أي ملف الآن."
            )
        )
        self.table = QTableWidget(len(candidates), 4)
        self.table.setHorizontalHeaderLabels(
            ("حفظ", "الاسم / الاسم البديل", "العميل / مجلد الوجهة", "الملفات")
        )
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(49)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(3, 65)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        clients = store.clients()
        for row, candidate in enumerate(candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(candidate.name))
            target = QComboBox()
            target.addItem("عميل جديد", None)
            for client in clients:
                target.addItem(client.name, client.id)
            self.table.setCellWidget(row, 2, target)
            count = QTableWidgetItem(str(candidate.files))
            count.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 3, count)
        layout.addWidget(self.table, 1)
        self.error = text("", "notice")
        self.error.setProperty("warning", True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        save = buttons.addButton("حفظ الأسماء والروابط المحددة", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty("primary", True)
        buttons.addButton("إلغاء", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        errors = []
        for row in range(self.table.rowCount()):
            if row in self.saved_rows or self.table.item(row, 0).checkState() != Qt.CheckState.Checked:
                continue
            name = self.table.item(row, 1).text().strip()
            try:
                original = self.original_names[row]
                target_id = self.table.cellWidget(row, 2).currentData()
                if target_id is None:
                    self.store.save_client(name, [original] if original != name else [])
                else:
                    self.store.add_alias(target_id, name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            self.saved_count += 1
            self.saved_rows.add(row)
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
            for column in (0, 1, 3):
                self.table.item(row, column).setFlags(Qt.ItemFlag.NoItemFlags)
            self.table.cellWidget(row, 2).setEnabled(False)
        if errors:
            self.error.setText(f"حُفظ {self.saved_count} اسم. صحح الباقي أو ألغِ تحديده:\n" + "\n".join(errors))
            self.error.show()
        else:
            self.accept()
