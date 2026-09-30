"""Offscreen Qt tests use temporary files, never the user's design folders."""

import os
import time
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from smart_organizer.automation import AutoCycle, AutoOrganizer, ClientCandidate
from smart_organizer.core import Organizer, ScanResult, Store
from smart_organizer.models import PreviewFilter, PreviewModel
from smart_organizer.review import DiscoveryDialog, ResolveDialog
from smart_organizer.theme import STYLE
from smart_organizer.window import ClientDialog, MainWindow


@pytest.fixture(scope="session")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    instance.setStyle("Fusion")
    instance.setStyleSheet(STYLE)
    yield instance


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data" / "organizer.sqlite3")
    client = store.save_client("مطبعة النور", ["Al Noor", "Alnoor", "النور"])
    source, destination = tmp_path / "inbox", tmp_path / "archive"
    source.mkdir()
    for name in ("Alnoor_brand_2026-09.ai", "النور_بطاقة_٢٠٢٦-٠٨.psd", "unknown_draft.cdr"):
        (source / name).write_bytes(b"temporary design-file test fixture")
    return store, client, source, destination


@pytest.fixture
def window(app, workspace, tmp_path):
    store, client, source, destination = workspace
    widget = MainWindow(store, tmp_path / "data")
    widget.source_input.setText(str(source))
    widget.destination_input.setText(str(destination))
    widget.show()
    app.processEvents()
    yield widget
    if widget.thread is not None:
        widget.cancel_task()
        wait_for_job(widget)
    widget.close()
    widget.deleteLater()
    app.processEvents()


def wait_for_job(window, timeout=10):
    deadline = time.monotonic() + timeout
    while window.thread is not None and time.monotonic() < deadline:
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        window.thread.finished.connect(loop.quit)
        timer.start(max(1, int((deadline - time.monotonic()) * 1000)))
        loop.exec()
        timer.stop()
    assert window.thread is None, "Qt worker did not stop within timeout"


def test_window_defaults_are_rtl_and_copy_only(window):
    assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert window.mode_combo.currentData() == "copy"
    assert not window.execute_button.isEnabled()
    assert window.pages.count() == 3
    assert window.clients_table.rowCount() == 1


