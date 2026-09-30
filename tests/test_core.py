import os
import sqlite3
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from smart_organizer import core
from smart_organizer.core import SUPPORTED_EXTENSIONS, Organizer, Store


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "incoming"
    source.mkdir()
    destination = tmp_path / "organized"
    store = Store(tmp_path / "state" / "organizer.sqlite3")
    return source, destination, store, Organizer(store)


def make_file(source, name, content=b"design content"):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    timestamp = datetime(2025, 7, 15, 12).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def test_bilingual_aliases_normalized_and_portable(setup):
    source, destination, store, organizer = setup
    client = store.save_client("أحمد علي", ["Ahmed Ali", "AHMED-ALI", "احمد على"])
    assert client.aliases == ("Ahmed Ali",)
    for name in ("أَحْمَـد_عَلِی_٢٠٢٤-٠٢-٢٩.cdr", "AHMED ALI 2024_03.psd"):
        make_file(source, name)
    result = organizer.scan(source, destination)
    assert len(result.items) == 2
    assert {item.client_id for item in result.items} == {client.id}
    assert {item.month for item in result.items} == {"2024-02", "2024-03"}
    assert all(item.confidence == 1.0 for item in result.items)
    assert organizer.destination_for(result.items[0], destination).parent.parent.name == client.name
    with pytest.raises(ValueError):
        store.save_client("Other", ["احمد علي"])
    with pytest.raises(ValueError):
        store.save_client("Other", ["ahmed ali"])


def test_ambiguous_and_boundary_matching(setup):
    source, destination, store, organizer = setup
    anna = store.save_client("Ann", [])
    bob = store.save_client("Bob", [])
    make_file(source, "Ann Bob.ai")
    make_file(source, "Annual.ai")
    make_file(source, "Ann_final.ai")
    result = {item.source.stem: item for item in organizer.scan(source, destination).items}
    assert result["Ann Bob"].client_id is None
    assert result["Ann Bob"].suggested_client_id is None
    assert result["Annual"].client_id is None
    assert result["Ann_final"].client_id == anna.id
    assert bob.id != anna.id


def test_fuzzy_and_transliteration_are_only_suggestions(setup):
    source, destination, store, organizer = setup
    ahmed = store.save_client("أحمد", [])
    acme = store.save_client("Acme", [])
    make_file(source, "Ahmed 2024-04.cdr")
    make_file(source, "Acmee 2024-04.psd")
    items = {item.source.stem.split()[0]: item for item in organizer.scan(source, destination).items}
    assert items["Ahmed"].client_id is None
    assert items["Ahmed"].suggested_client_id == ahmed.id
    assert items["Acmee"].client_id is None
    assert items["Acmee"].suggested_client_id == acme.id
    result = organizer.execute(organizer.scan(source, destination))
    assert (result.succeeded, result.skipped) == (0, 2)
    assert not destination.exists()


def test_filename_priority_and_nearest_relative_parent(setup):
    source, destination, store, organizer = setup
    acme = store.save_client("Acme", [])
    bob = store.save_client("Bob", [])
    make_file(source, "Bob/Acme/final.ai")
    make_file(source, "Bob/Acme_final.ai")
    result = organizer.scan(source, destination)
    assert all(item.client_id == acme.id for item in result.items)
    assert all(item.client_id != bob.id for item in result.items)
    make_file(source, "Bob/Bob Acme.ai")
    ambiguous = next(
        item for item in organizer.scan(source, destination).items if item.source.stem == "Bob Acme"
    )
    assert ambiguous.client_id is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Acme 2024-02-29.cdr", "2024-02"),
        ("Acme ۲۰۲۴ ۰۹ ۳۰.psd", "2024-09"),
        ("Acme 2024_12.ai", "2024-12"),
        ("Acme 2023-02-29.ai", "2025-07"),
        ("Acme 2024-13.ai", "2025-07"),
        ("Acme 2024-00.ai", "2025-07"),
        ("Acme 2024-02-301.ai", "2025-07"),
        ("Acme 2024-02-29 2.ai", "2024-02"),
        ("Acme 2024-02-29 300x200.ai", "2024-02"),
        ("Acme 2024-02-29 2024-03-01.ai", "2024-02"),
        ("Acme ٢٠٢٤_٠٢_٢٩ ٣٠٠x٢٠٠.ai", "2024-02"),
        ("Acme 2023-02-29 2024-03-01.ai", "2024-03"),
        ("Acme 2024-02 2.ai", "2025-07"),
        ("Acme 2024-02 99.ai", "2025-07"),
        ("Acme 2024-02 29.ai", "2024-02"),
        ("Acme unknown.ai", "2025-07"),
    ],
)
def test_dates(setup, filename, expected):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, filename)
    assert organizer.scan(source, destination).items[0].month == expected
    assert organizer.scan(source, destination, date_mode="modified").items[0].month == "2025-07"


