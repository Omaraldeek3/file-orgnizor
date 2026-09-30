"""Opt-in, serial polling automation. No timers, startup hooks, or external services."""

from __future__ import annotations

import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path

from .core import (
    BatchResult,
    Cancel,
    Client,
    Organizer,
    Progress,
    ScanItem,
    ScanResult,
    Store,
    _Cancelled,
    _check_cancel,
    _component,
    _normalize,
    _roots,
)

_Fingerprint = tuple[int, int, tuple[int, int] | None]


@dataclass(frozen=True)
class AutoSettings:
    source: Path
    destination: Path
    mode: str = "copy"
    date_mode: str = "filename"
    recursive: bool = True
    settle_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.mode not in {"copy", "move"}:
            raise ValueError("وضع العملية غير صالح")
        if self.date_mode not in {"filename", "modified"}:
            raise ValueError("طريقة التاريخ غير صالحة")
        if not math.isfinite(self.settle_seconds) or self.settle_seconds < 0:
            raise ValueError("مدة استقرار الملف يجب أن تكون رقماً موجباً أو صفراً")


@dataclass
class AutoCycle:
    """Review-only scan; processed counts new transfers in this cycle, not history."""

    scan: ScanResult
    batch: BatchResult | None
    waiting: int
    processed: int
    errors: list[str]


@dataclass(frozen=True)
class _Observation:
    fingerprint: _Fingerprint
    since: float