def test_manual_client_confirmation_and_checkbox(app, workspace):
    store, client, source, destination = workspace
    model = PreviewModel()
    model.load(Organizer(store).scan(source, destination), store.clients())
    assert len(model.checked) == 2
    row = next(i for i, item in enumerate(model.scan.items) if item.client_id is None)
    assert not model.setData(model.index(row, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert model.setData(model.index(row, 3), client.id)
    assert model.scan.items[row].client_id == client.id
    assert row in model.checked
    assert model.data(model.index(row, 6)) == "اختيار يدوي"
    assert model.setData(model.index(row, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert row not in model.checked
    model.select_ready(True)
    assert len(model.checked) == 3
    assert model.setData(model.index(row, 3), None)
    assert row not in model.checked
    assert not model.setData(model.index(row, 3), 9999)


def test_filters_do_not_change_selection(app, workspace):
    store, client, source, destination = workspace
    model = PreviewModel()
    model.load(Organizer(store).scan(source, destination), store.clients())
    proxy = PreviewFilter()
    proxy.setSourceModel(model)
    proxy.set_filters("", ".ai", "all")
    assert proxy.rowCount() == 1
    assert len(model.checked) == 2
    proxy.set_filters("", "all", "review")
    assert proxy.rowCount() == 1
    assert proxy.setData(proxy.index(0, 3), client.id)
    assert proxy.rowCount() == 0
    assert len(model.checked) == 3
    proxy.set_filters("Alnoor", "all", "all")
    assert proxy.rowCount() == 1
    proxy.set_filters("", "all", "ready")
    assert proxy.rowCount() == 3


def test_empty_model_is_safe(app):
    model = PreviewModel()
    assert model.rowCount() == 0
    model.select_ready(True)
    assert model.checked == set()


def test_scan_is_background_and_results_arrive_on_gui_thread(window, app):
    result_threads = []
    original = window.scan_finished

    def capture(result):
        result_threads.append(QThread.currentThread())
        original(result)

    window.scan_finished = capture
    window.start_scan()
    assert window.thread is not None
    assert not window.scan_button.isEnabled()
    wait_for_job(window)
    assert window.model.rowCount() == 3
    assert len(window.model.checked) == 2
    assert window.execute_button.isEnabled()
    assert result_threads == [app.thread()]


@pytest.mark.parametrize("setting", ["source", "destination", "date", "recursive"])
def test_settings_invalidate_preview(window, workspace, setting):
    store, client, source, destination = workspace
    window.scan_finished(Organizer(store).scan(source, destination))
    if setting == "source":
        window.source_input.setText(str(source / "different"))
    elif setting == "destination":
        window.destination_input.setText(str(destination / "different"))
    elif setting == "date":
        window.date_combo.setCurrentIndex(1)
    else:
        window.recursive_check.setChecked(False)
    assert window.model.scan is None
    assert not window.execute_button.isEnabled()


def test_gui_copy_and_undo_end_to_end(window, workspace):
    store, client, source, destination = workspace
    window.confirm = lambda *args: True
    window.start_scan()
    wait_for_job(window)
    window.start_execute()
    wait_for_job(window)
    batches = store.history()
    assert len(batches) == 1
    assert batches[0]["succeeded"] == 2
    assert len(list(source.iterdir())) == 3
    assert (destination / client.name / "2026-09" / "Alnoor_brand_2026-09.ai").is_file()
    assert not window.execute_button.isEnabled()
    window.navigate(2)
    QApplication.processEvents()
    QTest.mouseClick(
        window.history_table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.history_table.visualRect(window.history_table.model().index(0, 0)).center(),
    )
    assert window.selected_batch() is not None, (
        window.history_table.rowCount(),
        window.history_table.isEnabled(),
        window.history_table.selectionModel().selectedIndexes(),
        window.thread,
    )
    assert window.undo_button.isEnabled()
    window.start_undo()
    wait_for_job(window)
    assert store.history()[0]["undone"] == 2
    assert not list(destination.rglob("*.ai"))
    assert len(list(source.iterdir())) == 3


def test_no_transfer_if_confirmation_cancelled(window, workspace):
    store, client, source, destination = workspace
    window.scan_finished(Organizer(store).scan(source, destination))
    window.confirm = lambda *args: False
    window.start_execute()
    assert window.thread is None
    assert not destination.exists()
    assert not store.history()


def test_error_restores_controls(window, workspace):
    store, client, source, destination = workspace
    window.destination_input.setText(str(source))
    window.start_scan()
    wait_for_job(window)
    assert window.scan_button.isEnabled()
    assert window.notice.property("warning")
    assert window.last_errors
    assert window.model.scan is None


def test_cancel_keeps_gui_responsive(window):
    observed = []

    def cancellable(cancel, progress):
        while not cancel():
            QThread.msleep(1)
        observed.append(True)
        return "cancelled"

    result = []
    window.run_task(cancellable, result.append, "test")
    window.cancel_task()
    wait_for_job(window)
    assert observed == [True]
    assert result == ["cancelled"]
    assert window.scan_button.isEnabled()


def test_client_dialog_saves_bilingual_aliases(app, tmp_path):
    store = Store(tmp_path / "clients.sqlite3")
    dialog = ClientDialog(store)
    dialog.name_input.setText("استوديو أحمد")
    dialog.aliases_input.setPlainText("Ahmed Studio\nAhmad Studio،أحمد")
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert set(store.clients()[0].aliases) == {"Ahmed Studio", "Ahmad Studio", "أحمد"}
    dialog.deleteLater()


def test_client_dialog_rejects_unsafe_names(app, tmp_path):
    store = Store(tmp_path / "clients.sqlite3")
    dialog = ClientDialog(store)
    dialog.name_input.setText("../outside")
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog.error.isHidden()
    assert not store.clients()
    dialog.deleteLater()


def test_client_deletion_leaves_existing_files_and_history(window, workspace):
    store, client, source, destination = workspace
    scan = Organizer(store).scan(source, destination)
    Organizer(store).execute(replace(scan, items=[i for i in scan.items if i.client_id]))
    window.confirm = lambda *args: True
    window.navigate(1)
    QApplication.processEvents()
    QTest.mouseClick(
        window.clients_table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.clients_table.visualRect(window.clients_table.model().index(0, 0)).center(),
    )
    assert window.selected_client() is not None, (
        window.clients_table.rowCount(),
        window.clients_table.isEnabled(),
        window.clients_table.selectionModel().selectedIndexes(),
        window.thread,
    )
    window.delete_client()
    assert not store.clients()
    assert len(store.history()) == 1
    assert list(destination.rglob("*.ai"))


def test_window_renders_preview(window, workspace, tmp_path, app):
    store, client, source, destination = workspace
    window.scan_finished(Organizer(store).scan(source, destination))
    window.resize(1400, 920)
    app.processEvents()
    screenshot = tmp_path / "smart-organizer-preview.png"
    assert window.grab().save(str(screenshot))
    assert screenshot.stat().st_size > 10_000
    print(f"GUI screenshot: {screenshot}")


def test_settings_persist_but_move_is_not_default(window, workspace):
    store, client, source, destination = workspace
    window.mode_combo.setCurrentIndex(1)
    window.date_combo.setCurrentIndex(1)
    window.recursive_check.setChecked(False)
    window.save_settings()
    second = MainWindow(store, window.data_dir)
    assert second.source_input.text() == str(source)
    assert second.destination_input.text() == str(destination)
    assert second.date_combo.currentData() == "modified"
    assert not second.recursive_check.isChecked()
    assert second.mode_combo.currentData() == "copy"
    second.close()
    second.deleteLater()


def start_test_automation(window, workspace, monkeypatch):
    import smart_organizer.window as window_module

    store, client, source, destination = workspace
    clock = [0.0]
    for path in source.iterdir():
        os.utime(path, (time.time() - 60, time.time() - 60))
    monkeypatch.setattr(
        window_module,
        "AutoOrganizer",
        lambda store, settings: AutoOrganizer(store, settings, clock=lambda: clock[0]),
    )
    window.confirm = lambda *args: True
    window.toggle_automation()
    wait_for_job(window)
    window.automation_timer.stop()
    return clock


def test_automatic_organization_is_opt_in_and_idle_on_start(window, workspace):
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert window.options_panel.isHidden()
    window.confirm = lambda *args: False
    window.toggle_automation()
    assert not window.auto_enabled
    assert not workspace[3].exists()
    assert not workspace[0].history()


def test_auto_waits_then_copies_without_per_batch_confirmations(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    clock = start_test_automation(window, workspace, monkeypatch)
    assert window.auto_enabled
    assert not window.source_input.isEnabled()
    assert not store.history()
    assert window.auto_waiting == 3
    assert window.model.rowCount() == 0
    window.confirm = lambda *args: pytest.fail("Automatic batches must not show another confirmation")
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.auto_processed == 2
    assert len(store.history()) == 1
    assert window.model.rowCount() == 1
    assert window.model.scan.items[0].client_id is None
    assert not window.execute_button.isEnabled()
    assert len(list(source.iterdir())) == 3
    clock[0] = 18
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert len(store.history()) == 1
    window.stop_automation()
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert window.source_input.isEnabled()


def test_auto_detects_new_files_after_initial_cycle(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    incoming = source / "Alnoor_poster_2026-10.psd"
    incoming.write_bytes(b"new completed design")
    os.utime(incoming, (time.time() - 60, time.time() - 60))
    clock[0] = 12
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.auto_waiting == 1
    assert window.auto_processed == 2
    clock[0] = 21
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.auto_processed == 3
    assert (destination / client.name / "2026-10" / incoming.name).read_bytes() == incoming.read_bytes()


def test_invalid_root_stops_automation_instead_of_retrying(window, workspace):
    window.destination_input.setText(window.source_input.text())
    window.confirm = lambda *args: True
    window.toggle_automation()
    wait_for_job(window)
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert window.notice.property("warning")
    assert window.last_errors


def test_path_change_pauses_automation(window, workspace, monkeypatch):
    start_test_automation(window, workspace, monkeypatch)
    window.destination_input.setText(str(workspace[3] / "different"))
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()


def test_undo_stops_active_monitor_and_does_not_redo(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    window.navigate(2)
    QApplication.processEvents()
    window.history_table.setCurrentCell(0, 0)
    assert window.selected_batch() is not None
    window.start_undo()
    wait_for_job(window)
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert store.history()[0]["undone"] == 2
    window.run_auto_cycle()
    assert window.thread is None
    assert not list(destination.rglob("*.ai"))


def test_learning_alias_rematches_future_files(app, workspace):
    store, client, source, destination = workspace
    scan = Organizer(store).scan(source, destination)
    item = next(item for item in scan.items if item.client_id is None)
    dialog = ResolveDialog(store, scan, item)
    dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client.id))
    dialog.alias_input.setText("unknown")
    dialog.learn_check.setChecked(True)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.learned
    assert "unknown" in store.clients()[0].aliases
    assert (
        next(i for i in Organizer(store).scan(source, destination).items if i.source == item.source).client_id
        == client.id
    )
    dialog.deleteLater()


def test_one_time_confirmation_does_not_save_alias(app, workspace):
    store, client, source, destination = workspace
    scan = Organizer(store).scan(source, destination)
    item = next(item for item in scan.items if item.client_id is None)
    before = store.clients()
    dialog = ResolveDialog(store, scan, item)
    dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client.id))
    dialog.learn_check.setChecked(False)
    dialog.save()
    assert dialog.client_id == client.id
    assert not dialog.learned
    assert store.clients() == before
    dialog.deleteLater()


def test_learning_conflict_is_visible_and_preserves_alias_ownership(app, workspace):
    store, client, source, destination = workspace
    second = store.save_client("Other Studio", ["unknown"])
    scan = Organizer(store).scan(source, destination)
    dialog = ResolveDialog(store, scan, scan.items[0])
    dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client.id))
    dialog.alias_input.setText("unknown")
    dialog.learn_check.setChecked(True)
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog.error.isHidden()
    assert "unknown" in next(c for c in store.clients() if c.id == second.id).aliases
    dialog.deleteLater()


def test_discovery_requires_confirmation_and_preserves_original_name_on_edit(app, tmp_path):
    store = Store(tmp_path / "discovery.sqlite3")
    dialog = DiscoveryDialog(store, [ClientCandidate("Noor", 5), ClientCandidate("Acme", 2)])
    assert not store.clients()
    dialog.table.item(0, 1).setText("النور")
    dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.save()
    assert dialog.saved_count == 1
    assert store.clients()[0].name == "النور"
    assert store.clients()[0].aliases == ("Noor",)
    dialog.deleteLater()


def test_discovery_partial_errors_do_not_duplicate_successful_rows(app, tmp_path):
    store = Store(tmp_path / "discovery.sqlite3")
    dialog = DiscoveryDialog(store, [ClientCandidate("Noor", 5), ClientCandidate("../bad", 2)])
    dialog.save()
    assert dialog.saved_count == 1
    assert not dialog.error.isHidden()
    dialog.table.item(1, 1).setText("Acme")
    dialog.save()
    assert dialog.saved_count == 2
    assert len(store.clients()) == 2
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_source_drop_suggests_destination_without_touching_files(window, workspace):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent

    store, client, source, destination = workspace
    window.destination_input.clear()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    event = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.source_input.dropEvent(event)
    assert window.destination_input.text() == str(source / "Organized")
    assert not (source / "Organized").exists()


def test_history_selection_survives_monitor_refresh(window, workspace):
    store, client, source, destination = workspace
    batch = Organizer(store).execute(Organizer(store).scan(source, destination))
    window.navigate(2)
    QApplication.processEvents()
    window.history_table.setCurrentCell(0, 0)
    assert window.selected_batch()["id"] == batch.batch_id
    window.refresh_history()
    assert window.selected_batch()["id"] == batch.batch_id


def test_window_close_stops_monitor_without_background_reactivation(window, workspace, monkeypatch):
    start_test_automation(window, workspace, monkeypatch)
    window.close()
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()


def test_hide_to_tray_keeps_monitor_active(window, workspace, monkeypatch):
    class FakeTray:
        def showMessage(self, *args):
            pass

        def setToolTip(self, *args):
            pass

        def hide(self):
            pass

    start_test_automation(window, workspace, monkeypatch)
    monkeypatch.setattr(window, "tray", FakeTray())
    if not hasattr(window, "tray_pause"):
        monkeypatch.setattr(window, "tray_pause", window.auto_button, raising=False)
    window.hide_to_tray()
    assert not window.isVisible()
    assert window.auto_enabled
    window.restore_window()
    assert window.isVisible()


def test_keyboard_client_editor_pauses_monitor(window, workspace, monkeypatch):
    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    assert window.automation_timer.isActive()
    window.table.setCurrentIndex(window.proxy.index(0, 3))
    window.table.setFocus()
    QTest.keyClick(window.table, Qt.Key.Key_F2)
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert window.table.findChildren(type(window.mode_combo))


def test_review_selection_survives_monitor_cycle(window, workspace, monkeypatch):
    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    window.table.setCurrentIndex(window.proxy.index(0, 1))
    source = window.model.scan.items[window.selected_preview_row()].source
    clock[0] = 18
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.selected_preview_row() is not None
    assert window.model.scan.items[window.selected_preview_row()].source == source
    assert window.learn_button.isEnabled()


def test_learning_keeps_explicit_assignment_even_if_alias_does_not_match(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    window.scan_finished(Organizer(store).scan(source, destination))
    row = next(i for i, item in enumerate(window.model.scan.items) if item.client_id is None)
    window.table.setCurrentIndex(window.proxy.mapFromSource(window.model.index(row, 1)))

    def accept(dialog):
        dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client.id))
        dialog.alias_input.setText("Future Client Alias")
        dialog.learn_check.setChecked(True)
        dialog.save()
        return dialog.result()

    monkeypatch.setattr(ResolveDialog, "exec", accept)
    window.resolve_selected()
    assert window.thread is None
    assert window.model.scan.items[row].client_id == client.id
    assert row in window.model.checked
    assert "Future Client Alias" in store.clients()[0].aliases