def test_recursive_exclusion_and_root_validation(setup):
    source, _, store, organizer = setup
    store.save_client("Acme", [])
    destination = source / "organized"
    make_file(source, "Acme.ai")
    make_file(source, "sub/Acme.cdt")
    make_file(destination, "Acme/existing.psb")
    make_file(source, "Acme.txt")
    assert len(organizer.scan(source, destination).items) == 2
    assert len(organizer.scan(source, destination, recursive=False).items) == 1
    with pytest.raises(ValueError):
        organizer.scan(source, source)
    with pytest.raises(ValueError):
        organizer.scan(source, source.parent)
    with pytest.raises(ValueError):
        organizer.scan(source, destination, date_mode="bogus")
    assert SUPPORTED_EXTENSIONS == {".cdr", ".cdt", ".psd", ".psb", ".ai"}


@pytest.mark.parametrize("nested", [False, True])
def test_scan_root_listing_errors_propagate_but_subfolder_errors_are_warnings(setup, monkeypatch, nested):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    readable = make_file(source, "Acme.ai")
    make_file(source, "nested/Acme.ai")
    denied_folder = source / "nested" if nested else source
    scandir = core.os.scandir

    def denied(path):
        if Path(path) == denied_folder:
            raise PermissionError("test listing denied")
        return scandir(path)

    monkeypatch.setattr(core.os, "scandir", denied)
    if nested:
        result = organizer.scan(source, destination)
        assert [item.source for item in result.items] == [readable]
        assert len(result.warnings) == 1
        assert "test listing denied" in result.warnings[0]
    else:
        with pytest.raises(PermissionError, match="test listing denied"):
            organizer.scan(source, destination)
    assert not store.history()
    assert not destination.exists()


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_transfer_and_undo(setup, mode):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme 2024-01.CDR", b"correct design bytes")
    scan = organizer.scan(source, destination)
    progress = []
    result = organizer.execute(scan, mode=mode, progress=lambda *args: progress.append(args))
    assert (result.succeeded, result.failed, result.skipped) == (1, 0, 0)
    output = destination / "Acme" / "2024-01" / original.name
    assert output.read_bytes() == b"correct design bytes"
    assert original.exists() == (mode == "copy")
    assert store.operations(result.batch_id)[0]["status"] == "completed"
    assert progress[-1][0:2] == (1, 1)
    undone = organizer.undo(result.batch_id)
    assert (undone.succeeded, undone.failed) == (1, 0)
    assert not output.exists()
    assert original.read_bytes() == b"correct design bytes"
    assert store.history()[0]["undone"] == 1
    assert store.history()[0]["status"] == "undone"
    assert organizer.undo(result.batch_id).skipped == 1


def test_case_insensitive_collision_and_repeat_idempotency(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"new content")
    previous = make_file(destination, "Acme/2025-07/ACME.AI", b"previous content")
    scan = organizer.scan(source, destination)
    first = organizer.execute(scan)
    assert first.succeeded == 1
    operation = store.operations(first.batch_id)[0]
    assert Path(operation["destination"]).name == "Acme (2).ai"
    assert previous.read_bytes() == b"previous content"
    assert original.exists()
    second = organizer.execute(scan)
    assert (second.succeeded, second.skipped) == (0, 1)
    scanned = organizer.scan(source, destination)
    assert "نسخة سابقة" in scanned.items[0].reason
    assert organizer.execute(scanned).skipped == 1
    assert len(list(destination.rglob("*.*"))) == 2


