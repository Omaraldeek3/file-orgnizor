import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from smart_organizer import automation, core
from smart_organizer.automation import (
    AutoOrganizer,
    AutoSettings,
    ClientCandidate,
    discover_clients,
    suggest_alias,
)
from smart_organizer.core import Organizer, Store


class Clock:
    def __init__(self):
        self.now = 0.0
        self.wall = 2_000_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "incoming"
    source.mkdir()
    destination = tmp_path / "organized"
    store = Store(tmp_path / "state" / "organizer.sqlite3")
    clock = Clock()
    monkeypatch.setattr(automation.time, "time", lambda: clock.wall)
    engine = AutoOrganizer(store, AutoSettings(source, destination), clock=clock)
    return source, destination, store, clock, engine


def make_file(source, name, content=b"design", timestamp=1_900_000_000):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (timestamp, timestamp))
    return path


def settle(engine, clock):
    engine.cycle()
    clock.advance(engine.settings.settle_seconds)
    return engine.cycle()


def test_opt_in_stability_delay_and_no_duplicate_cycles(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor_logo_2026-09.psd")
    assert engine.settings.mode == "copy"
    assert not store.history()
    assert not destination.exists()
    with pytest.raises(FrozenInstanceError):
        engine.settings.mode = "move"
    first = engine.cycle()
    assert (first.waiting, first.processed, first.batch, first.scan.items) == (1, 0, None, [])
    clock.advance(7)
    assert engine.cycle().waiting == 1
    clock.advance(1)
    result = engine.cycle()
    assert (result.processed, result.waiting, result.errors, result.scan.items) == (1, 0, [], [])
    assert result.batch.succeeded == 1
    assert original.exists()
    assert (destination / "Noor" / "2026-09" / original.name).read_bytes() == b"design"

    def no_hash(*args, **kwargs):
        pytest.fail("Polling a successful fingerprint must not rehash large files")

    monkeypatch.setattr(core, "_hash", no_hash)
    for _ in range(4):
        result = engine.cycle()
        assert (result.processed, result.waiting, result.batch, result.scan.items) == (0, 0, None, [])
    assert len(store.history()) == 1


def test_changed_file_resets_stability_window(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"first")
    assert engine.cycle().waiting == 1
    clock.advance(7)
    original.write_bytes(b"second version")
    os.utime(original, (clock.wall, clock.wall))
    assert engine.cycle().waiting == 1
    clock.advance(7)
    assert engine.cycle().waiting == 1
    clock.advance(1)
    assert engine.cycle().processed == 1
    output = Path(store.operations(store.history()[0]["id"])[0]["destination"])
    assert output.read_bytes() == b"second version"


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_same_metadata_replacement_after_success_is_processed_once(setup, mode):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"BBBB")
    before = original.stat()
    assert replacement.stat().st_ino != before.st_ino
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode=mode), clock)
    first = settle(engine, clock)
    assert first.processed == 1
    previous_output = Path(store.operations(first.batch.batch_id)[0]["destination"])

    os.replace(replacement, original)
    after = original.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert engine.cycle().waiting == 1
    clock.advance(7)
    assert engine.cycle().waiting == 1
    clock.advance(1)
    result = engine.cycle()
    assert result.processed == 1
    assert previous_output.read_bytes() == b"AAAA"
    assert Path(store.operations(result.batch.batch_id)[0]["destination"]).read_bytes() == b"BBBB"
    assert original.exists() == (mode == "copy")
    assert engine.cycle().batch is None
    assert len(store.history()) == 2


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_same_metadata_replacement_resets_unfinished_settling(setup, mode):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"BBBB")
    assert original.stat().st_ino != replacement.stat().st_ino
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode=mode), clock)
    assert engine.cycle().waiting == 1
    clock.advance(7)
    os.replace(replacement, original)
    assert engine.cycle().waiting == 1
    clock.advance(7)
    assert engine.cycle().waiting == 1
    assert not destination.exists()
    clock.advance(1)
    result = engine.cycle()
    assert result.processed == 1
    assert Path(store.operations(result.batch.batch_id)[0]["destination"]).read_bytes() == b"BBBB"


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_replacement_after_scan_is_refused_then_settles_again(setup, monkeypatch, mode):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"BBBB")
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode=mode), clock)
    assert engine.cycle().waiting == 1
    clock.advance(8)
    scan = engine.organizer.scan

    def replace_after_scan(*args, **kwargs):
        result = scan(*args, **kwargs)
        os.replace(replacement, original)
        return result

    with monkeypatch.context() as context:
        context.setattr(engine.organizer, "scan", replace_after_scan)
        result = engine.cycle()
    assert result.batch.failed == 1
    assert result.processed == 0
    assert original.read_bytes() == b"BBBB"
    assert not destination.exists()
    assert engine.cycle().waiting == 1
    clock.advance(7)
    assert engine.cycle().waiting == 1
    clock.advance(1)
    assert engine.cycle().processed == 1