def test_close_waits_for_running_job_and_never_restarts(window):
    def job(cancel, progress):
        while not cancel():
            QThread.msleep(1)
        return None

    window.run_task(job, lambda result: None, "test")
    window.close()
    assert window._close_pending
    wait_for_job(window)
    assert not window.isVisible()
    assert not window.automation_timer.isActive()


@pytest.mark.parametrize("size", [(960, 640), (1040, 680), (1280, 760)])
def test_compact_window_keeps_actions_accessible(window, app, tmp_path, size):
    window.resize(*size)
    window.toggle_options()
    app.processEvents()
    assert window.width() <= size[0]
    assert window.height() <= size[1]
    assert window.organizer_scroll.horizontalScrollBar().maximum() == 0
    page = window.pages.currentWidget()
    position = window.execute_button.mapTo(page, window.execute_button.rect().bottomLeft())
    assert page.rect().contains(position)
    window.organizer_scroll.ensureWidgetVisible(window.settle_spin)
    app.processEvents()
    screenshot = tmp_path / "compact-organizer.png"
    assert window.grab().save(str(screenshot))
    print(f"Compact screenshot: {screenshot}")


def test_timer_schedules_next_cycle_without_manual_scan(window, workspace, monkeypatch):
    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    window.automation_cycle_finished.connect(lambda result: loop.quit())
    window.automation_timer.start(20)
    timeout.start(5000)
    loop.exec()
    timeout.stop()
    window.stop_automation()
    wait_for_job(window)
    assert window.auto_processed == 2
    assert len(workspace[0].history()) == 1