def test_modified_previous_copy_does_not_dedupe_or_undo(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, "Acme.ai", b"aaaa")
    scan = organizer.scan(source, destination)
    result = organizer.execute(scan)
    output = Path(store.operations(result.batch_id)[0]["destination"])
    stamp = output.stat().st_mtime_ns
    output.write_bytes(b"bbbb")
    os.utime(output, ns=(stamp, stamp))
    assert organizer.undo(result.batch_id).failed == 1
    assert output.read_bytes() == b"bbbb"
    assert store.operations(result.batch_id)[0]["status"] == "undo_failed"
    second = organizer.execute(scan)
    assert second.succeeded == 1
    assert Path(store.operations(second.batch_id)[0]["destination"]).name == "Acme (2).ai"


def test_stale_source_and_copy_undo_safeguard(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai")
    scan = organizer.scan(source, destination)
    original.write_bytes(b"edited since scan")
    result = organizer.execute(scan, mode="move")
    assert result.failed == 1
    assert original.exists()
    assert not destination.exists()
    result = organizer.execute(organizer.scan(source, destination))
    output = Path(store.operations(result.batch_id)[0]["destination"])
    original.unlink()
    assert organizer.undo(result.batch_id).failed == 1
    assert output.exists()


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_execute_refuses_same_metadata_replacement_since_scan(setup, mode):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"BBBB")
    scan = organizer.scan(source, destination)
    item = scan.items[0]
    before = original.stat()
    assert item.source_identity == (before.st_dev, before.st_ino)
    assert replacement.stat().st_ino != before.st_ino
    os.replace(replacement, original)
    after = original.stat()
    assert (after.st_size, after.st_mtime_ns) == (item.size, item.mtime_ns)

    result = organizer.execute(scan, mode=mode)
    assert (result.succeeded, result.failed) == (0, 1)
    assert original.read_bytes() == b"BBBB"
    assert not destination.exists()
    assert organizer.execute(organizer.scan(source, destination), mode=mode).succeeded == 1


def test_duplicate_check_does_not_accept_replacement_using_stale_scan(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"AAAA")
    scan = organizer.scan(source, destination)
    first = organizer.execute(scan)
    assert first.succeeded == 1
    target = Path(store.operations(first.batch_id)[0]["destination"])
    os.replace(replacement, original)
    assert not organizer._already_copied(scan.items[0], target, None)
    assert organizer.execute(scan).failed == 1

    fresh = organizer.scan(source, destination)
    assert fresh.items[0].source_identity != scan.items[0].source_identity
    assert organizer._already_copied(fresh.items[0], target, None)
    assert organizer.execute(fresh).skipped == 1
    assert len(list(destination.rglob("*.ai"))) == 1


def test_scan_item_positional_api_without_identity_remains_supported(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, "Acme.ai")
    scan = organizer.scan(source, destination)
    item = scan.items[0]
    scan.items[0] = core.ScanItem(
        item.source,
        item.size,
        item.mtime_ns,
        item.month,
        item.client_id,
        item.suggested_client_id,
        item.confidence,
        item.reason,
    )
    assert scan.items[0].source_identity is None
    assert organizer.execute(scan).succeeded == 1


def test_undo_move_never_overwrites_new_source(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"old original")
    result = organizer.execute(organizer.scan(source, destination), mode="move")
    original.write_bytes(b"new unrelated source")
    undone = organizer.undo(result.batch_id)
    assert undone.failed == 1
    assert original.read_bytes() == b"new unrelated source"
    assert Path(store.operations(result.batch_id)[0]["destination"]).read_bytes() == b"old original"