def test_mtime_age_and_future_timestamp_prevent_mid_save(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai", timestamp=clock.wall + 30)
    engine.cycle()
    clock.advance(8)
    assert engine.cycle().waiting == 1
    clock.advance(29)
    assert engine.cycle().waiting == 1
    assert not destination.exists()
    clock.advance(1)
    assert engine.cycle().processed == 1


def test_zero_byte_files_wait_even_if_unknown_and_old(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai", b"")
    make_file(source, "unknown.psd", b"")
    result = settle(engine, clock)
    assert (result.waiting, result.scan.items, result.batch) == (2, [], None)
    clock.advance(1000)
    assert engine.cycle().waiting == 2
    assert not store.history()
    assert not destination.exists()


def test_zero_settle_still_requires_two_sightings(setup):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    engine = AutoOrganizer(store, AutoSettings(source, destination, settle_seconds=0), clock)
    assert engine.cycle().waiting == 1
    assert engine.cycle().processed == 1


def test_zero_byte_file_becomes_ready_only_after_finished_write(setup):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"")
    assert settle(engine, clock).waiting == 1
    original.write_bytes(b"finished design")
    os.utime(original, (clock.wall, clock.wall))
    assert engine.cycle().waiting == 1
    clock.advance(8)
    assert engine.cycle().processed == 1


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_only_confirmed_aliases_transfer_and_new_files_are_detected(setup, mode):
    source, destination, store, clock, _ = setup
    noor = store.save_client("النور", ["Noor"])
    store.save_client("Beta", [])
    original = make_file(source, "Noor_logo_2026-09.psd")
    make_file(source, "Noor Beta_card.ai")
    make_file(source, "Nooor_banner.ai")
    make_file(source, "Unknown.ai")
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode=mode), clock)
    assert engine.cycle().scan.items == []
    clock.advance(8)
    result = engine.cycle()
    assert result.processed == 1
    assert len(result.scan.items) == 3
    assert all(item.client_id is None for item in result.scan.items)
    assert original.exists() == (mode == "copy")
    assert (destination / noor.name / "2026-09" / original.name).is_file()
    assert len(store.operations(result.batch.batch_id)) == 1
    make_file(source, "Noor_new_2026-10.cdr")
    assert engine.cycle().waiting == 1
    clock.advance(8)
    assert engine.cycle().processed == 1
    assert len(store.history()) == 2


def test_nested_destination_is_excluded_and_nonrecursive_is_respected(setup):
    source, _, store, clock, _ = setup
    store.save_client("Noor", [])
    destination = source / "organized"
    make_file(source, "Noor.ai")
    make_file(source, "nested/Noor.psd")
    make_file(destination, "Noor/2026-09/existing.psd")
    engine = AutoOrganizer(store, AutoSettings(source, destination), clock)
    assert settle(engine, clock).processed == 2
    assert engine.cycle().waiting == 0
    assert len(store.history()) == 1
    flat = AutoOrganizer(store, replace(engine.settings, recursive=False), clock)
    assert flat.cycle().waiting == 1
    clock.advance(8)
    assert flat.cycle().batch is None
    assert len(store.history()) == 1