def test_source_listing_failure_pauses_monitor(window, workspace, monkeypatch):
    source = workspace[2]
    original = os.scandir

    def inaccessible(path):
        if str(path) == str(source):
            raise PermissionError("Source folder unavailable")
        return original(path)

    monkeypatch.setattr(os, "scandir", inaccessible)
    window.confirm = lambda *args: True
    window.toggle_automation()
    wait_for_job(window)
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert window.notice.property("warning")
    assert any("Source folder unavailable" in message for message in window.last_errors)
    assert not workspace[0].history()


def test_default_window_shows_review_actions_without_page_scrolling(window, app):
    window.resize(1280, 800)
    app.processEvents()
    viewport = window.organizer_scroll.viewport()
    for widget in (window.auto_button, window.learn_button, window.discover_button):
        position = widget.mapTo(viewport, widget.rect().bottomLeft())
        assert viewport.rect().contains(position)


def test_discovery_links_alias_to_existing_client_only_after_confirmation(app, tmp_path):
    store = Store(tmp_path / "discovery.sqlite3")
    client = store.save_client("لارا", [])
    dialog = DiscoveryDialog(store, [ClientCandidate("Lara", 3)])
    target = dialog.table.cellWidget(0, 2)
    target.setCurrentIndex(target.findData(client.id))
    assert store.clients() == [client]
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.saved_count == 1
    assert len(store.clients()) == 1
    assert store.clients()[0].name == "لارا"
    assert store.clients()[0].aliases == ("Lara",)
    assert not target.isEnabled()
    dialog.save()
    assert dialog.saved_count == 1
    dialog.deleteLater()