def test_manual_assignment_and_invalid_destination_components(setup):
    source, destination, store, organizer = setup
    client = store.save_client("Acme", [])
    make_file(source, "unknown.ai")
    scan = organizer.scan(source, destination)
    scan.items[0] = replace(scan.items[0], client_id=client.id)
    assert organizer.execute(scan).succeeded == 1
    for month in ("../escape", "2024-13", "2024-01/extra"):
        item = replace(scan.items[0], month=month)
        with pytest.raises(ValueError):
            organizer.destination_for(item, destination)
    store.delete_client(client.id)
    assert organizer.execute(scan).failed == 1


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        "CON",
        "nul.txt",
        "COM¹",
        "client.",
        "client ",
        "../escape",
        "a/b",
        "a\\b",
        "C:drive",
        "bad?name",
        "bad\x00name",
        "a\u202eb",
    ],
)
def test_invalid_names(setup, name):
    _, _, store, _ = setup
    with pytest.raises(ValueError):
        store.save_client(name, [])


def test_client_edit_collision_is_transactional_and_settings_threadsafe(setup):
    _, _, store, _ = setup
    first = store.save_client("Acme", ["اكمي"])
    second = store.save_client("Beta", ["بيتا"])
    with pytest.raises(ValueError):
        store.save_client("New", ["بيتا"], client_id=first.id)
    assert first in store.clients()
    renamed = store.save_client("ACME New", ["Acme"], client_id=first.id)
    assert renamed.id == first.id
    store.delete_client(second.id)
    assert store.save_client("Beta", []).id != first.id
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda number: store.set_setting(str(number), str(number * 2)), range(12)))
    assert store.get_setting("5") == "10"
    assert store.get_setting("absent", "fallback") == "fallback"


def test_deleted_client_id_cannot_be_reassigned_by_stale_scan(setup):
    source, destination, store, organizer = setup
    first = store.save_client("Acme", [])
    make_file(source, "Acme.ai")
    scan = organizer.scan(source, destination)
    store.delete_client(first.id)
    second = store.save_client("Unrelated", [])
    assert second.id != first.id
    assert organizer.execute(scan).failed == 1
    assert not destination.exists()


def test_cancel_scan_and_execute_before_start(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, "Acme.ai")
    cancelled = organizer.scan(source, destination, cancel=lambda: True)
    assert cancelled.items == []
    assert cancelled.warnings
    result = organizer.execute(organizer.scan(source, destination), cancel=lambda: True)
    assert (result.succeeded, result.skipped) == (0, 1)
    assert store.history()[0]["status"] == "cancelled"
    assert not destination.exists()


def test_chunked_cancel_cleans_partial_copy(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"x" * 300)
    monkeypatch.setattr(core, "_CHUNK_SIZE", 32)
    scan = organizer.scan(source, destination)
    target = organizer.destination_for(scan.items[0], destination)

    def cancel():
        return target.exists() and target.stat().st_size > 0

    result = organizer.execute(scan, mode="move", cancel=cancel)
    assert result.succeeded == 0
    assert result.skipped == 1
    assert original.read_bytes() == b"x" * 300
    assert not target.exists()
    assert store.operations(result.batch_id)[0]["status"] == "cancelled"


def test_journal_precedes_file_creation_and_observer_errors_safe(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, "Acme.ai")
    original_copy = core._copy_new

    def checked_copy(source_path, target, expected, cancel):
        with sqlite3.connect(store.path) as connection:
            row = connection.execute("SELECT status, destination FROM operations ORDER BY id DESC").fetchone()
        assert row == ("copying", str(target))
        assert not target.exists()
        return original_copy(source_path, target, expected, cancel)

    def bad_observer(*args):
        raise RuntimeError("UI went away")

    monkeypatch.setattr(core, "_copy_new", checked_copy)
    assert organizer.execute(organizer.scan(source, destination), progress=bad_observer).succeeded == 1


def test_changed_source_during_move_keeps_source(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"AAAA")
    original_copy = core._copy_new

    def edit_after_copy(*args):
        result = original_copy(*args)
        stamp = original.stat().st_mtime_ns
        original.write_bytes(b"BBBB")
        os.utime(original, ns=(stamp, stamp))
        return result

    monkeypatch.setattr(core, "_copy_new", edit_after_copy)
    result = organizer.execute(organizer.scan(source, destination), mode="move")
    assert result.failed == 1
    assert original.read_bytes() == b"BBBB"
    assert store.operations(result.batch_id)[0]["status"] == "recovery_required"
    assert Path(store.operations(result.batch_id)[0]["destination"]).read_bytes() == b"AAAA"


