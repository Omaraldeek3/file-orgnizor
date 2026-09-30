"""Arabic desktop interface and cancellable background jobs."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .automation import AutoCycle, AutoOrganizer, AutoSettings, discover_clients
from .core import BatchResult, Client, Organizer, ScanResult, Store
from .models import ClientDelegate, PreviewFilter, PreviewModel, format_size
from .review import DiscoveryDialog, ResolveDialog
from .theme import app_icon


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, callback: Callable, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setProperty("primary", primary)
    widget.clicked.connect(callback)
    return widget


def panel() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 17, 20, 17)
    layout.setSpacing(12)
    return frame, layout


def configure_table(table: QTableView):
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(49)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setSortingEnabled(False)
    table.horizontalHeader().setHighlightSections(False)
    table.horizontalHeader().setMinimumSectionSize(40)


class Worker(QObject):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal()

    def __init__(self, job: Callable):
        super().__init__()
        self.job = job

    @Slot()
    def run(self):
        try:
            result = self.job(QThread.currentThread().isInterruptionRequested, self.progress.emit)
            self.result.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
        finally:
            self.finished.emit()


class FolderEdit(QLineEdit):
    folder_dropped = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).is_dir():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).is_dir():
            self.setText(urls[0].toLocalFile())
            self.folder_dropped.emit()
            event.acceptProposedAction()
        else:
            event.ignore()


class ClientDialog(QDialog):
    def __init__(self, store: Store, client: Client | None = None, parent=None):
        super().__init__(parent)
        self.store, self.client = store, client
        self.setWindowTitle("تعديل العميل" if client else "إضافة عميل")
        self.setMinimumWidth(490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)
        layout.addWidget(label("هوية واحدة، بأكثر من اسم", "sectionTitle"))
        layout.addWidget(
            label(
                "اربط الاسم العربي والإنجليزي والاختصارات بالعميل نفسه. سيصبح الاسم الرئيسي اسم مجلد العميل.",
                "muted",
                True,
            )
        )
        form = QFormLayout()
        form.setSpacing(12)
        self.name_input = QLineEdit(client.name if client else "")
        self.name_input.setPlaceholderText("مثال: مطبعة النور")
        self.name_input.setMaxLength(100)
        self.aliases_input = QPlainTextEdit("\n".join(client.aliases) if client else "")
        self.aliases_input.setPlaceholderText("Al Noor\nAlnoor\nالنور\nEl Nour")
        self.aliases_input.setFixedHeight(130)
        form.addRow("الاسم الرئيسي", self.name_input)
        form.addRow("الأسماء البديلة\nاسم في كل سطر", self.aliases_input)
        layout.addLayout(form)
        layout.addWidget(
            label("اختر أسماء مميزة؛ الاسم المشترك بين عميلين يسبب مطابقة ملتبسة.", "muted", True)
        )
        self.error = label("", "notice", True)
        self.error.setProperty("warning", True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        save = buttons.addButton("حفظ العميل", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty("primary", True)
        buttons.addButton("إلغاء", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def save(self):
        aliases = self.aliases_input.toPlainText().replace("،", "\n").replace(",", "\n").splitlines()
        try:
            self.store.save_client(
                self.name_input.text().strip(),
                [a.strip() for a in aliases if a.strip()],
                client_id=self.client.id if self.client else None,
            )
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        except Exception as exc:
            self.error.setText(f"تعذر حفظ العميل: {exc}")
            self.error.show()
            return
        self.accept()


class MainWindow(QMainWindow):
    automation_cycle_finished = Signal(object)

    def __init__(self, store: Store, data_dir: Path):
        super().__init__()
        self.store, self.data_dir = store, data_dir
        self.organizer = Organizer(store)
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.result_handler: Callable | None = None
        self.last_errors: list[str] = []
        self._history: list[dict] = []
        self.auto_enabled = False
        self.auto_engine: AutoOrganizer | None = None
        self.auto_processed = 0
        self.auto_waiting = 0
        self._job_is_auto = False
        self._close_pending = False
        self._force_quit = False
        self._after_task: Callable | None = None
        self.automation_timer = QTimer(self)
        self.automation_timer.setSingleShot(True)
        self.automation_timer.setInterval(4000)
        self.automation_timer.timeout.connect(self.run_auto_cycle)
        self.setWindowTitle("Smart File Organizer | منظم ملفات التصميم")
        self.setWindowIcon(app_icon())
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        available = self.screen().availableGeometry()
        self.resize(min(1380, available.width() - 40), min(900, available.height() - 60))
        self.setMinimumSize(960, 640)
        self.model = PreviewModel(self)
        self.proxy = PreviewFilter(self)
        self.proxy.setSourceModel(self.model)
        self._build()
        self._load_settings()
        self.refresh_clients()
        self.refresh_history()
        self.model.selection_changed.connect(self.update_summary)
        self.update_summary()
        self._setup_tray()

    def _build(self):
        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 26, 18, 22)
        side.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(45, 45))
        side.addWidget(logo)
        side.addSpacing(7)
        side.addWidget(label("منظّم الاستوديو", "brand"))
        side.addWidget(label("SMART ORGANIZER", "brandSub"))
        side.addSpacing(42)
        side.addWidget(label("مساحة العمل", "sideLabel"))
        self.nav_buttons = []
        for index, text in enumerate(("المساعد التلقائي", "العملاء والذاكرة", "النشاط والاستعادة")):
            nav = button(text, lambda checked=False, i=index: self.navigate(i))
            nav.setCheckable(True)
            nav.setAutoExclusive(True)
            side.addWidget(nav)
            self.nav_buttons.append(nav)
        self.nav_buttons[0].setChecked(True)
        side.addStretch()
        side.addWidget(label("مصمم لملفات التصميم", "sideNote"))
        side.addWidget(label("CDR / PSD / AI / PSB / CDT", "sideLabel"))
        side.addSpacing(24)
        side.addWidget(button("دليل الاستخدام", self.show_help))
        side.addSpacing(9)
        side.addWidget(label("محلي بالكامل • بدون رفع ملفات", "sideNote", True))
        side.addWidget(label("LOCAL AUTOMATION", "sideLabel"))
        root_layout.addWidget(sidebar)
        content = QVBoxLayout()
        content.setContentsMargins(26, 22, 26, 16)
        content.setSpacing(12)
        root_layout.addLayout(content, 1)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("LESS SORTING. MORE CREATING.", "eyebrow"))
        self.title = label("مساحة لعملك، لا لفوضى ملفاتك.", "pageTitle")
        self.subtitle = label("مساعد يراقب، يرتب، ويتذكر اختياراتك. كل شيء على جهازك.", "subtitle")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.background_button = button("للخلفية", self.hide_to_tray)
        self.background_button.setProperty("quiet", True)
        header.addWidget(self.background_button)
        header.addWidget(label("خصوصية محلية", "chip"), 0, Qt.AlignmentFlag.AlignVCenter)
        content.addLayout(header)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._organizer_page())
        self.pages.addWidget(self._clients_page())
        self.pages.addWidget(self._history_page())
        content.addWidget(self.pages, 1)
        self.notice = label(
            "اختر أو اسحب مجلد المصدر. اكتشف العملاء مرة واحدة، ثم دع المساعد يتولى الباقي.", "notice", True
        )
        content.addWidget(self.notice)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        self.progress_label = label("", "muted")
        self.cancel_button = button("إلغاء العملية", self.cancel_task)
        self.cancel_button.hide()
        self.details_button = button("عرض التفاصيل", self.show_errors)
        self.details_button.hide()
        progress_row.addWidget(self.progress_label, 1)
        progress_row.addWidget(self.details_button)
        progress_row.addWidget(self.cancel_button)
        content.addWidget(self.progress)
        content.addLayout(progress_row)

    def _organizer_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("organizerPage")
        page.setMinimumHeight(540)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        hero, hero_layout = panel()
        hero.setObjectName("autoHero")
        hero_layout.setContentsMargins(22, 12, 22, 12)
        hero_top = QHBoxLayout()
        hero_top.addWidget(label("اترك الترتيب علينا.", "autoTitle"))
        self.auto_badge = label("متوقف", "autoStatus")
        hero_top.addWidget(self.auto_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        hero_top.addStretch()
        self.auto_button = button("تشغيل المساعد", self.toggle_automation, primary=True)
        self.auto_button.setObjectName("autoButton")
        self.auto_button.setMinimumWidth(190)
        hero_top.addWidget(self.auto_button)
        hero_layout.addLayout(hero_top)
        self.auto_description = label(
            "يراقب مجلدك وينظّم الملفات المعروفة تلقائياً. يعرض ما يحتاج رأيك فقط.",
            "autoDescription",
            True,
        )
        hero_layout.addWidget(self.auto_description)
        layout.addWidget(hero)
        config, config_layout = panel()
        config_layout.setContentsMargins(17, 10, 17, 10)
        heading = QHBoxLayout()
        heading.addWidget(label("مسار العمل", "sectionTitle"))
        heading.addStretch()
        self.options_button = button("خيارات التنظيم", self.toggle_options)
        self.options_button.setProperty("quiet", True)
        heading.addWidget(self.options_button)
        config_layout.addLayout(heading)
        self.source_input = FolderEdit()
        self.source_input.setObjectName("sourceInput")
        self.destination_input = FolderEdit()
        self.destination_input.setObjectName("destinationInput")
        self.browse_buttons = []
        routes = QHBoxLayout()
        routes.setSpacing(18)
        for title, field, placeholder in (
            ("01 / من هنا", self.source_input, "اسحب مجلد العمل أو اختره..."),
            ("02 / إلى هنا", self.destination_input, "الأرشيف / العميل / الشهر"),
        ):
            column = QVBoxLayout()
            column.setSpacing(5)
            column.addWidget(label(title, "routeLabel"))
            row = QHBoxLayout()
            field.setPlaceholderText(placeholder)
            browse = button("اختيار", lambda checked=False, f=field: self.browse(f))
            self.browse_buttons.append(browse)
            row.addWidget(field, 1)
            row.addWidget(browse)
            column.addLayout(row)
            routes.addLayout(column, 1)
            field.textChanged.connect(self.invalidate_preview)
        self.source_input.folder_dropped.connect(self.suggest_destination)
        self.source_input.editingFinished.connect(self.suggest_destination)
        config_layout.addLayout(routes)
        self.options_panel = QWidget()
        options = QVBoxLayout(self.options_panel)
        options.setContentsMargins(0, 3, 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("نسخ آمن (الافتراضي)", "copy")
        self.mode_combo.addItem("نقل الملفات الأصلية", "move")
        self.date_combo = QComboBox()
        self.date_combo.addItem("الشهر من الاسم أو التعديل", "filename")
        self.date_combo.addItem("آخر تعديل فقط", "modified")
        self.date_combo.currentIndexChanged.connect(self.invalidate_preview)
        self.recursive_check = QCheckBox("المجلدات الفرعية")
        self.recursive_check.setChecked(True)
        self.recursive_check.toggled.connect(self.invalidate_preview)
        self.settle_spin = QSpinBox()
        self.settle_spin.setRange(3, 120)
        self.settle_spin.setValue(8)
        self.settle_spin.setSuffix(" ثوانٍ")
        self.settle_spin.setToolTip("مدة ثبات حجم الملف وتاريخه قبل تنظيمه تلقائياً")
        mode_options = QHBoxLayout()
        mode_options.addWidget(self.mode_combo, 1)
        mode_options.addWidget(self.date_combo, 1)
        options.addLayout(mode_options)
        timing_options = QHBoxLayout()
        timing_options.addWidget(self.recursive_check)
        timing_options.addStretch()
        timing_options.addWidget(label("استقرار الملف", "muted"))
        timing_options.addWidget(self.settle_spin)
        options.addLayout(timing_options)
        config_layout.addWidget(self.options_panel)
        self.options_panel.hide()
        layout.addWidget(config)
        strip = QFrame()
        strip.setObjectName("statStrip")
        stats = QHBoxLayout(strip)
        stats.setContentsMargins(19, 7, 19, 7)
        stats.setSpacing(24)
        self.stat_values, self.stat_labels = [], []
        for caption in ("في المعاينة", "جاهز للتنظيم", "يحتاج قرارك"):
            stat = QHBoxLayout()
            value = label("0", "statValue")
            caption_label = label(caption, "statCaption")
            self.stat_values.append(value)
            self.stat_labels.append(caption_label)
            stat.addWidget(value)
            stat.addWidget(caption_label)
            stat.addStretch()
            stats.addLayout(stat, 1)
        self.last_check_label = label("المساعد لم يبدأ بعد", "muted")
        stats.addWidget(self.last_check_label)
        layout.addWidget(strip)
        preview, preview_layout = panel()
        preview_layout.setContentsMargins(0, 12, 0, 10)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(16, 0, 16, 0)
        self.review_title = label("صندوق المراجعة", "sectionTitle")
        toolbar.addWidget(self.review_title)
        toolbar.addStretch()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("بحث في الملفات...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaximumWidth(180)
        self.search_input.textChanged.connect(self.filter_preview)
        self.type_filter = QComboBox()
        for title, value in (
            ("كل الأنواع", "all"),
            ("CorelDRAW", ".cdr,.cdt"),
            ("Photoshop", ".psd,.psb"),
            ("Illustrator", ".ai"),
        ):
            self.type_filter.addItem(title, value)
        self.status_filter = QComboBox()
        for title, value in (("كل الحالات", "all"), ("تحتاج مراجعة", "review"), ("جاهزة", "ready")):
            self.status_filter.addItem(title, value)
        self.type_filter.currentIndexChanged.connect(self.filter_preview)
        self.status_filter.currentIndexChanged.connect(self.filter_preview)
        toolbar.addWidget(self.search_input)
        toolbar.addWidget(self.type_filter)
        self.status_filter.hide()
        self.scan_button = button("فحص الآن", self.start_scan)
        toolbar.addWidget(self.scan_button)
        preview_layout.addLayout(toolbar)
        self.preview_stack = QStackedWidget()
        self.preview_stack.setMinimumHeight(110)
        self.preview_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        empty_icon = QLabel()
        empty_icon.setPixmap(app_icon().pixmap(56, 56))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_icon)
        self.empty_title = label("أضف مجلدك. والباقي علينا.", "emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_subtitle = label(
            "شغّل المساعد للترتيب المستمر، أو افحص الآن لاكتشاف العملاء ومعاينة الملفات.", "muted", True
        )
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_subtitle)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty)
        self.table = QTableView()
        self.table.setObjectName("previewTable")
        configure_table(self.table)
        self.table.setModel(self.proxy)
        delegate = ClientDelegate(self.model, self.table)
        delegate.editing_started.connect(self.stop_automation)
        self.table.setItemDelegateForColumn(3, delegate)
        self.table.clicked.connect(self.edit_client_cell)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        for col, width in enumerate((38, 250, 65, 150, 86, 78, 210, 220)):
            self.table.setColumnWidth(col, width)
        self.table.setColumnHidden(7, True)
        self.table.setColumnHidden(5, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_stack.addWidget(self.table)
        preview_layout.addWidget(self.preview_stack, 1)
        table_footer = QHBoxLayout()
        table_footer.setContentsMargins(16, 0, 16, 0)
        self.discover_button = button("اكتشاف العملاء", self.start_discovery)
        self.learn_button = button("تأكيد وتعلّم", self.resolve_selected)
        self.learn_button.setToolTip("اختر ملفاً ثم أكّد العميل واحفظ اسمه البديل للمرة القادمة")
        self.retry_button = button("إعادة المحاولة", self.retry_automation)
        self.retry_button.hide()
        self.select_button = button("تحديد الكل", lambda: self.model.select_ready(True))
        self.clear_button = button("إلغاء", lambda: self.model.select_ready(False))
        self.select_button.setProperty("quiet", True)
        self.clear_button.setProperty("quiet", True)
        table_footer.addWidget(self.discover_button)
        table_footer.addWidget(self.learn_button)
        table_footer.addWidget(self.retry_button)
        table_footer.addStretch()
        table_footer.addWidget(self.select_button)
        table_footer.addWidget(self.clear_button)
        preview_layout.addLayout(table_footer)
        layout.addWidget(preview, 1)
        footer = QHBoxLayout()
        self.selection_label = label("لا توجد ملفات محددة", "muted")
        footer.addWidget(self.selection_label, 1)
        self.open_destination_button = button("فتح الوجهة", self.open_destination)
        footer.addWidget(self.open_destination_button)
        self.execute_button = button("تنظيم الملفات المحددة", self.start_execute, primary=True)
        self.execute_button.setMinimumWidth(200)
        footer.addWidget(self.execute_button)
        self.setup_button = button("اكتشاف وربط العملاء", self.start_discovery, primary=True)
        self.setup_button.setMinimumWidth(200)
        self.setup_button.setToolTip("راجع الأسماء المكتشفة واربطها بعملائك قبل بدء التنظيم")
        footer.addWidget(self.setup_button)
        self.table.selectionModel().selectionChanged.connect(self.update_review_actions)
        self.organizer_scroll = QScrollArea()
        self.organizer_scroll.setWidgetResizable(True)
        self.organizer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.organizer_scroll.setWidget(page)
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(12)
        wrapper_layout.addWidget(self.organizer_scroll, 1)
        wrapper_layout.addLayout(footer)
        return wrapper

    def _clients_page(self) -> QWidget:
        page, layout = panel()
        row = QHBoxLayout()
        row.addWidget(label("دليل العملاء", "sectionTitle"), 1)
        self.add_client_button = button("+  إضافة عميل", self.add_client, primary=True)
        self.edit_client_button = button("تعديل", self.edit_client)
        self.delete_client_button = button("حذف", self.delete_client)
        self.delete_client_button.setProperty("danger", True)
        for widget in (self.add_client_button, self.edit_client_button, self.delete_client_button):
            row.addWidget(widget)
        layout.addLayout(row)
        layout.addWidget(
            label(
                "مثال: «مطبعة النور» + «Al Noor» + «النور» = مجلد عميل واحد. "
                "الاختلاف الإملائي أو النقل الصوتي يظهر كاقتراح يحتاج تأكيدك.",
                "notice",
                True,
            )
        )
        self.clients_table = QTableWidget(0, 2)
        self.clients_table.setHorizontalHeaderLabels(("العميل / اسم المجلد", "الأسماء البديلة"))
        configure_table(self.clients_table)
        self.clients_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.clients_table.setColumnWidth(0, 250)
        self.clients_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.clients_table.doubleClicked.connect(self.edit_client)
        self.clients_table.itemSelectionChanged.connect(self.update_client_actions)
        layout.addWidget(self.clients_table, 1)
        self.client_count = label("", "muted")
        layout.addWidget(self.client_count)
        layout.addWidget(
            label(
                "تعديل اسم العميل يؤثر في العمليات المقبلة فقط؛ لا يعيد تسمية المجلدات الموجودة.",
                "muted",
                True,
            )
        )
        return page

    def _history_page(self) -> QWidget:
        page, layout = panel()
        row = QHBoxLayout()
        row.addWidget(label("سجل قابل للتتبع", "sectionTitle"), 1)
        self.refresh_history_button = button("تحديث", self.refresh_history)
        self.history_details_button = button("تفاصيل العملية", self.show_history_details)
        self.undo_button = button("التراجع عن العملية", self.start_undo)
        for widget in (self.refresh_history_button, self.history_details_button, self.undo_button):
            row.addWidget(widget)
        layout.addLayout(row)
        layout.addWidget(
            label(
                "التراجع يتحقق من بصمة الملف أولاً. لن يُحذف ملف تغير بعد التنظيم، ولن يُستبدل ملف موجود في المصدر.",
                "notice",
                True,
            )
        )
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(
            ("التاريخ", "الوضع", "نجح", "تعذر", "تم التراجع", "الحالة")
        )
        configure_table(self.history_table)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self.update_history_actions)
        self.history_table.doubleClicked.connect(self.show_history_details)
        layout.addWidget(self.history_table, 1)
        layout.addWidget(label("آخر 100 عملية • يشمل الملفات المنسوخة والمنقولة والأخطاء", "muted"))
        return page

    def _load_settings(self):
        self.source_input.setText(self.store.get_setting("source"))
        self.destination_input.setText(self.store.get_setting("destination"))
        index = self.date_combo.findData(self.store.get_setting("date_mode", "filename"))
        self.date_combo.setCurrentIndex(max(index, 0))
        self.recursive_check.setChecked(self.store.get_setting("recursive", "true") == "true")
        try:
            self.settle_spin.setValue(int(self.store.get_setting("settle_seconds", "8")))
        except ValueError:
            self.settle_spin.setValue(8)

    def save_settings(self):
        for key, value in (
            ("source", self.source_input.text().strip()),
            ("destination", self.destination_input.text().strip()),
            ("date_mode", self.date_combo.currentData()),
            ("recursive", "true" if self.recursive_check.isChecked() else "false"),
            ("settle_seconds", str(self.settle_spin.value())),
        ):
            self.store.set_setting(key, value)

    @Slot(int)
    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        self.title.setText(
            ("مساحة لعملك، لا لفوضى ملفاتك.", "كل عميل، بكل أسمائه.", "نشاط واضح. وتراجع آمن.")[index]
        )
        self.subtitle.setText(
            (
                "مساعد يراقب، يرتب، ويتذكر اختياراتك. كل شيء على جهازك.",
                "اكتشف العملاء من ملفاتك أو علّم المساعد الاسم الصحيح مرة واحدة.",
                "راجع ما تم تنظيمه، وتراجع بأمان عندما تحتاج.",
            )[index]
        )
        if index == 2:
            self.refresh_history()

    @Slot()
    def invalidate_preview(self, *_):
        if self.auto_enabled:
            self.stop_automation()
        if self.model.scan is not None:
            self.model.load(None, self.store.clients())
            self.preview_stack.setCurrentIndex(0)
            self.set_notice("تغيرت الإعدادات أو بيانات العملاء. أعد الفحص لتحديث المعاينة.")

    def browse(self, field: QLineEdit):
        directory = QFileDialog.getExistingDirectory(self, "اختيار مجلد", field.text() or str(Path.home()))
        if directory:
            field.setText(directory)
            if field is self.source_input:
                self.suggest_destination()

    @Slot()
    def suggest_destination(self):
        source = Path(self.source_input.text().strip()).expanduser()
        if self.source_input.text().strip() and source.is_dir() and not self.destination_input.text().strip():
            self.destination_input.setText(str(source / "Organized"))

    @Slot()
    def toggle_options(self):
        self.options_panel.setVisible(self.options_panel.isHidden())

    def _setup_tray(self):
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip("Smart Organizer - المساعد متوقف")
            menu = QMenu(self)
            menu.addAction("فتح المنظم", self.restore_window)
            self.tray_pause = menu.addAction("إيقاف المراقبة", self.stop_automation)
            menu.addSeparator()
            menu.addAction("إنهاء التطبيق", self.quit_application)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
        self.background_button.setEnabled(self.tray is not None)

    @Slot()
    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_window()

    @Slot()
    def hide_to_tray(self):
        if self.tray is not None:
            self.hide()
            self.tray.showMessage(
                "Smart Organizer",
                "المساعد يواصل مراقبة مجلدك. افتح النافذة أو أوقفه من هذه الأيقونة."
                if self.auto_enabled
                else "النافذة مخفية. المساعد متوقف حتى تشغله بنفسك.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    @Slot()
    def quit_application(self):
        self._force_quit = True
        self.close()

    @Slot()
    def toggle_automation(self):
        if self.auto_enabled:
            self.stop_automation()
            return
        if self.thread is not None:
            return
        self.suggest_destination()
        if not self.source_input.text().strip() or not self.destination_input.text().strip():
            self.set_notice("اختر مجلد المصدر والوجهة لتشغيل المساعد.", warning=True)
            return
        if not self.store.clients():
            self.start_discovery()
            return
        mode = self.mode_combo.currentData()
        settings = AutoSettings(
            source=Path(self.source_input.text().strip()).expanduser(),
            destination=Path(self.destination_input.text().strip()).expanduser(),
            mode=mode,
            date_mode=self.date_combo.currentData(),
            recursive=self.recursive_check.isChecked(),
            settle_seconds=float(self.settle_spin.value()),
        )
        verb = "نسخ" if mode == "copy" else "نقل وحذف الأصول بعد التحقق"
        if not self.confirm(
            "تشغيل التنظيم التلقائي",
            f"سيتم {verb} للملفات ذات العميل المؤكد، دون تأكيد جديد لكل دفعة.\n\n"
            f"المصدر: {settings.source}\nالوجهة: {settings.destination}\n\n"
            f"سيُنتظر ثبات الملف {self.settle_spin.value()} ثوانٍ على الأقل. "
            "الحالات الملتبسة تبقى للمراجعة ولا تُنظّم تلقائياً.\n"
            "لن تُستبدل الملفات الموجودة. زر الإيقاف أو إغلاق التطبيق يوقف المراقبة.\n"
            "زر «للخلفية» يخفي النافذة ويُبقي المساعد يعمل.",
        ):
            return
        try:
            self.save_settings()
            self.auto_engine = AutoOrganizer(self.store, settings)
        except Exception as exc:
            self.on_task_error(str(exc))
            return
        self.auto_enabled = True
        self.auto_processed = 0
        self.auto_waiting = 0
        self.model.load(None, self.store.clients())
        self.set_busy(False)
        self.update_auto_status()
        self.set_notice("المساعد يعمل. سيظهر هنا فقط ما يحتاج تأكيد العميل أو معالجة خطأ.")
        self.run_auto_cycle()

    @Slot()
    def stop_automation(self):
        was_active = self.auto_enabled
        self.auto_enabled = False
        self.automation_timer.stop()
        if self.thread is not None and self._job_is_auto:
            self.thread.requestInterruption()
        self.update_auto_status()
        self.set_busy(self.thread is not None)
        if was_active:
            self.set_notice("توقفت المراقبة. يمكنك المراجعة أو تعديل الإعدادات، ثم تشغيل المساعد مجدداً.")

    def update_auto_status(self):
        self.auto_badge.setText("مراقبة نشطة" if self.auto_enabled else "متوقف")
        self.auto_badge.setProperty("active", self.auto_enabled)
        self.auto_badge.style().unpolish(self.auto_badge)
        self.auto_badge.style().polish(self.auto_badge)
        self.auto_button.setText("إيقاف المساعد" if self.auto_enabled else "تشغيل المساعد")
        self.auto_description.setText(
            "المطابقات تُنظّم تلقائياً. الملفات قيد الحفظ تنتظر، والحالات غير المؤكدة تظهر أدناه."
            if self.auto_enabled
            else "يراقب مجلدك وينظّم الملفات المعروفة تلقائياً. يعرض ما يحتاج رأيك فقط."
        )
        if getattr(self, "tray", None) is not None:
            self.tray.setToolTip("Smart Organizer - " + ("المراقبة نشطة" if self.auto_enabled else "متوقف"))
            self.tray_pause.setEnabled(self.auto_enabled)

    @Slot()
    def run_auto_cycle(self):
        if not self.auto_enabled or self.thread is not None or self.auto_engine is None:
            return
        engine = self.auto_engine
        self.run_task(
            lambda cancel, progress: engine.cycle(cancel=cancel, progress=progress),
            self.auto_cycle_done,
            "يتحقق المساعد من الملفات الجديدة...",
            automation=True,
        )

    def auto_cycle_done(self, result: AutoCycle):
        self.auto_processed += result.processed
        self.auto_waiting = result.waiting
        self.update_auto_status()
        row = self.selected_preview_row()
        selected_source = self.model.scan.items[row].source if row is not None and self.model.scan else None
        self.model.load(result.scan, self.store.clients())
        if selected_source is not None:
            for row, item in enumerate(result.scan.items):
                if item.source == selected_source:
                    index = self.proxy.mapFromSource(self.model.index(row, 1))
                    if index.isValid():
                        self.table.selectionModel().setCurrentIndex(
                            index,
                            QItemSelectionModel.SelectionFlag.ClearAndSelect
                            | QItemSelectionModel.SelectionFlag.Rows,
                        )
                    break
        self.preview_stack.setCurrentIndex(1 if result.scan.items else 0)
        if result.errors:
            self.empty_title.setText("لم يكتمل الفحص دون تنبيهات")
            self.empty_subtitle.setText(
                "افتح «عرض التفاصيل» لمعرفة الملفات المتعذرة؛ النتيجة الفارغة لا تعني نجاح التنظيم."
            )
        elif result.waiting:
            self.empty_title.setText("ننتظر اكتمال حفظ ملفاتك")
            self.empty_subtitle.setText(f"{result.waiting} ملف لم يستقر بعد. لا حاجة لتكرار الفحص.")
        else:
            self.empty_title.setText("لا توجد ملفات جديدة جاهزة للتنظيم")
            self.empty_subtitle.setText(
                f"نُظّم {self.auto_processed} ملف منذ تشغيل المساعد. "
                "إذا توقعت ملفات أخرى، أوقفه واستخدم «فحص الآن» لمراجعة المجلد والأنواع المدعومة."
            )
        self.last_check_label.setText("آخر تحقق " + datetime.now().strftime("%H:%M:%S"))
        self.last_errors = result.errors
        self.details_button.setVisible(bool(result.errors))
        self.retry_button.setVisible(bool(result.errors))
        if result.errors:
            self.set_notice(
                f"المساعد مستمر؛ هناك {len(result.errors)} تنبيه. راجع التفاصيل أو أعد المحاولة.", True
            )
        elif result.processed:
            self.set_notice(
                f"نُظّم {result.processed} ملف تلقائياً. الإجمالي في هذه الجلسة: {self.auto_processed}."
            )
        elif result.scan.items:
            self.set_notice(
                f"لم يُنظّم أي ملف في هذه الدورة: {len(result.scan.items)} ملف يحتاج تأكيد العميل. "
                "اضغط «اكتشاف وربط العملاء»، أو اختر ملفاً ثم «تأكيد وتعلّم».",
                True,
            )
            if not self.auto_processed:
                self.auto_badge.setText("بانتظار تحديد العملاء")
        elif result.waiting:
            self.set_notice(f"بانتظار استقرار {result.waiting} ملف قبل مطابقة العملاء والتنظيم.")
        else:
            self.set_notice(
                f"لا توجد عمليات جديدة في هذه الدورة. نُظّم {self.auto_processed} ملف في هذه الجلسة."
            )
        self.update_summary()
        self.automation_cycle_finished.emit(result)

    @Slot()
    def retry_automation(self):
        if self.thread is None and self.auto_engine is not None:
            self.auto_engine.retry()
            if self.auto_enabled:
                self.automation_timer.stop()
                self.run_auto_cycle()
            else:
                self.set_notice("تمت إعادة ضبط الأخطاء. شغّل المساعد لإعادة المحاولة.")

    @Slot()
    def start_discovery(self):
        if self.thread is not None:
            return
        self.stop_automation()
        self.suggest_destination()
        if not self.source_input.text().strip() or not self.destination_input.text().strip():
            self.set_notice("اختر مجلد المصدر أولاً لاكتشاف العملاء.", True)
            return
        source = Path(self.source_input.text().strip()).expanduser()
        destination = Path(self.destination_input.text().strip()).expanduser()
        date_mode, recursive = self.date_combo.currentData(), self.recursive_check.isChecked()
        self.run_task(
            lambda cancel, progress: self.organizer.scan(
                source,
                destination,
                date_mode,
                recursive,
                cancel,
                verify_duplicates=False,
                check_readable=False,
            ),
            self.show_discovery,
            "يبحث عن أسماء العملاء في المجلدات والملفات...",
        )

    def show_discovery(self, scan):
        self.scan_finished(scan)
        candidates = [c for c in discover_clients(scan, self.store.clients()) if c.existing_client_id is None]
        if not candidates:
            if scan.warnings or not scan.items:
                return
            self.set_notice(
                "فُحصت الملفات ولم نجد أسماء عملاء جديدة مميزة. "
                "اختر ملفاً ثم «تأكيد وتعلّم»، أو أضف عميلاً من صفحة العملاء."
            )
            return
        dialog = DiscoveryDialog(self.store, candidates, self)
        dialog.exec()
        if dialog.saved_count:
            self.refresh_clients()
            self.invalidate_preview()
            if self.thread is not None:
                self._after_task = self.start_scan
            else:
                self.start_scan()

    def selected_preview_row(self):
        rows = self.table.selectionModel().selectedRows()
        return self.proxy.mapToSource(rows[0]).row() if rows else None

    @Slot()
    def update_review_actions(self, *_):
        self.learn_button.setEnabled(self.selected_preview_row() is not None and self.thread is None)

    @Slot()
    def resolve_selected(self):
        row = self.selected_preview_row()
        if row is None or self.model.scan is None or self.thread is not None:
            return
        self.stop_automation()
        if not self.store.clients():
            if ClientDialog(self.store, parent=self).exec() != QDialog.DialogCode.Accepted:
                return
            self.refresh_clients()
        dialog = ResolveDialog(self.store, self.model.scan, self.model.scan.items[row], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model.clients = {client.id: client for client in self.store.clients()}
            self.model.setData(self.model.index(row, 3), dialog.client_id)
            self.refresh_clients()
            if dialog.learned:
                self.set_notice(
                    "تأكد العميل وحُفظ الاسم للمرة القادمة. نظّم المحدد الآن، "
                    "أو شغّل المساعد لتطبيق قاعدة المطابقة على الملفات."
                )
            else:
                self.set_notice(
                    "تأكد العميل لهذا الملف. يمكنك تنظيم المحدد أو حفظ اسم بديل لتتعرف عليه المراقبة."
                )

    @Slot()
    def filter_preview(self, *_):
        self.proxy.set_filters(
            self.search_input.text(), self.type_filter.currentData(), self.status_filter.currentData()
        )

    @Slot(object)
    def edit_client_cell(self, index):
        if index.column() == 3 and self.thread is None:
            self.table.edit(index)

    @Slot()
    def update_summary(self):
        items = self.model.scan.items if self.model.scan else []
        ready = sum(item.client_id in self.model.clients for item in items)
        values = (
            (self.auto_processed, self.auto_waiting, len(items))
            if self.auto_enabled
            else (len(items), ready, len(items) - ready)
        )
        captions = (
            ("نُظّم تلقائياً", "بانتظار استقرار الملف", "يحتاج قرارك")
            if self.auto_enabled
            else ("في المعاينة", "جاهز للتنظيم", "يحتاج قرارك")
        )
        for widget, value in zip(self.stat_values, values, strict=True):
            widget.setText(f"{value:,}")
        for widget, caption in zip(self.stat_labels, captions, strict=True):
            widget.setText(caption)
        total_size = sum(items[row].size for row in self.model.checked)
        self.selection_label.setText(
            f"{len(self.model.checked):,} ملف محدد من كامل المعاينة  /  {format_size(total_size)}"
        )
        self.execute_button.setEnabled(
            bool(self.model.checked) and self.thread is None and not self.auto_enabled
        )
        self.execute_button.setText(
            f"تنظيم {len(self.model.checked):,} ملف" if self.model.checked else "تنظيم الملفات المحددة"
        )
        needs_setup = not self.clients_table.rowCount() or bool(items) and not ready
        self.execute_button.setVisible(not needs_setup)
        self.setup_button.setVisible(needs_setup)
        self.setup_button.setEnabled(self.thread is None)
        self.execute_button.setToolTip(
            "أوقف المساعد قبل التنظيم اليدوي" if self.auto_enabled else "حدد ملفات لها عميل مؤكد أولاً"
        )

    def set_notice(self, text: str, warning=False):
        self.notice.setText(text)
        self.notice.setProperty("warning", warning)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)

    def set_busy(self, busy: bool):
        for widget in (
            self.add_client_button,
            self.edit_client_button,
            self.delete_client_button,
            self.table,
            self.select_button,
            self.clear_button,
            self.refresh_history_button,
            self.undo_button,
            self.discover_button,
            self.retry_button,
        ):
            widget.setEnabled(not busy)
        for widget in (
            self.source_input,
            self.destination_input,
            self.mode_combo,
            self.date_combo,
            self.recursive_check,
            self.settle_spin,
            self.scan_button,
            *self.browse_buttons,
        ):
            widget.setEnabled(not busy and not self.auto_enabled)
        self.auto_button.setEnabled(self.auto_enabled or not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.progress.setVisible(busy)
        self.update_summary()
        self.update_review_actions()
        if not busy:
            self.update_client_actions()
            self.update_history_actions()

    def run_task(self, job: Callable, on_result: Callable, message: str, *, automation=False):
        if self.thread is not None:
            return
        self.last_errors = []
        self.details_button.hide()
        self.result_handler = on_result
        self._job_is_auto = automation
        self.thread = QThread(self)
        self.worker = Worker(job)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.result.connect(self.on_task_result)
        self.worker.failed.connect(self.on_task_error)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.on_task_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.set_busy(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText(message)
        if not automation:
            self.set_notice(message)
        self.thread.start()

    @Slot(object)
    def on_task_result(self, result):
        if self.result_handler:
            try:
                self.result_handler(result)
            except Exception as exc:
                self.on_task_error(str(exc))

    @Slot(str)
    def on_task_error(self, message):
        if self._job_is_auto:
            self.stop_automation()
        self.set_notice(f"تعذر إكمال العملية: {message}", warning=True)
        self.last_errors = [message]
        self.details_button.show()

    @Slot(int, int, str)
    def on_progress(self, current, total, message):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.progress_label.setText(f"{current:,} / {total:,}  {Path(message).name}")

    @Slot()
    def on_task_finished(self):
        self.thread = None
        self.worker = None
        self.result_handler = None
        self._job_is_auto = False
        self.set_busy(False)
        self.progress_label.setText("يراقب المجلد كل 4 ثوانٍ" if self.auto_enabled else "جاهز")
        self.refresh_history()
        if self._close_pending:
            self._after_task = None
            self.close()
        elif self._after_task is not None:
            callback, self._after_task = self._after_task, None
            callback()
        elif self.auto_enabled:
            self.automation_timer.start()

    @Slot()
    def cancel_task(self):
        if self.auto_enabled:
            self.stop_automation()
        if self.thread:
            self.thread.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.set_notice(
                "جارٍ الإلغاء بأمان. تبقى الملفات المكتملة مسجلة ويمكن التراجع عنها.", warning=True
            )

    @Slot()
    def start_scan(self):
        if self.thread is not None:
            return
        self.stop_automation()
        self.suggest_destination()
        if not self.source_input.text().strip() or not self.destination_input.text().strip():
            self.set_notice("حدد مجلد المصدر ومجلد الوجهة أولاً.", warning=True)
            return
        source = Path(self.source_input.text().strip()).expanduser()
        destination = Path(self.destination_input.text().strip()).expanduser()
        date_mode, recursive = self.date_combo.currentData(), self.recursive_check.isChecked()
        try:
            self.save_settings()
        except Exception as exc:
            self.on_task_error(str(exc))
            return
        self.model.load(None, self.store.clients())
        self.preview_stack.setCurrentIndex(0)
        self.empty_title.setText("جارٍ اكتشاف ملفات التصميم...")
        self.empty_subtitle.setText(
            "نطابق الأسماء والتواريخ ونتحقق من النسخ السابقة. لا يتم نسخ أو نقل أي ملف أثناء الفحص."
        )
        self.run_task(
            lambda cancel, progress: self.organizer.scan(source, destination, date_mode, recursive, cancel),
            self.scan_finished,
            "جارٍ فحص الملفات ومطابقة أسماء العملاء...",
        )

    def scan_finished(self, scan: ScanResult):
        self.model.load(scan, self.store.clients())
        self.preview_stack.setCurrentIndex(1 if scan.items else 0)
        if not scan.items:
            self.empty_title.setText(
                "تعذر العثور على ملفات قابلة للفحص" if scan.warnings else "لا توجد ملفات تصميم في نتيجة الفحص"
            )
            self.empty_subtitle.setText(
                "الأنواع المدعومة: CDR، CDT، PSD، PSB، AI. تحقق من المسار والمجلدات الفرعية."
            )
        self.last_errors = scan.warnings
        self.details_button.setVisible(bool(scan.warnings))
        unresolved = sum(item.client_id is None for item in scan.items)
        if scan.items and unresolved == len(scan.items):
            message = (
                f"اكتُشف {len(scan.items):,} ملف، لكن لا توجد مطابقة مؤكدة للعملاء. "
                "لم تُنقل ملفات. اضغط «اكتشاف وربط العملاء» أو «تأكيد وتعلّم»."
            )
        else:
            message = (
                f"معاينة فقط: {len(scan.items):,} ملف، {len(scan.items) - unresolved:,} جاهز، "
                f"{unresolved:,} يحتاج تحديد العميل. اختر «تنظيم الملفات المحددة» أو شغّل المساعد للتنفيذ."
            )
        self.set_notice(
            message + (f" تنبيهات الفحص: {len(scan.warnings)}؛ راجع التفاصيل." if scan.warnings else ""),
            warning=bool(scan.warnings) or bool(scan.items) and unresolved == len(scan.items),
        )

    def confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        accept = box.addButton("تأكيد", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("إلغاء", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() == accept

    @Slot()
    def start_execute(self):
        if self.model.scan is None or not self.model.checked or self.thread is not None:
            return
        self.stop_automation()
        items = [replace(self.model.scan.items[i]) for i in sorted(self.model.checked)]
        scan = replace(self.model.scan, items=items)
        mode = self.mode_combo.currentData()
        action = "نسخ" if mode == "copy" else "نقل"
        warning = (
            "ستبقى الملفات الأصلية في مكانها."
            if mode == "copy"
            else "ستُحذف الملفات الأصلية بعد نسخها والتحقق من بصمتها."
        )
        if not self.confirm(
            f"تأكيد {action} الملفات",
            f"سيتم {action} {len(items):,} ملفاً من كامل المعاينة (بما فيها المحددة المخفية بالبحث).\n"
            f"{warning}\n\nالوجهة: {scan.destination}\n\n"
            "لن تُستبدل الملفات الموجودة. يمكنك التراجع لاحقاً من سجل العمليات.",
        ):
            return
        self.run_task(
            lambda cancel, progress: self.organizer.execute(scan, mode, progress, cancel),
            self.execute_finished,
            f"جارٍ {action} الملفات والتحقق من سلامتها...",
        )

    def execute_finished(self, result: BatchResult):
        self.model.load(None, self.store.clients())
        self.preview_stack.setCurrentIndex(0)
        self.empty_title.setText("تمت معالجة الملفات المحددة")
        self.empty_subtitle.setText("يمكنك فتح الوجهة أو مراجعة سجل العمليات. أعد الفحص لتنظيم دفعة جديدة.")
        self.batch_notice(result)

    def batch_notice(self, result: BatchResult, undo=False):
        self.last_errors = result.errors
        self.details_button.setVisible(bool(result.errors))
        action = "التراجع" if undo else "التنظيم"
        self.set_notice(
            f"نتيجة {action}: نجح {result.succeeded:,} | تعذر {result.failed:,} | تم تجاهل {result.skipped:,}. "
            "تفاصيل الملفات محفوظة في سجل العمليات.",
            warning=bool(result.failed or result.errors),
        )

    @Slot()
    def open_destination(self):
        destination = Path(self.destination_input.text().strip()).expanduser()
        if not self.destination_input.text().strip() or not destination.is_dir():
            self.set_notice("مجلد الوجهة غير موجود بعد. اختر وجهة أو نفذ أول عملية تنظيم.", warning=True)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination.resolve()))):
            self.set_notice("تعذر فتح مجلد الوجهة في مستكشف الملفات.", warning=True)

    def refresh_clients(self):
        clients = self.store.clients()
        self.clients_table.clearSelection()
        self.clients_table.setRowCount(len(clients))
        for row, client in enumerate(clients):
            name = QTableWidgetItem(client.name)
            name.setData(Qt.ItemDataRole.UserRole, client.id)
            self.clients_table.setItem(row, 0, name)
            aliases = QTableWidgetItem(" / ".join(client.aliases))
            aliases.setToolTip("\n".join(client.aliases))
            self.clients_table.setItem(row, 1, aliases)
        self.client_count.setText(
            f"{len(clients)} عميل"
            if clients
            else "لم تُضف أي عميل بعد. أضف أول عميل وأسماءه العربية والإنجليزية."
        )
        self.update_client_actions()

    def selected_client(self) -> Client | None:
        rows = self.clients_table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        selected = self.clients_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((client for client in self.store.clients() if client.id == selected), None)

    @Slot()
    def update_client_actions(self):
        enabled = bool(self.clients_table.selectionModel().selectedRows()) and self.thread is None
        self.edit_client_button.setEnabled(enabled)
        self.delete_client_button.setEnabled(enabled)

    @Slot()
    def add_client(self):
        if self.thread is None:
            self.stop_automation()
        if (
            self.thread is None
            and ClientDialog(self.store, parent=self).exec() == QDialog.DialogCode.Accepted
        ):
            self.refresh_clients()
            self.invalidate_preview()
            self.set_notice("حُفظ العميل وأسماؤه البديلة. انتقل إلى تنظيم الملفات لبدء المعاينة.")

    @Slot()
    def edit_client(self, *_):
        if self.thread is None:
            self.stop_automation()
        client = self.selected_client()
        if (
            client
            and self.thread is None
            and ClientDialog(self.store, client, self).exec() == QDialog.DialogCode.Accepted
        ):
            self.refresh_clients()
            self.invalidate_preview()

    @Slot()
    def delete_client(self):
        client = self.selected_client()
        if not client or self.thread is not None:
            return
        self.stop_automation()
        if self.confirm(
            "حذف العميل", f"حذف «{client.name}» وأسمائه البديلة؟\nلن تُحذف ملفات العميل أو سجل العمليات."
        ):
            try:
                self.store.delete_client(client.id)
                self.refresh_clients()
                self.invalidate_preview()
            except Exception as exc:
                self.on_task_error(str(exc))

    @Slot()
    def refresh_history(self):
        selected = self.selected_batch()
        selected_id = selected["id"] if selected else None
        previous_block = self.history_table.blockSignals(True)
        self.history_table.clearSelection()
        self._history = self.store.history()
        self.history_table.setRowCount(len(self._history))
        statuses = {
            "completed": "مكتملة",
            "complete": "مكتملة",
            "running": "قيد التنفيذ / متوقفة",
            "partial": "مكتملة جزئياً",
            "failed": "تعذرت",
            "undone": "تم التراجع",
            "cancelled": "أُلغيت",
            "interrupted": "متوقفة",
            "undo_partial": "تراجع جزئي",
            "undoing": "جارٍ التراجع / متوقفة",
        }
        for row, batch in enumerate(self._history):
            values = (
                datetime.fromisoformat(batch["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "نسخ" if batch["mode"] == "copy" else "نقل",
                str(batch["succeeded"]),
                str(batch["failed"]),
                str(batch["undone"]),
                statuses.get(batch["status"], batch["status"]),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, batch["id"])
                self.history_table.setItem(row, col, item)
            if batch["id"] == selected_id:
                self.history_table.setCurrentCell(row, 0)
        self.history_table.blockSignals(previous_block)
        self.update_history_actions()

    def selected_batch(self) -> dict | None:
        rows = self.history_table.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        return self._history[row] if 0 <= row < len(self._history) else None

    @Slot()
    def update_history_actions(self):
        selected = self.selected_batch()
        self.history_details_button.setEnabled(selected is not None)
        self.undo_button.setEnabled(selected is not None and self.thread is None)

    @Slot()
    def start_undo(self):
        batch = self.selected_batch()
        if not batch or self.thread is not None:
            return
        self.stop_automation()
        if not self.confirm(
            "تأكيد التراجع",
            "سيتم التراجع عن الملفات الآمنة فقط في العملية المحددة.\n"
            "للنسخ: تُحذف النسخة المنظمة بعد التحقق من وجود الأصل وسلامته.\n"
            "للنقل: يُعاد الملف إلى مساره الأصلي دون استبدال أي ملف.\n\n"
            "أي ملف تغير بعد التنظيم سيبقى كما هو ويُسجل تعذر التراجع عنه.",
        ):
            return
        self.invalidate_preview()
        self.run_task(
            lambda cancel, progress: self.organizer.undo(batch["id"], progress, cancel),
            lambda result: self.batch_notice(result, undo=True),
            "جارٍ التحقق والتراجع بأمان...",
        )

    @Slot()
    def show_history_details(self, *_):
        batch = self.selected_batch()
        if not batch:
            return
        operations = self.store.operations(batch["id"])
        lines = [f"{batch['created_at']} | {batch['mode']} | {batch['status']}", ""]
        for operation in operations:
            lines.extend(
                (
                    f"[{operation['status']}]",
                    f"من: {operation['source']}",
                    f"إلى: {operation['destination']}",
                    str(operation.get("error") or ""),
                    "",
                )
            )
        self.text_dialog("تفاصيل العملية", "\n".join(lines))

    @Slot()
    def show_errors(self):
        self.text_dialog("التفاصيل والتنبيهات", "\n\n".join(self.last_errors))

    def text_dialog(self, title: str, text: str):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(790, 510)
        layout = QVBoxLayout(dialog)
        body = QPlainTextEdit(text)
        body.setReadOnly(True)
        layout.addWidget(body)
        layout.addWidget(button("إغلاق", dialog.accept))
        dialog.exec()

    @Slot()
    def show_help(self):
        self.text_dialog(
            "دليل الاستخدام",
            (
                "SMART FILE ORGANIZER\n\n"
                "1. اختر مجلد المصدر أو اسحبه إلى الحقل. نقترح مجلد Organized داخله كوجهة قابلة للتغيير.\n\n"
                "2. اضغط «اكتشاف العملاء» لاستخراج أسماء مقترحة من الملفات والمجلدات. راجعها ثم احفظ المحدد.\n"
                "   اختر عميلاً موجوداً لربط الاسم به، مثل Lara مع لارا، بدلاً من إنشاء عميل آخر.\n"
                "   للعميل الجديد يمكنك تعديل الاسم إلى العربية؛ يبقى الاسم الأصلي اسماً بديلاً للمطابقة.\n\n"
                "3. اضغط «تشغيل المساعد» ووافق مرة واحدة على المجلدات ووضع النسخ أو النقل.\n"
                "   يراقب المجلد كل 4 ثوانٍ وينظم الملفات المعروفة دون طلب تأكيد لكل دفعة.\n"
                "   ينتظر ثبات حجم الملف وتاريخه 8 ثوانٍ افتراضياً؛ المدة قابلة للتعديل من الخيارات.\n"
                "   الملفات الفارغة أو الحديثة جداً تنتظر. الملفات المقفلة أو غير الآمنة تظهر كأخطاء.\n\n"
                "4. غير المعروف والملتبس لا يُنظّم. اختر الملف ثم «تأكيد وتعلّم» لتحديد العميل وحفظ اسم بديل.\n"
                "   هذا يوقف المساعد للمراجعة. راجع الاسم ثم شغّل المساعد مجدداً ليستفيد من القاعدة الجديدة.\n"
                "   لا يُستخدم التشابه أو النقل الصوتي وحده لتنفيذ عملية تلقائية.\n\n"
                "5. زر «للخلفية» يخفي النافذة في منطقة الإشعارات ويترك المراقبة تعمل.\n"
                "   زر الإيقاف أو إغلاق التطبيق يوقفانها. لا يوجد تشغيل تلقائي عند بدء Windows أو فتح التطبيق.\n\n"
                "الفحص اليدوي: «فحص الآن» يعرض كل الملفات دون نقلها؛ راجع التحديد ثم نفذ التنظيم.\n"
                "البحث يرشح العرض فقط، وليس الملفات المحددة للتنفيذ.\n"
                "المسار الناتج: الوجهة / العميل / YYYY-MM / اسم الملف الأصلي.\n"
                "الأسماء المتكررة تحصل على رقم إضافي؛ لا يُستبدل الموجود ولا تتكرر النسخ الموثقة دون تغيير.\n"
                "المراقبة تعتمد على هوية الملف وحجمه وتاريخ تعديله؛ قد لا تكتشف تغيير المحتوى إذا بقيت الثلاثة كما هي.\n"
                "في هذه الحالة استخدم «فحص الآن» للتحقق من المحتوى مجدداً.\n"
                "الأخطاء لا يعاد تنفيذها باستمرار؛ صحح السبب واضغط «إعادة المحاولة».\n"
                "«النشاط والاستعادة» يعرض العمليات ويتيح التراجع. تتوقف المراقبة قبل التراجع لمنع إعادة التنظيم مباشرة.\n\n"
                "التاريخ: يُقرأ YYYY-MM أو YYYY-MM-DD من الاسم (تُدعم الأرقام العربية)، وإلا يُستخدم آخر تعديل.\n"
                "لا يقرأ البرنامج طبقات التصميم أو تاريخ إنشائه الداخلي. الملفات غير المدعومة لا تُلمس.\n"
                "المجلدات المرتبطة رمزياً تُتجاهل، ومجلد الوجهة مستبعد من الفحص المتداخل.\n\n"
                "ملفات NTFS التي تحتوي تدفقات بيانات إضافية (ADS) يُرفض نسخها ونقلها لحماية تلك البيانات.\n\n"
                "الخصوصية: كل البيانات محلية، دون رفع الملفات أو استخدام خدمات ذكاء اصطناعي خارجية.\n"
                "احتفظ بنسخة احتياطية لملفاتك المهمة. سجل العمليات لا يجعل النسخ ونقل الملفات معاملة ذرية واحدة.\n"
                "عند انقطاع الطاقة افحص تفاصيل العمليات المتوقفة قبل إعادة المحاولة.\n\n"
                f"قاعدة البيانات والإعدادات محفوظة في:\n{self.data_dir}\n\n"
                "الأدوات: Python + PySide6 + SQLite + RapidFuzz + Unidecode."
            ),
        )

    def closeEvent(self, event):
        self.stop_automation()
        if self.thread is not None:
            self._close_pending = True
            self.cancel_task()
            self.set_notice("نوقف العملية بأمان ثم نغلق النافذة تلقائياً.", warning=True)
            event.ignore()
            return
        try:
            self.save_settings()
        except Exception:
            pass
        if getattr(self, "tray", None) is not None:
            self.tray.hide()
        event.accept()
        if self._force_quit:
            QApplication.instance().quit()