def test_discovery_link_conflict_preserves_clients(app, tmp_path):
    store = Store(tmp_path / "discovery.sqlite3")
    first = store.save_client("First", ["Lara"])
    second = store.save_client("Second", [])
    dialog = DiscoveryDialog(store, [ClientCandidate("Lara", 3)])
    target = dialog.table.cellWidget(0, 2)
    target.setCurrentIndex(target.findData(second.id))
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.saved_count == 0
    assert not dialog.error.isHidden()
    assert store.clients() == [first, second]
    dialog.deleteLater()


def test_no_match_discovery_and_bilingual_automation_end_to_end(window, workspace, monkeypatch, tmp_path):
    store, previous, source, destination = workspace
    store.delete_client(previous.id)
    client = store.save_client("لارا", [])
    window.refresh_clients()
    for path in source.iterdir():
        path.unlink()
    names = ("Lara 17.cdr", "LARA 16.cdr", "Backup_of_Lara 17.cdr", "الرام.cdr")
    for name in names:
        (source / name).write_bytes(b"temporary Corel design fixture")
    clock = start_test_automation(window, workspace, monkeypatch)
    assert window.model.scan.items == []
    assert window.auto_waiting == 4
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.auto_processed == 0
    assert window.notice.property("warning")
    assert "لم يُنظّم أي ملف" in window.notice.text()
    assert not window.setup_button.isHidden()
    assert window.execute_button.isHidden()
    assert window.auto_badge.text() == "بانتظار تحديد العملاء"
    window.resize(1040, 700)
    QApplication.processEvents()
    screenshot = tmp_path / "no-client-match.png"
    assert window.grab().save(str(screenshot))
    print(f"No-match screenshot: {screenshot}")

    # Discovery must rescan even when the monitor has an empty, partial snapshot.
    window.model.load(ScanResult(source, destination, [], []), store.clients())
    discovered = []

    def approve(dialog):
        for row in range(dialog.table.rowCount()):
            name = dialog.table.item(row, 1).text()
            discovered.append(name)
            if name.casefold() == "lara":
                target = dialog.table.cellWidget(row, 2)
                target.setCurrentIndex(target.findData(client.id))
            else:
                dialog.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.show()
        QApplication.processEvents()
        screenshot = tmp_path / "link-existing-client.png"
        assert dialog.grab().save(str(screenshot))
        print(f"Discovery screenshot: {screenshot}")
        dialog.save()
        return dialog.result()

    monkeypatch.setattr(DiscoveryDialog, "exec", approve)
    window.start_discovery()
    wait_for_job(window)
    assert "lara" in {name.casefold() for name in discovered}
    assert not window.auto_enabled
    assert len(store.clients()) == 1
    assert tuple(alias.casefold() for alias in store.clients()[0].aliases) == ("lara",)
    assert len(window.model.checked) == 3
    assert window.execute_button.isEnabled()
    assert window.setup_button.isHidden()
    assert not destination.exists()
    assert not store.history()

    clock = start_test_automation(window, workspace, monkeypatch)
    clock[0] = 9
    window.run_auto_cycle()
    wait_for_job(window)
    window.automation_timer.stop()
    assert window.auto_processed == 3
    outputs = list(destination.rglob("*.cdr"))
    assert {p.name for p in outputs} == set(names[:3])
    assert all(p.relative_to(destination).parts[0] == "لارا" for p in outputs)
    assert len(list(source.iterdir())) == 4
    assert window.model.scan.items[0].source.name == "الرام.cdr"