def test_symlinks_rejected_or_skipped(setup, tmp_path):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    outside = tmp_path / "outside"
    outside.mkdir()
    original = make_file(outside, "Acme.ai")
    link = source / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation requires Developer Mode or elevation")
    scan = organizer.scan(source, destination)
    assert scan.items == []
    assert scan.warnings
    with pytest.raises(ValueError):
        organizer.scan(link, destination)
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        organizer.scan(source, destination)
    assert original.exists()


def test_reparse_points_distinguish_redirects_from_cloud_placeholders():
    base = {"st_mode": stat.S_IFDIR, "st_file_attributes": 0x400}
    assert core._is_link(SimpleNamespace(**base, st_reparse_tag=0xA0000003))
    assert core._is_link(SimpleNamespace(**base, st_reparse_tag=0xA000000C))
    assert not core._is_link(SimpleNamespace(**base, st_reparse_tag=0x9000001A))
    assert core._is_link(SimpleNamespace(**base))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junctions_are_skipped(setup, tmp_path):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    outside = tmp_path / "outside"
    make_file(outside, "Acme.ai")
    link = source / "junction"
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if completed.returncode:
        pytest.skip("Filesystem does not support creating junctions")
    try:
        result = organizer.scan(source, destination)
        assert not result.items
        assert result.warnings
        with pytest.raises(ValueError):
            organizer.scan(link, destination)
        with pytest.raises(ValueError):
            organizer.scan(source, link / "output")
    finally:
        link.rmdir()


def test_disk_full_after_short_write_cleans_partial(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"a" * 1024)
    scan = organizer.scan(source, destination)
    target = organizer.destination_for(scan.items[0], destination)
    original_open = Path.open

    class FailingWriter:
        def __init__(self, writer):
            self.writer = writer
            self.written = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.writer.close()

        def fileno(self):
            return self.writer.fileno()

        def write(self, chunk):
            if self.written:
                raise OSError("disk full")
            self.written = True
            return self.writer.write(chunk[:16])

    def patched_open(path, mode="r", *args, **kwargs):
        opened = original_open(path, mode, *args, **kwargs)
        return FailingWriter(opened) if path == target and mode == "xb" else opened

    monkeypatch.setattr(Path, "open", patched_open)
    result = organizer.execute(scan, mode="move")
    assert result.failed == 1
    assert original.read_bytes() == b"a" * 1024
    assert not target.exists()
    assert store.operations(result.batch_id)[0]["status"] == "failed"