class AutoOrganizer:
    """Call cycle only while explicitly enabled by the user in this session.

    Successful identity/metadata fingerprints stay suppressed after retry() or a
    rules edit. A new engine verifies persisted copy history once, using hashes.
    The parent must stop automation before manual operations or undo. Root and
    store errors propagate so the parent can pause instead of retrying blindly.
    """

    def __init__(self, store: Store, settings: AutoSettings, clock: Callable = time.monotonic):
        self.store = store
        self.settings = settings
        self.clock = clock
        self.organizer = Organizer(store)
        self._lock = threading.Lock()
        self._observed: dict[Path, _Observation] = {}
        self._processed: dict[Path, _Fingerprint] = {}
        self._failed: dict[Path, tuple[_Fingerprint, str]] = {}
        self._rules: tuple[Client, ...] | None = None

    def retry(self) -> None:
        """Forget failed attempts only; successful files are never requeued."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("دورة التنظيم التلقائي ما زالت جارية")
        try:
            self._failed.clear()
        finally:
            self._lock.release()

    def cycle(self, cancel: Cancel | None = None, progress: Progress | None = None) -> AutoCycle:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("دورة التنظيم التلقائي ما زالت جارية")
        try:
            return self._cycle(cancel, progress)
        finally:
            self._lock.release()

    def _cycle(self, cancel: Cancel | None, progress: Progress | None) -> AutoCycle:
        settings = self.settings
        source, destination = _roots(settings.source, settings.destination)
        review = ScanResult(source, destination, [], [])
        result = AutoCycle(review, None, 0, 0, [])
        try:
            _check_cancel(cancel)
            rules = tuple(self.store.clients())
            if rules != self._rules:
                self._failed.clear()
                self._rules = rules
            scanned = self.organizer.scan(
                source,
                destination,
                settings.date_mode,
                settings.recursive,
                cancel,
                verify_duplicates=False,
                check_readable=False,
            )
            review.warnings.extend(scanned.warnings)
            result.errors.extend(scanned.warnings)
            _check_cancel(cancel)
            live = {item.source for item in scanned.items}
            # A listing/permission error is not evidence that a path disappeared.
            for path in self._observed.keys() - live:
                try:
                    path.lstat()
                except FileNotFoundError:
                    self._observed.pop(path, None)
                    self._processed.pop(path, None)
                    self._failed.pop(path, None)
                except OSError:
                    pass
            now, wall_time = self.clock(), time.time()
            ready: list[ScanItem] = []
            for item in scanned.items:
                _check_cancel(cancel)
                path = item.source
                fingerprint = (item.size, item.mtime_ns, item.source_identity)
                previous = self._observed.get(path)
                if previous is None or previous.fingerprint != fingerprint:
                    self._observed[path] = _Observation(fingerprint, now)
                    self._processed.pop(path, None)
                    self._failed.pop(path, None)
                    result.waiting += 1
                    continue
                if self._processed.get(path) == fingerprint:
                    continue
                if (
                    item.size == 0
                    or now - previous.since < settings.settle_seconds
                    or wall_time - item.mtime_ns / 1_000_000_000 < settings.settle_seconds
                ):
                    result.waiting += 1
                    continue
                failure = self._failed.get(path)
                if failure is not None and failure[0] == fingerprint:
                    review.items.append(replace(item, reason=failure[1]))
                    result.errors.append(failure[1])
                    continue
                if item.client_id is None or item.confidence != 1.0:
                    review.items.append(item)
                    continue
                try:
                    if settings.mode == "copy" and self.organizer._already_copied(
                        item, self.organizer.destination_for(item, destination), cancel
                    ):
                        self._processed[path] = fingerprint
                        continue
                except (OSError, ValueError) as error:
                    message = f"{path.name}: {error}"
                    self._failed[path] = (fingerprint, message)
                    review.items.append(replace(item, reason=message))
                    result.errors.append(message)
                    continue
                ready.append(item)
            _check_cancel(cancel)
            if ready:
                result.batch = self.organizer.execute(
                    ScanResult(source, destination, ready, []),
                    mode=settings.mode,
                    progress=progress,
                    cancel=cancel,
                )
                result.processed = result.batch.succeeded
                result.errors.extend(result.batch.errors)
                items = {item.source: item for item in ready}
                for operation in self.store.operations(result.batch.batch_id):
                    path = Path(operation["source"])
                    item = items[path]
                    fingerprint = (item.size, item.mtime_ns, item.source_identity)
                    if operation["status"] in {"completed", "duplicate"}:
                        self._processed[path] = fingerprint
                    elif operation["status"] in {"failed", "recovery_required"}:
                        message = operation["error"]
                        self._failed[path] = (fingerprint, message)
                        review.items.append(replace(item, reason=message))
        except _Cancelled:
            if not any("إلغاء" in warning for warning in review.warnings):
                message = "تم إلغاء دورة التنظيم التلقائي"
                review.warnings.append(message)
                result.errors.append(message)
        return result


@dataclass(frozen=True)
class ClientCandidate:
    name: str
    files: int
    existing_client_id: int | None = None


_DESIGN_WORDS = {
    _normalize(word)
    for word in (
        "logo banner card business businesscard brand branding flyer poster brochure design designs "
        "artwork final draft copy "
        "version revision front back print ready new old export exports files file incoming clients "
        "organized source psd psb ai cdr cdt "
        "لوجو شعار بانر بنر كارت كروت بطاقة بطاقه بوستر فلاير بروشور تصميم تصاميم تصميمات "
        "نهائي نهائى نهائية نهائيه نسخة نسخه فينال اصدار إصدار امام خلف طباعة طباعه جديد قديم "
        "ملفات ملف عملاء العملاء تصدير هوية هويه"
    ).split()
}


def _candidate_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r"^(?:backup[ _-]+of[ _-]+)+", "", value, flags=re.IGNORECASE)
    tokens = (
        "".join(characters)
        for word, characters in groupby(
            value,
            key=lambda character: character.isalnum() or unicodedata.category(character).startswith("M"),
        )
        if word
    )
    prefix: list[str] = []
    for token in tokens:
        normalized = _normalize(token)
        if (
            normalized in _DESIGN_WORDS
            or normalized.isdecimal()
            or re.fullmatch(r"(?:v|ver|version|rev|نسخة|نسخه)\d+", normalized)
            or re.fullmatch(r"\d+x+\d+", normalized)
        ):
            break
        prefix.append(token)
    if not prefix:
        return ""
    # Keep the original spelling/diacritics; normalize only to classify and group.
    end = 0
    for token in prefix:
        end = value.index(token, end) + len(token)
    name = value[:end].strip(" _-.()[]")
    if sum(c.isalpha() for c in _normalize(name)) < 3:
        return ""
    try:
        _component(name, maximum=120)
    except ValueError:
        return ""
    return name


def suggest_alias(item: ScanItem, source_root: Path) -> str:
    """Suggest a readable prefix/folder, never save or confirm a matching rule."""
    try:
        relative = item.source.relative_to(source_root)
    except ValueError:
        return ""
    if ".." in relative.parts:
        return ""
    if len(relative.parts) > 1:
        folder = _candidate_name(relative.parts[0])
        if folder:
            return folder
    return _candidate_name(item.source.stem)


def discover_clients(scan: ScanResult, clients: list[Client]) -> list[ClientCandidate]:
    """Group conservative suggestions for an explicit import checklist, without I/O.

    Confirmed scan assignments group under their canonical client names. Other
    candidates identify existing clients only by normalized exact name/alias;
    fuzzy names stay separate rather than silently merging different clients.
    """
    aliases: dict[str, set[int]] = {}
    known = {client.id: client for client in clients}
    for client in clients:
        for alias in (client.name, *client.aliases):
            aliases.setdefault(_normalize(alias), set()).add(client.id)
    candidates: dict[str, ClientCandidate] = {}
    seen: set[Path] = set()
    for item in scan.items:
        if item.source in seen:
            continue
        seen.add(item.source)
        matched = known.get(item.client_id)
        name = matched.name if matched else suggest_alias(item, scan.source)
        if not name:
            continue
        normalized = _normalize(name)
        owners = aliases.get(normalized, set())
        existing = next(iter(owners)) if len(owners) == 1 else None
        if matched:
            existing = matched.id
        current = candidates.get(normalized)
        candidates[normalized] = ClientCandidate(
            current.name if current else name, (current.files if current else 0) + 1, existing
        )
    return sorted(candidates.values(), key=lambda candidate: _normalize(candidate.name))