def test_failed_fingerprint_does_not_retry_until_requested(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor_good.ai")
    bad = make_file(source, "Noor_bad.ai")
    copy = core._copy_new
    attempts = []

    def denied(path, *args, **kwargs):
        attempts.append(path)
        if path == bad:
            raise PermissionError("test destination permission denied")
        return copy(path, *args, **kwargs)

    monkeypatch.setattr(core, "_copy_new", denied)
    result = settle(engine, clock)
    assert (result.processed, result.batch.failed) == (1, 1)
    assert [item.source for item in result.scan.items] == [bad]
    assert "permission denied" in result.errors[0]
    for _ in range(3):
        result = engine.cycle()
        assert result.batch is None
        assert len(result.errors) == 1
        assert len(result.scan.items) == 1
    assert len(attempts) == 2
    assert len(store.history()) == 1
    monkeypatch.setattr(core, "_copy_new", copy)
    engine.retry()
    assert engine.cycle().processed == 1
    assert len(store.history()) == 2
    assert engine.cycle().batch is None
    assert len(list(destination.rglob("*.ai"))) == 2


def test_changed_failed_file_can_retry_without_clearing_successes(setup, monkeypatch):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    copy = core._copy_new

    def denied(*args, **kwargs):
        raise PermissionError("test locked destination")

    monkeypatch.setattr(core, "_copy_new", denied)
    assert settle(engine, clock).batch.failed == 1
    original.write_bytes(b"updated bytes")
    os.utime(original, (clock.wall, clock.wall))
    monkeypatch.setattr(core, "_copy_new", copy)
    assert engine.cycle().waiting == 1
    clock.advance(8)
    assert engine.cycle().processed == 1


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_same_metadata_replacement_retries_failed_attempt_after_settling(setup, monkeypatch, mode):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai", b"AAAA")
    replacement = make_file(source, "replacement.tmp", b"BBBB")
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode=mode), clock)

    def denied(*args, **kwargs):
        raise PermissionError("test locked destination")

    with monkeypatch.context() as context:
        context.setattr(core, "_copy_new", denied)
        assert settle(engine, clock).batch.failed == 1
    assert engine.cycle().batch is None
    assert original in engine._failed
    os.replace(replacement, original)
    result = engine.cycle()
    assert (result.waiting, result.batch, result.errors) == (1, None, [])
    assert original not in engine._failed
    clock.advance(8)
    result = engine.cycle()
    assert result.processed == 1
    assert Path(store.operations(result.batch.batch_id)[0]["destination"]).read_bytes() == b"BBBB"
    assert len(store.history()) == 2