def test_undo_cancel_keeps_both_files_and_can_retry(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai")
    result = organizer.execute(organizer.scan(source, destination))
    output = Path(store.operations(result.batch_id)[0]["destination"])
    cancelled = organizer.undo(result.batch_id, cancel=lambda: True)
    assert cancelled.skipped == 1
    assert output.exists() and original.exists()
    assert organizer.undo(result.batch_id).succeeded == 1


def test_incomplete_batch_is_not_blindly_undone(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai")
    result = organizer.execute(organizer.scan(source, destination))
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE batches SET status='running' WHERE id=?", (result.batch_id,))
    with pytest.raises(ValueError):
        organizer.undo(result.batch_id)
    assert original.exists()
    assert Path(store.operations(result.batch_id)[0]["destination"]).exists()


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_undo_rejects_changed_destination_metadata(setup, mode):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    make_file(source, "Acme.ai")
    result = organizer.execute(organizer.scan(source, destination), mode=mode)
    output = Path(store.operations(result.batch_id)[0]["destination"])
    stamp = output.stat().st_mtime_ns + 10_000_000_000
    os.utime(output, ns=(stamp, stamp))
    assert organizer.undo(result.batch_id).failed == 1
    assert output.exists()


def test_copy_undo_rejects_changed_original_with_preserved_metadata(setup):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"AAAA")
    result = organizer.execute(organizer.scan(source, destination))
    output = Path(store.operations(result.batch_id)[0]["destination"])
    stamp = original.stat().st_mtime_ns
    original.write_bytes(b"BBBB")
    os.utime(original, ns=(stamp, stamp))
    assert organizer.undo(result.batch_id).failed == 1
    assert original.read_bytes() == b"BBBB"
    assert output.read_bytes() == b"AAAA"


def test_scan_unreadable_warning_and_execute_outside_source_refused(setup, tmp_path, monkeypatch):
    source, destination, store, organizer = setup
    client = store.save_client("Acme", [])
    original = make_file(source, "Acme.ai")
    original_open = Path.open

    def patched_open(path, *args, **kwargs):
        if path == original:
            raise PermissionError("unreadable test input")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", patched_open)
        result = organizer.scan(source, destination)
        assert result.items == []
        assert result.warnings
    scan = organizer.scan(source, destination)
    outsider = make_file(tmp_path / "elsewhere", "Acme.ai")
    scan.items[0] = replace(scan.items[0], source=outsider, client_id=client.id)
    assert organizer.execute(scan, mode="move").failed == 1
    assert outsider.exists()
    assert not destination.exists()


def add_named_stream(path, content=b"unique additional artwork metadata"):
    if os.name != "nt":
        pytest.skip("Named streams require Windows")
    stream = Path(str(path) + ":artwork-metadata")
    stamp = path.stat().st_mtime_ns
    try:
        stream.write_bytes(content)
    except OSError:
        pytest.skip("Temporary filesystem does not support NTFS named streams")
    os.utime(path, ns=(stamp, stamp))
    return stream


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS named streams")
@pytest.mark.parametrize("mode", ["copy", "move"])
def test_source_named_stream_refuses_transfer_without_modification(setup, mode):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"original artwork")
    stream = add_named_stream(original)
    scan = organizer.scan(source, destination)
    assert len(scan.items) == 1
    result = organizer.execute(scan, mode=mode)
    assert (result.succeeded, result.failed) == (0, 1)
    assert original.read_bytes() == b"original artwork"
    assert stream.read_bytes() == b"unique additional artwork metadata"
    assert not destination.exists()
    operation = store.operations(result.batch_id)[0]
    assert operation["status"] == "failed"
    assert "تدفقات NTFS إضافية غير مدعومة بأمان" in operation["error"]
    assert "لم يتم تغيير الملف الأصلي" in operation["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS named streams")
@pytest.mark.parametrize("mode", ["copy", "move"])
def test_added_destination_named_stream_refuses_undo(setup, mode):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"original artwork")
    result = organizer.execute(organizer.scan(source, destination), mode=mode)
    assert result.succeeded == 1
    output = Path(store.operations(result.batch_id)[0]["destination"])
    before = output.stat()
    stream = add_named_stream(output)
    assert core._same_stat(before, output.stat())
    undone = organizer.undo(result.batch_id)
    assert (undone.succeeded, undone.failed) == (0, 1)
    assert output.read_bytes() == b"original artwork"
    assert stream.read_bytes() == b"unique additional artwork metadata"
    assert original.exists() == (mode == "copy")
    operation = store.operations(result.batch_id)[0]
    assert operation["status"] == "undo_failed"
    assert "تدفقات NTFS إضافية" in operation["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS named streams")
