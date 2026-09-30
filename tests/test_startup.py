import os
import subprocess
import sys
from pathlib import Path


def test_application_entry_point_and_single_instance_lock(tmp_path):
    script = """
import sys
from pathlib import Path
from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication
from smart_organizer.app import main
from smart_organizer.window import MainWindow

original_exec = QApplication.exec
def bounded_exec(self):
    windows = [w for w in self.topLevelWidgets() if isinstance(w, MainWindow)]
    assert len(windows) == 1 and windows[0].isVisible()
    lock = QLockFile(str(Path(sys.argv[2]) / 'organizer.lock'))
    lock.setStaleLockTime(0)
    assert not lock.tryLock(0), 'A second process could use the same database'
    QTimer.singleShot(30, self.quit)
    return original_exec()
QApplication.exec = bounded_exec
raise SystemExit(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "--data-dir", str(tmp_path / "app-data")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "app-data" / "organizer.sqlite3").is_file()
    assert not (tmp_path / "app-data" / "organizer.lock").exists()


def test_cli_help_is_available():
    result = subprocess.run(
        [sys.executable, "-m", "smart_organizer", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "--data-dir" in result.stdout