def test_destination_resolution_failure_is_cached_until_explicit_retry(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    attempts = []
    destination_for = engine.organizer.destination_for

    def denied(*args, **kwargs):
        attempts.append(1)
        raise PermissionError("test destination resolution denied")

    monkeypatch.setattr(engine.organizer, "destination_for", denied)
    result = settle(engine, clock)
    assert result.batch is None
    assert len(result.scan.items) == 1
    assert "resolution denied" in result.errors[0]
    assert engine.cycle().errors == result.errors
    assert len(attempts) == 1
    assert not destination.exists()
    monkeypatch.setattr(engine.organizer, "destination_for", destination_for)
    engine.retry()
    assert engine.cycle().processed == 1


def test_failed_disappeared_files_do_not_leave_cache_entries(setup, monkeypatch):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")

    def denied(*args, **kwargs):
        raise PermissionError("test denied")

    monkeypatch.setattr(core, "_copy_new", denied)
    assert settle(engine, clock).batch.failed == 1
    assert engine._failed
    original.unlink()
    assert engine.cycle().errors == []
    assert not engine._failed and not engine._observed


def test_rules_change_enables_unknown_and_retries_failure(setup, monkeypatch):
    source, _, store, clock, engine = setup
    client = store.save_client("Noor", [])
    make_file(source, "Unknown.ai")
    make_file(source, "Noor.ai")
    copy = core._copy_new

    def denied(*args, **kwargs):
        raise PermissionError("test locked destination")

    monkeypatch.setattr(core, "_copy_new", denied)
    result = settle(engine, clock)
    assert len(result.scan.items) == 2
    assert result.batch.failed == 1
    monkeypatch.setattr(core, "_copy_new", copy)
    assert engine.cycle().batch is None
    store.add_alias(client.id, "Unknown")
    result = engine.cycle()
    assert result.processed == 2
    assert result.scan.items == []
    store.add_alias(client.id, "Another")
    assert engine.cycle().batch is None
    assert len(store.history()) == 2


def test_cancel_before_start_creates_no_batch_and_keeps_state(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    assert engine.cycle(cancel=lambda: True).batch is None
    assert not store.history()
    assert not destination.exists()
    engine.cycle()
    clock.advance(8)
    assert engine.cycle(cancel=lambda: True).batch is None
    assert not store.history()
    assert engine.cycle().processed == 1


def test_cancel_after_scan_prevents_new_effects(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    engine.cycle()
    clock.advance(8)
    scan = engine.organizer.scan
    stopped = False

    def stop_after_scan(*args, **kwargs):
        nonlocal stopped
        result = scan(*args, **kwargs)
        stopped = True
        return result

    monkeypatch.setattr(engine.organizer, "scan", stop_after_scan)
    result = engine.cycle(cancel=lambda: stopped)
    assert result.batch is None
    assert not store.history()
    assert not destination.exists()


def test_cancel_in_progress_does_not_start_copy_and_can_resume(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    engine.cycle()
    clock.advance(8)
    stopped = False

    def stop(*args):
        nonlocal stopped
        stopped = True

    result = engine.cycle(cancel=lambda: stopped, progress=stop)
    assert result.batch.succeeded == 0
    assert not store.operations(result.batch.batch_id)
    assert not destination.exists()
    assert engine.cycle().processed == 1


def test_chunked_move_cancellation_retains_source_without_failure_cache(setup, monkeypatch):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor_2026-09.ai", b"x" * 300)
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode="move"), clock)
    monkeypatch.setattr(core, "_CHUNK_SIZE", 32)
    target = destination / "Noor" / "2026-09" / original.name
    engine.cycle()
    clock.advance(8)
    result = engine.cycle(cancel=lambda: target.exists() and target.stat().st_size > 0)
    assert result.processed == 0
    assert original.read_bytes() == b"x" * 300
    assert not target.exists()
    assert not engine._failed
    assert engine.cycle().processed == 1
    assert not original.exists()


def test_restart_uses_persisted_copy_history_once_without_empty_batches(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    assert settle(engine, clock).processed == 1
    reopened = Store(store.path)
    restarted = AutoOrganizer(reopened, engine.settings, clock)
    hashes = []
    hash_file = core._hash

    def tracked(path, *args, **kwargs):
        hashes.append(path)
        return hash_file(path, *args, **kwargs)

    monkeypatch.setattr(core, "_hash", tracked)
    assert restarted.cycle().waiting == 1
    assert hashes == []
    clock.advance(8)
    result = restarted.cycle()
    assert (result.batch, result.scan.items, result.processed) == (None, [], 0)
    assert len(hashes) == 2
    assert len(reopened.history()) == 1
    restarted.cycle()
    assert len(hashes) == 2


def test_restart_does_not_trust_changed_historical_output(setup):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai", b"AAAA")
    result = settle(engine, clock)
    target = Path(store.operations(result.batch.batch_id)[0]["destination"])
    stamp = target.stat().st_mtime_ns
    target.write_bytes(b"BBBB")
    os.utime(target, ns=(stamp, stamp))
    restarted = AutoOrganizer(Store(store.path), engine.settings, clock)
    assert settle(restarted, clock).processed == 1
    assert target.read_bytes() == b"BBBB"
    assert target.with_name("Noor (2).ai").read_bytes() == b"AAAA"


def test_retry_or_rules_edit_does_not_redo_session_copy_after_undo(setup):
    source, _, store, clock, engine = setup
    client = store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    result = settle(engine, clock)
    assert engine.organizer.undo(result.batch.batch_id).succeeded == 1
    engine.retry()
    store.add_alias(client.id, "Arabic alias")
    assert engine.cycle().batch is None
    assert len(store.history()) == 1


def test_disappeared_paths_are_pruned_and_recreated_file_waits(setup):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    assert settle(engine, clock).processed == 1
    original.unlink()
    assert engine.cycle().waiting == 0
    assert not engine._observed and not engine._processed and not engine._failed
    make_file(source, "Noor.ai", b"new design")
    assert engine.cycle().waiting == 1
    clock.advance(8)
    assert engine.cycle().processed == 1


def test_subfolder_listing_warnings_do_not_forget_files_or_block_other_files(setup, monkeypatch):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "nested/Noor.ai")
    assert settle(engine, clock).processed == 1
    make_file(source, "Noor_new.ai")
    scandir = core.os.scandir

    def denied(path):
        if Path(path) == original.parent:
            raise PermissionError("test listing denied")
        return scandir(path)

    monkeypatch.setattr(core.os, "scandir", denied)
    result = engine.cycle()
    assert result.errors == result.scan.warnings
    assert "test listing denied" in result.errors[0]
    assert original in engine._processed
    assert len(store.history()) == 1
    assert result.waiting == 1
    clock.advance(8)
    result = engine.cycle()
    assert result.processed == 1
    assert "test listing denied" in result.errors[0]
    assert original in engine._processed


def test_source_root_listing_failure_propagates_without_changing_cache(setup, monkeypatch):
    source, _, store, clock, engine = setup
    store.save_client("Noor", [])
    make_file(source, "Noor.ai")
    assert settle(engine, clock).processed == 1
    observed, processed = engine._observed.copy(), engine._processed.copy()
    scandir = core.os.scandir

    def denied(path):
        if Path(path) == source:
            raise PermissionError("test root listing denied")
        return scandir(path)

    with monkeypatch.context() as context:
        context.setattr(core.os, "scandir", denied)
        with pytest.raises(PermissionError, match="test root listing denied"):
            engine.cycle()
    assert engine._observed == observed
    assert engine._processed == processed
    assert engine.cycle().batch is None
    assert len(store.history()) == 1


def test_unreadable_sources_are_cached_until_explicit_retry(setup, monkeypatch):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    open_file = Path.open
    attempts = []

    def denied(path, *args, **kwargs):
        if path == original:
            attempts.append(path)
            raise PermissionError("test input unreadable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = settle(engine, clock)
    assert result.batch.failed == 1
    assert len(result.scan.items) == 1
    assert "test input unreadable" in result.errors[0]
    assert engine.cycle().errors == result.errors
    assert engine.cycle().batch is None
    assert attempts == [original]
    assert not list(destination.rglob("*.ai"))
    assert len(store.history()) == 1
    monkeypatch.setattr(Path, "open", open_file)
    assert engine.cycle().batch is None
    engine.retry()
    assert engine.cycle().processed == 1


def test_roots_are_validated_even_with_all_files_processed(setup):
    source, destination, store, clock, engine = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    assert settle(engine, clock).processed == 1
    original.unlink()
    source.rmdir()
    with pytest.raises(ValueError):
        engine.cycle()
    source.mkdir()
    for item in destination.rglob("*.ai"):
        item.unlink()
    for directory in sorted(destination.rglob("*"), reverse=True):
        directory.rmdir()
    destination.rmdir()
    destination.write_bytes(b"not a directory")
    with pytest.raises(ValueError):
        engine.cycle()


def test_overlapping_cycle_and_retry_are_rejected(setup, monkeypatch):
    source, _, store, _, engine = setup
    make_file(source, "unknown.ai")
    scan = engine.organizer.scan

    def nested(*args, **kwargs):
        with pytest.raises(RuntimeError):
            engine.cycle()
        with pytest.raises(RuntimeError):
            engine.retry()
        return scan(*args, **kwargs)

    monkeypatch.setattr(engine.organizer, "scan", nested)
    assert engine.cycle().waiting == 1
    assert not store.history()


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "delete"},
        {"date_mode": "invalid"},
        {"settle_seconds": -1},
        {"settle_seconds": float("inf")},
        {"settle_seconds": float("nan")},
    ],
)
def test_invalid_settings_fail_closed(setup, changes):
    source, destination, _, _, _ = setup
    with pytest.raises(ValueError):
        AutoSettings(source, destination, **changes)


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Noor_logo_2026-09.psd", "Noor"),
        ("النور_كارت_٢٠٢٦-٠٩.cdr", "النور"),
        ("نور للتجارة_شعار_نهائي.ai", "نور للتجارة"),
        ("نُور_شِعَار.ai", "نُور"),
        ("نُورٌ_كارت.ai", "نُورٌ"),
        ("نور للتجارة_شِعَار_نهائي.ai", "نور للتجارة"),
        ("نُور_شِعَار/final.ai", "نُور"),
        ("شِعَار_نهائي.ai", ""),
        ("شِعَار/نهائي.ai", ""),
        ("تَصْمِيم_نهائي.ai", ""),
        ("Noor Studio-banner-final-v2.psd", "Noor Studio"),
        ("ACME_v2_logo.ai", "ACME"),
        ("Acme-2026-09-final.ai", "Acme"),
        ("New Client/banner_final.psd", ""),
        ("Noor Studio/designs/final.psd", "Noor Studio"),
        ("النور/تصميمات/نهائي.ai", "النور"),
        ("designs/Noor_logo.ai", "Noor"),
        ("logo_2026-09.ai", ""),
        ("تصميم_نهائي.cdr", ""),
        ("123_كارت.cdr", ""),
        ("AB_logo.ai", ""),
        ("Noor.ai", "Noor"),
        ("Backup_of_Lara 17.cdr", "Lara"),
        ("Backup_of_Backup_of_Lara 18.cdr", "Lara"),
        ("bAcKuP_oF_BACKUP_OF_LaRa 19.cdr", "LaRa"),
        ("Backup of Backup-of-Lara 20.cdr", "Lara"),
        ("Backup_of_Backup_of_ام صهيب.cdr", "ام صهيب"),
        ("Backup_of_Backup_of_أُمّ صُهَيْب.cdr", "أُمّ صُهَيْب"),
        ("Backup_of_Backup_of_نُورٌ_شِعَار.ai", "نُورٌ"),
        ("Backup_of_شِعَار_نهائي.ai", ""),
        ("Backup_of_Backup_of_Lara/final.cdr", "Lara"),
        ("6X5.cdr", ""),
        ("Backup_of_6X5.cdr", ""),
        ("Backup_of_Backup_of_6X5.cdr", ""),
        ("Backup_of_Backup_of_6XX5.cdr", ""),
        ("Backup_of_Backup_of_.cdr", ""),
        ("larh 1xx6.cdr", "larh"),
        ("Backup_of_larh 1XX6.cdr", "larh"),
        ("Lara 6X5.cdr", "Lara"),
        ("Lara_Backup_of_Studio 17.cdr", "Lara_Backup_of_Studio"),
        ("Lara Backup of Studio 17.cdr", "Lara Backup of Studio"),
        ("Backup_of_Lara_Backup_of_Studio 17.cdr", "Lara_Backup_of_Studio"),
        ("Backup_office 17.cdr", "Backup_office"),
        ("Backups_of_Lara 17.cdr", "Backups_of_Lara"),
    ],
)
def test_suggest_alias_conservative_prefix_or_relative_folder(setup, filename, expected):
    source, destination, store, _, _ = setup
    make_file(source, filename)
    scan = Organizer(store).scan(source, destination, verify_duplicates=False)
    assert suggest_alias(scan.items[0], source) == expected
    assert store.clients() == []


