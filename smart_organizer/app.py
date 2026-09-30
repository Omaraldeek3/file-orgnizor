"""Application entry point. User data lives outside the scanned project."""

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLocale, QLockFile, QStandardPaths, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from .core import Store
from .theme import STYLE, app_icon
from .window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart File Organizer desktop application")
    parser.add_argument("--data-dir", type=Path, help="Override the local application-data directory")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("SmartFileOrganizer")
    app.setOrganizationName("SmartFileOrganizer")
    app.setApplicationVersion("1.0.0")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(app_icon())
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    QLocale.setDefault(QLocale("ar_EG"))
    data_dir = args.data_dir or Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    try:
        data_dir = data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(data_dir / "organizer.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.warning(
                None, "التطبيق مفتوح", "توجد نسخة أخرى تستخدم قاعدة البيانات هذه. استخدم النافذة المفتوحة."
            )
            return 1
        handler = RotatingFileHandler(
            data_dir / "organizer.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        logging.basicConfig(
            level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s"
        )
        store = Store(data_dir / "organizer.sqlite3")
        window = MainWindow(store, data_dir)
    except Exception as exc:
        QMessageBox.critical(None, "تعذر بدء التطبيق", f"تعذر تجهيز التطبيق أو قاعدة البيانات:\n{exc}")
        return 1

    def report_exception(kind, value, traceback):
        logging.error("Unhandled application exception", exc_info=(kind, value, traceback))
        QMessageBox.critical(window, "حدث خطأ", f"{value}\n\nتفاصيل الخطأ محفوظة في سجل التطبيق.")

    sys.excepthook = report_exception
    window.show()
    return app.exec()