@pytest.mark.parametrize("changed", ["source", "destination"])
def test_move_refuses_named_stream_added_after_copy(setup, monkeypatch, changed):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", b"original artwork")
    scan = organizer.scan(source, destination)
    output = organizer.destination_for(scan.items[0], destination)
    original_copy = core._copy_new
    streams = []

    def copy_then_add_stream(*args, **kwargs):
        copied = original_copy(*args, **kwargs)
        streams.append(add_named_stream(original if changed == "source" else output))
        return copied

    monkeypatch.setattr(core, "_copy_new", copy_then_add_stream)
    result = organizer.execute(scan, mode="move")
    assert (result.succeeded, result.failed) == (0, 1)
    assert original.read_bytes() == b"original artwork"
    assert output.read_bytes() == b"original artwork"
    assert streams[0].read_bytes() == b"unique additional artwork metadata"
    assert store.operations(result.batch_id)[0]["status"] == "recovery_required"


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS named streams")
@pytest.mark.parametrize("changed", ["deletion_target", "safeguard"])
def test_named_stream_rechecked_immediately_before_delete(setup, monkeypatch, changed):
    source, _, _, _ = setup
    deletion_target = make_file(source, "Acme.ai", b"artwork")
    safeguard = make_file(source, "backup.ai", b"artwork")
    digest, expected = core._hash(deletion_target)
    _, backup_info = core._hash(safeguard)
    original_verify = core._verify
    streams = []

    def verify_then_add_stream(path, *args, **kwargs):
        original_verify(path, *args, **kwargs)
        if path == safeguard:
            streams.append(add_named_stream(deletion_target if changed == "deletion_target" else safeguard))

    monkeypatch.setattr(core, "_verify", verify_then_add_stream)
    with pytest.raises(ValueError, match="تدفقات NTFS إضافية"):
        core._remove_verified(deletion_target, expected, digest, safeguard=(safeguard, backup_info, digest))
    assert deletion_target.read_bytes() == b"artwork"
    assert safeguard.read_bytes() == b"artwork"
    assert streams[0].read_bytes() == b"unique additional artwork metadata"


@pytest.mark.skipif(os.name != "nt", reason="Windows stream enumeration")
@pytest.mark.parametrize("supports_streams", [False, True])
def test_unsupported_stream_enumeration_requires_volume_capability_check(
    setup, monkeypatch, supports_streams
):
    source, _, _, _ = setup
    original = make_file(source, "Acme.ai")

    def unsupported_first(*args):
        core.ctypes.set_last_error(87)
        return core.ctypes.c_void_p(-1).value

    def volume_information(root, name, name_size, serial, maximum, flags, filesystem, filesystem_size):
        core.ctypes.cast(flags, core.ctypes.POINTER(core.wintypes.DWORD)).contents.value = (
            0x00040000 if supports_streams else 0
        )
        return 1

    monkeypatch.setattr(core._kernel32, "FindFirstStreamW", unsupported_first)
    monkeypatch.setattr(core._kernel32, "GetVolumeInformationW", volume_information)
    if supports_streams:
        with pytest.raises(OSError, match="تدفقات NTFS الإضافية"):
            core._assert_no_named_streams(original)
    else:
        core._assert_no_named_streams(original)


@pytest.mark.skipif(os.name != "nt", reason="Windows stream enumeration")
@pytest.mark.parametrize("stage", ["first", "next", "volume"])
def test_stream_enumeration_errors_fail_closed(setup, monkeypatch, stage):
    source, _, _, _ = setup
    original = make_file(source, "Acme.ai")
    closed = []
    original_close = core._kernel32.FindClose

    def denied_first(*args):
        core.ctypes.set_last_error(87 if stage == "volume" else 5)
        return core.ctypes.c_void_p(-1).value

    def denied(*args):
        core.ctypes.set_last_error(5)
        return 0

    def close(handle):
        closed.append(handle)
        return original_close(handle)

    if stage == "next":
        monkeypatch.setattr(core._kernel32, "FindNextStreamW", denied)
    else:
        monkeypatch.setattr(core._kernel32, "FindFirstStreamW", denied_first)
        if stage == "volume":
            monkeypatch.setattr(core._kernel32, "GetVolumePathNameW", denied)
    monkeypatch.setattr(core._kernel32, "FindClose", close)
    with pytest.raises(OSError, match="لم يتم تغيير الملف الأصلي"):
        core._assert_no_named_streams(original)
    assert bool(closed) == (stage == "next")
    assert original.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows stream enumeration")
@pytest.mark.parametrize("content", [b"", b"only an unnamed stream"])
def test_normal_unnamed_streams_remain_supported(setup, content):
    source, destination, store, organizer = setup
    store.save_client("Acme", [])
    original = make_file(source, "Acme.ai", content)
    core._assert_no_named_streams(original)
    result = organizer.execute(organizer.scan(source, destination), mode="move")
    assert result.succeeded == 1
    assert organizer.undo(result.batch_id).succeeded == 1
    assert original.read_bytes() == content