def test_discovery_groups_normalized_spelling_but_never_fuzzy_merges(setup):
    source, destination, store, _, _ = setup
    client = store.save_client("النور", ["Noor"])
    for filename in (
        "Noor_logo_2026-09.psd",
        "NOOR_banner.ai",
        "Nooor_card.ai",
        "النور_كارت.cdr",
        "شركة جديدة/final.ai",
        "شركة جديدة/designs/logo.psd",
        "logo_final.ai",
    ):
        make_file(source, filename)
    scan = Organizer(store).scan(source, destination, verify_duplicates=False)
    before = store.clients()
    candidates = discover_clients(scan, before)
    by_key = {core._normalize(candidate.name): candidate for candidate in candidates}
    assert by_key["النور"].files == 3
    assert by_key["النور"].existing_client_id == client.id
    assert by_key["nooor"] == ClientCandidate("Nooor", 1)
    assert by_key["شركة جديدة"] == ClientCandidate("شركة جديدة", 2)
    assert len(candidates) == 3
    assert store.clients() == before
    assert not store.history()
    assert not destination.exists()
    assert discover_clients(replace(scan, items=scan.items * 2), before) == candidates


def test_discovery_groups_backups_without_transliteration_or_side_effects(setup):
    source, destination, store, _, _ = setup
    store.save_client("لارا", [])
    filenames = (
        "Backup_of_Lara 17.cdr",
        "Lara 16.cdr",
        "Backup_of_Backup_of_Lara 18.cdr",
        "Backup_of_Backup_of_ام صهيب.cdr",
        "ام صهيب.cdr",
        "6X5.cdr",
        "Backup_of_6X5.cdr",
        "Backup_of_Backup_of_6X5.cdr",
        "larh 1xx6.cdr",
    )
    for filename in filenames:
        make_file(source, filename)
    before = store.clients()
    scan = Organizer(store).scan(source, destination, verify_duplicates=False)
    assert len(scan.items) == 9
    assert all(item.client_id is None for item in scan.items)
    assert discover_clients(scan, before) == [
        ClientCandidate("Lara", 3),
        ClientCandidate("larh", 1),
        ClientCandidate("ام صهيب", 2),
    ]
    assert store.clients() == before
    assert not store.history()
    assert not destination.exists()
    assert {path.name for path in source.iterdir()} == set(filenames)
    assert all((source / filename).read_bytes() == b"design" for filename in filenames)