def test_no_client_start_opens_discovery_without_enabling_automation(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    store.delete_client(client.id)
    window.refresh_clients()
    seen = []
    monkeypatch.setattr(DiscoveryDialog, "exec", lambda dialog: seen.append(dialog.table.rowCount()) or 0)
    window.toggle_automation()
    wait_for_job(window)
    assert seen and seen[0] > 0
    assert not window.auto_enabled
    assert not window.automation_timer.isActive()
    assert not window.setup_button.isHidden()
    assert not store.clients()
    assert not destination.exists()


def test_discovery_preserves_empty_scan_warnings(window, workspace):
    _, _, source, destination = workspace
    scan = ScanResult(source, destination, [], ["Cannot inspect source file"])
    window.show_discovery(scan)
    assert window.notice.property("warning")
    assert window.last_errors == scan.warnings
    assert not window.details_button.isHidden()
    assert "تنبيهات الفحص" in window.notice.text()


def test_empty_auto_cycle_does_not_claim_success(window, workspace):
    _, _, source, destination = workspace
    scan = ScanResult(source, destination, [], [])
    window.auto_cycle_done(AutoCycle(scan, None, 0, 0, ["Cannot inspect source file"]))
    assert "لم يكتمل" in window.empty_title.text()
    assert window.notice.property("warning")
    window.auto_cycle_done(AutoCycle(scan, None, 0, 0, []))
    assert "لا توجد ملفات جديدة" in window.empty_title.text()
    assert "0" in window.notice.text()


def test_resolve_first_client_without_leaving_preview(window, workspace, monkeypatch):
    store, client, source, destination = workspace
    store.delete_client(client.id)
    window.refresh_clients()
    window.scan_finished(Organizer(store).scan(source, destination))
    window.table.setCurrentIndex(window.proxy.index(0, 1))
    created = []

    def create(dialog):
        dialog.name_input.setText("First client")
        dialog.save()
        created.append(store.clients()[0].id)
        return dialog.result()

    def resolve(dialog):
        dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(created[0]))
        dialog.learn_check.setChecked(False)
        dialog.save()
        return dialog.result()

    monkeypatch.setattr(ClientDialog, "exec", create)
    monkeypatch.setattr(ResolveDialog, "exec", resolve)
    window.resolve_selected()
    assert len(window.model.checked) == 1
    assert window.execute_button.isEnabled()
    assert window.model.scan.items[window.selected_preview_row()].client_id == created[0]