def test_add_alias_preserves_existing_values_and_normalizes_duplicates(setup):
    _, _, store, _ = setup
    client = store.save_client("النور", ["Noor", "Al Noor"])
    updated = store.add_alias(client.id, "  NOOR  ")
    assert updated == client
    assert store.add_alias(client.id, "النور") == client
    updated = store.add_alias(client.id, "Nour")
    assert updated.aliases == ("Noor", "Al Noor", "Nour")
    assert Store(store.path).clients() == [updated]


def test_add_alias_collision_is_atomic_and_missing_client_rejected(setup):
    _, _, store, _ = setup
    first = store.save_client("Noor", ["النور"])
    second = store.save_client("Beta", ["بيتا"])
    for alias in ("BETA", "بِيتا"):
        with pytest.raises(ValueError):
            store.add_alias(first.id, alias)
        assert store.clients() == [second, first]
    with pytest.raises(ValueError):
        store.add_alias(99999, "New alias")
    assert first in store.clients()


@pytest.mark.parametrize("alias", ["", "   ", "---", "bad\x00alias", "x" * 241])
def test_add_alias_validation_leaves_previous_aliases_untouched(setup, alias):
    _, _, store, _ = setup
    client = store.save_client("Noor", ["النور"])
    with pytest.raises(ValueError):
        store.add_alias(client.id, alias)
    assert store.clients() == [client]


def test_concurrent_add_alias_appends_without_lost_updates(setup):
    _, _, store, _ = setup
    client = store.save_client("Noor", ["النور"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda number: store.add_alias(client.id, f"Alias {number}"), range(12)))
    aliases = store.clients()[0].aliases
    assert set(aliases) == {"النور", *(f"Alias {number}" for number in range(12))}
    assert len(aliases) == 13


def test_concurrent_alias_collision_only_one_client_can_own_alias(setup):
    _, _, store, _ = setup
    first = store.save_client("Noor", ["النور"])
    second = store.save_client("Beta", ["بيتا"])

    def claim(client_id):
        try:
            return store.add_alias(client_id, "Shared alias").id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        owners = list(executor.map(claim, [first.id, second.id]))
    assert owners.count(None) == 1
    clients = store.clients()
    assert sum("Shared alias" in client.aliases for client in clients) == 1
    assert next(client for client in clients if client.id == first.id).aliases[0] == "النور"
    assert next(client for client in clients if client.id == second.id).aliases[0] == "بيتا"


def test_scan_can_skip_expensive_duplicate_verification_without_changing_execution(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    assert organizer.execute(organizer.scan(source, destination)).succeeded == 1
    hash_file = core._hash
    hashes = []

    def tracked(path, *args, **kwargs):
        hashes.append(path)
        return hash_file(path, *args, **kwargs)

    monkeypatch.setattr(core, "_hash", tracked)
    scan = organizer.scan(source, destination, verify_duplicates=False)
    assert len(scan.items) == 1
    assert hashes == []
    assert "نسخة سابقة" not in scan.items[0].reason
    assert organizer.execute(scan).skipped == 1
    assert hashes
    assert "نسخة سابقة" in organizer.scan(source, destination).items[0].reason


def test_execute_cancellation_from_observer_precedes_new_operation(setup):
    source, destination, store, organizer = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    stopped = False

    def progress(*args):
        nonlocal stopped
        stopped = True

    result = organizer.execute(organizer.scan(source, destination), progress=progress, cancel=lambda: stopped)
    assert (result.succeeded, result.skipped) == (0, 1)
    assert not store.operations(result.batch_id)
    assert not destination.exists()


def test_metadata_only_scan_defers_read_probe_but_execute_still_checks(setup, monkeypatch):
    source, destination, store, organizer = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    open_file = Path.open

    def denied(path, *args, **kwargs):
        if path == original:
            raise PermissionError("test source read denied")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    normal = organizer.scan(source, destination)
    assert normal.items == []
    assert normal.warnings
    metadata = organizer.scan(source, destination, verify_duplicates=False, check_readable=False)
    assert len(metadata.items) == 1
    assert organizer.execute(metadata).failed == 1
    assert not list(destination.rglob("*.ai"))