def test_discovery_honors_known_client_even_with_unrecognized_design_suffix(setup):
    source, destination, store, _, _ = setup
    client = store.save_client("Alnoor", [])
    make_file(source, "Alnoor_brand_2026-09.ai")
    make_file(source, "Alnoor_unrecognizeddescriptor_2026-09.ai")
    make_file(source, "Different Folder/Alnoor_corporate_2026-09.ai")
    scan = Organizer(store).scan(source, destination, verify_duplicates=False)
    assert discover_clients(scan, store.clients()) == [ClientCandidate("Alnoor", 3, client.id)]


def test_discovery_groups_diacritized_names_without_design_word_candidates(setup):
    source, destination, store, _, _ = setup
    for filename in ("نُور_شِعَار.ai", "نور_كارت.ai", "شِعَار_نهائي.ai"):
        make_file(source, filename)
    scan = Organizer(store).scan(source, destination, verify_duplicates=False)
    scan.items.sort(key=lambda item: item.source.name != "نُور_شِعَار.ai")
    assert discover_clients(scan, []) == [ClientCandidate("نُور", 2)]
    assert store.clients() == []


def test_suggestion_rejects_paths_outside_source_and_unsafe_aliases(setup):
    source, destination, store, _, _ = setup
    make_file(source, "Noor.ai")
    item = Organizer(store).scan(source, destination).items[0]
    assert suggest_alias(replace(item, source=source.parent / "Other.ai"), source) == ""
    assert suggest_alias(replace(item, source=source / ".." / "Other.ai"), source) == ""
    assert suggest_alias(replace(item, source=source / "CON_logo.ai"), source) == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS named streams")
def test_auto_move_retains_named_stream_and_caches_refusal(setup):
    source, destination, store, clock, _ = setup
    store.save_client("Noor", [])
    original = make_file(source, "Noor.ai")
    stream = Path(str(original) + ":artwork-metadata")
    try:
        stream.write_bytes(b"extra artwork")
    except OSError:
        pytest.skip("Temporary filesystem does not support named streams")
    engine = AutoOrganizer(store, AutoSettings(source, destination, mode="move"), clock)
    result = settle(engine, clock)
    assert result.batch.failed == 1
    assert original.read_bytes() == b"design"
    assert stream.read_bytes() == b"extra artwork"
    assert not destination.exists()
    assert engine.cycle().batch is None
    assert len(store.history()) == 1
