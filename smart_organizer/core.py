from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz
from unidecode import unidecode

SUPPORTED_EXTENSIONS = frozenset({".cdr", ".cdt", ".psd", ".psb", ".ai"})
_CHUNK_SIZE = 1024 * 1024
_ARABIC = str.maketrans("أإآٱىیئک", "اااايييك")
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
# A numeric token after YYYY-MM is a day candidate, not a revision. After a
# complete YYYY-MM-DD, separate numeric revision/dimension tokens are allowed.
_DATE = re.compile(r"(?<!\d)(\d{4})[-_ ]+(\d{2})(?:[-_ ]+(\d{2})(?!\d)|(?!\d|[-_ ]+\d))")
Progress = Callable[[int, int, str], None]
Cancel = Callable[[], bool]


if os.name == "nt":

    class _StreamData(ctypes.Structure):
        _fields_ = [("StreamSize", ctypes.c_longlong), ("cStreamName", wintypes.WCHAR * 296)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.FindFirstStreamW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(_StreamData),
        wintypes.DWORD,
    ]
    _kernel32.FindFirstStreamW.restype = wintypes.HANDLE
    _kernel32.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_StreamData)]
    _kernel32.FindNextStreamW.restype = wintypes.BOOL
    _kernel32.FindClose.argtypes = [wintypes.HANDLE]
    _kernel32.FindClose.restype = wintypes.BOOL
    _kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    _kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _kernel32.GetVolumeInformationW.restype = wintypes.BOOL


@dataclass(frozen=True)
class Client:
    id: int
    name: str
    aliases: tuple[str, ...]


@dataclass
class ScanItem:
    source: Path
    size: int
    mtime_ns: int
    month: str
    client_id: int | None
    suggested_client_id: int | None
    confidence: float
    reason: str
    source_identity: tuple[int, int] | None = None


@dataclass
class ScanResult:
    source: Path
    destination: Path
    items: list[ScanItem]
    warnings: list[str]


@dataclass
class BatchResult:
    batch_id: str
    succeeded: int
    failed: int
    skipped: int
    errors: list[str]


class _Cancelled(Exception):
    pass


class _RecoveryRequired(Exception):
    pass


def _check_cancel(cancel: Cancel | None) -> None:
    if cancel is not None and cancel():
        raise _Cancelled("تم إلغاء العملية")


def _notify(progress: Progress | None, done: int, total: int, message: str) -> None:
    if progress is not None:
        try:
            progress(done, total, message)
        except Exception:
            # An observer must not interrupt a transfer between verification and journaling.
            pass


def _digits(value: str) -> str:
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value)


def _normalize(value: str) -> str:
    value = _digits(unicodedata.normalize("NFKC", value)).translate(_ARABIC).casefold()
    value = "".join(c for c in value if not unicodedata.category(c).startswith("M") and c != "ـ")
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def _component(value: str, maximum: int = 255) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or value.endswith("."):
        raise ValueError("اسم المجلد أو الملف فارغ أو ينتهي بمسافة أو نقطة")
    if value in {".", ".."} or any(
        c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C") for c in value
    ):
        raise ValueError("الاسم يحتوي على محارف غير آمنة في Windows")
    portable = unicodedata.normalize("NFKC", value).casefold().split(".")[0].rstrip()
    if portable in _RESERVED or len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError("الاسم محجوز في Windows أو طويل جداً")
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _key(path: Path) -> str:
    return str(_absolute(path)).casefold()


def _within(path: Path, root: Path) -> bool:
    child = tuple(part.casefold() for part in _absolute(path).parts)
    parent = tuple(part.casefold() for part in _absolute(root).parts)
    return child[: len(parent)] == parent


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if not getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return False
    tag = getattr(info, "st_reparse_tag", 0)
    # Name-surrogate tags redirect paths. OneDrive cloud placeholders do not.
    return not tag or bool(tag & 0x20000000)


def _safe_path(path: Path) -> None:
    path = _absolute(path)
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise ValueError(f"لا يسمح بالروابط أو نقاط إعادة التوجيه: {part}")


def _regular(path: Path) -> os.stat_result:
    _safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"ليس ملفاً عادياً: {path}")
    _assert_no_named_streams(path)
    return info


def _assert_no_named_streams(path: Path) -> None:
    if os.name != "nt":
        return
    filename = str(_absolute(path))
    if not filename.startswith("\\\\?\\"):
        filename = "\\\\?\\UNC\\" + filename[2:] if filename.startswith("\\\\") else "\\\\?\\" + filename
    data = _StreamData()
    failure = f"تعذر التحقق بأمان من تدفقات NTFS الإضافية؛ لم يتم تغيير الملف الأصلي: {path}"
    handle = _kernel32.FindFirstStreamW(filename, 0, ctypes.byref(data), 0)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: enumeration found no streams.
            return
        if error in {1, 50, 87, 120}:
            # An unsupported enumeration is safe only if the volume explicitly
            # reports that it cannot store named streams (for example FAT/exFAT).
            root = ctypes.create_unicode_buffer(32768)
            flags = wintypes.DWORD()
            if _kernel32.GetVolumePathNameW(filename, root, len(root)) and _kernel32.GetVolumeInformationW(
                root.value, None, 0, None, None, ctypes.byref(flags), None, 0
            ):
                if not flags.value & 0x00040000:  # FILE_NAMED_STREAMS
                    return
        raise OSError(f"{failure} (Windows {error})")
    try:
        while True:
            if data.cStreamName.casefold() != "::$data":
                raise ValueError(
                    f"الملف يحتوي على تدفقات NTFS إضافية غير مدعومة بأمان؛ لم يتم تغيير الملف الأصلي: {path}"
                )
            if not _kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error == 38:
                    break
                raise OSError(f"{failure} (Windows {error})")
    finally:
        _kernel32.FindClose(handle)


def _roots(source: Path, destination: Path) -> tuple[Path, Path]:
    source, destination = _absolute(source), _absolute(destination)
    _safe_path(source)
    _safe_path(destination)
    if not source.is_dir():
        raise ValueError("مجلد المصدر غير موجود أو غير قابل للقراءة")
    if _within(source, destination):
        raise ValueError("يجب اختلاف المصدر والوجهة، ولا يجوز أن يكون المصدر داخل الوجهة")
    if destination.exists() and not destination.is_dir():
        raise ValueError("الوجهة ليست مجلداً")
    return source, destination


def _case_entry(path: Path) -> Path | None:
    _safe_path(path.parent)
    if not path.parent.is_dir():
        return None
    with os.scandir(path.parent) as entries:
        for entry in entries:
            if entry.name.casefold() == path.name.casefold():
                return Path(entry.path)
    return None


def _ensure_directory(path: Path) -> Path:
    path = _absolute(path)
    _safe_path(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        target = current / part
        existing = _case_entry(target)
        if existing is None:
            _component(part)
            try:
                target.mkdir()
            except FileExistsError:
                pass
            existing = target
        _safe_path(existing)
        if not existing.is_dir():
            raise ValueError(f"المسار ليس مجلداً آمناً: {existing}")
        current = existing
    return current


def _same_stat(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) == (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    )


def _hash(path: Path, cancel: Cancel | None = None) -> tuple[str, os.stat_result]:
    before = _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if not _same_stat(before, os.fstat(stream.fileno())):
            raise ValueError(f"تغير الملف أثناء فتحه: {path}")
        while True:
            _check_cancel(cancel)
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        if not _same_stat(before, os.fstat(stream.fileno())):
            raise ValueError(f"تغير الملف أثناء قراءته: {path}")
    if not _same_stat(before, _regular(path)):
        raise ValueError(f"تغير الملف أثناء قراءته: {path}")
    return digest.hexdigest(), before


def _verify(path: Path, expected: os.stat_result, digest: str, cancel: Cancel | None = None) -> None:
    current = _regular(path)
    if not _same_stat(current, expected):
        raise ValueError(f"تغير الملف؛ تم رفض العملية لحمايته: {path}")
    actual, current = _hash(path, cancel)
    if actual != digest or not _same_stat(current, expected):
        raise ValueError(f"تغير محتوى الملف؛ تم رفض العملية لحمايته: {path}")


def _remove_verified(
    path: Path,
    expected: os.stat_result,
    digest: str,
    cancel: Cancel | None = None,
    safeguard: tuple[Path, os.stat_result, str] | None = None,
) -> None:
    _verify(path, expected, digest, cancel)
    if safeguard is not None:
        _verify(*safeguard, cancel=cancel)
    _check_cancel(cancel)
    if safeguard is not None and not _same_stat(_regular(safeguard[0]), safeguard[1]):
        raise ValueError(f"تغيرت النسخة الاحتياطية قبل الحذف: {safeguard[0]}")
    if not _same_stat(_regular(path), expected):
        raise ValueError(f"تغير الملف قبل الحذف: {path}")
    path.unlink()


def _copy_new(
    source: Path, target: Path, expected: os.stat_result, cancel: Cancel | None
) -> tuple[str, os.stat_result]:
    _check_cancel(cancel)
    if not _same_stat(_regular(source), expected):
        raise ValueError(f"تغير المصدر منذ المعاينة: {source}")
    _safe_path(target)
    if _case_entry(target) is not None:
        raise FileExistsError(f"الملف موجود بالفعل: {target}")
    digest = hashlib.sha256()
    created: os.stat_result | None = None
    written = 0
    try:
        with source.open("rb") as reader:
            if not _same_stat(os.fstat(reader.fileno()), expected):
                raise ValueError(f"تغير المصدر أثناء فتحه: {source}")
            with target.open("xb", buffering=0) as writer:
                created = os.fstat(writer.fileno())
                while True:
                    _check_cancel(cancel)
                    chunk = reader.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    remaining = memoryview(chunk)
                    while remaining:
                        _check_cancel(cancel)
                        count = writer.write(remaining)
                        if not count:
                            raise OSError("تعذرت كتابة البيانات إلى الوجهة")
                        digest.update(remaining[:count])
                        written += count
                        remaining = remaining[count:]
                writer.flush()
                os.fsync(writer.fileno())
                created = os.fstat(writer.fileno())
            if not _same_stat(os.fstat(reader.fileno()), expected):
                raise ValueError(f"تغير المصدر أثناء النسخ: {source}")
        if not _same_stat(_regular(source), expected) or written != expected.st_size:
            raise ValueError(f"تغير المصدر أثناء النسخ: {source}")
        current = _regular(target)
        if (current.st_dev, current.st_ino, current.st_size) != (created.st_dev, created.st_ino, written):
            raise ValueError(f"تغيرت النسخة قبل التحقق: {target}")
        os.utime(
            target,
            ns=(expected.st_mtime_ns, expected.st_mtime_ns),
            follow_symlinks=os.utime not in os.supports_follow_symlinks,
        )
        copied_digest, copied_stat = _hash(target, cancel)
        if copied_digest != digest.hexdigest():
            raise ValueError(f"فشل التحقق من النسخة: {target}")
        return copied_digest, copied_stat
    except Exception as error:
        if created is not None:
            removed = False
            try:
                current_digest, current = _hash(target)
                if (
                    (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
                    and current.st_size == written
                    and current_digest == digest.hexdigest()
                ):
                    _remove_verified(target, current, current_digest)
                    removed = True
            except FileNotFoundError:
                removed = True
            except (OSError, ValueError):
                # Keep an externally changed or unverifiable file for manual recovery.
                pass
            if not removed:
                raise _RecoveryRequired(f"{error}؛ تعذر تنظيف النسخة بأمان: {target}") from error
        raise


class Store:
    def __init__(self, path: Path):
        self.path = _absolute(path)
        _safe_path(self.path)
        self.path = _ensure_directory(self.path.parent) / self.path.name
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS aliases (
                    normalized TEXT PRIMARY KEY, value TEXT NOT NULL,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    canonical INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, total INTEGER NOT NULL, succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                    undone INTEGER NOT NULL DEFAULT 0, source_root TEXT NOT NULL,
                    destination_root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    source TEXT NOT NULL, source_key TEXT NOT NULL, destination TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    source_dev TEXT, source_ino TEXT,
                    destination_dev TEXT, destination_ino TEXT, destination_mtime_ns INTEGER,
                    digest TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS operations_source ON operations(source_key, status);
                CREATE INDEX IF NOT EXISTS operations_batch ON operations(batch_id);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        _safe_path(self.path)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def clients(self) -> list[Client]:
        with self._connect() as connection:
            names = connection.execute("SELECT id, name FROM clients ORDER BY name COLLATE NOCASE").fetchall()
            aliases = connection.execute(
                "SELECT client_id, value FROM aliases WHERE canonical=0 ORDER BY rowid"
            ).fetchall()
        return [
            Client(row["id"], row["name"], tuple(a["value"] for a in aliases if a["client_id"] == row["id"]))
            for row in names
        ]

    def save_client(self, name: str, aliases: list[str], client_id: int | None = None) -> Client:
        name = _component(unicodedata.normalize("NFC", name), maximum=120)
        normalized = _normalize(name)
        if not normalized:
            raise ValueError("يجب أن يحتوي اسم العميل على أحرف أو أرقام")
        values = {normalized: name}
        for alias in aliases:
            alias = alias.strip()
            key = _normalize(alias)
            if not key:
                raise ValueError("الاسم البديل فارغ أو لا يحتوي على أحرف أو أرقام")
            if len(alias) > 240 or any(unicodedata.category(c).startswith("C") for c in alias):
                raise ValueError("الاسم البديل طويل أو يحتوي على محارف غير آمنة")
            values.setdefault(key, alias)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in values:
                owner = connection.execute(
                    "SELECT client_id FROM aliases WHERE normalized=?", (key,)
                ).fetchone()
                if owner is not None and owner["client_id"] != client_id:
                    raise ValueError("اسم العميل أو أحد أسمائه البديلة مستخدم لعميل آخر")
            if client_id is None:
                client_id = connection.execute(
                    "INSERT INTO clients(name, name_key) VALUES (?, ?)", (name, normalized)
                ).lastrowid
            else:
                changed = connection.execute(
                    "UPDATE clients SET name=?, name_key=? WHERE id=?", (name, normalized, client_id)
                ).rowcount
                if not changed:
                    raise ValueError("العميل غير موجود")
                connection.execute("DELETE FROM aliases WHERE client_id=?", (client_id,))
            connection.executemany(
                "INSERT INTO aliases(normalized, value, client_id, canonical) VALUES (?, ?, ?, ?)",
                [(key, value, client_id, int(key == normalized)) for key, value in values.items()],
            )
        return Client(client_id, name, tuple(value for key, value in values.items() if key != normalized))

    def delete_client(self, client_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM clients WHERE id=?", (client_id,))

    def add_alias(self, client_id: int, alias: str) -> Client:
        """Append an explicitly approved alias without replacing concurrent edits."""
        alias = unicodedata.normalize("NFC", alias).strip()
        key = _normalize(alias)
        if not key:
            raise ValueError("الاسم البديل فارغ أو لا يحتوي على أحرف أو أرقام")
        if len(alias) > 240 or any(unicodedata.category(c).startswith("C") for c in alias):
            raise ValueError("الاسم البديل طويل أو يحتوي على محارف غير آمنة")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            client = connection.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
            if client is None:
                raise ValueError("العميل غير موجود")
            owner = connection.execute("SELECT client_id FROM aliases WHERE normalized=?", (key,)).fetchone()
            if owner is not None and owner["client_id"] != client_id:
                raise ValueError("اسم العميل أو أحد أسمائه البديلة مستخدم لعميل آخر")
            if owner is None:
                connection.execute(
                    "INSERT INTO aliases(normalized, value, client_id, canonical) VALUES (?, ?, ?, 0)",
                    (key, alias, client_id),
                )
            aliases = connection.execute(
                "SELECT value FROM aliases WHERE client_id=? AND canonical=0 ORDER BY rowid", (client_id,)
            ).fetchall()
            return Client(client_id, client["name"], tuple(row["value"] for row in aliases))

    def get_setting(self, key, default="") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def history(self, limit=100) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (max(0, int(limit)),),
                )
            ]

    def operations(self, batch_id) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM operations WHERE batch_id=? ORDER BY id", (batch_id,)
                )
            ]


def _match(
    filename: str, parents: list[str], clients: list[Client]
) -> tuple[int | None, int | None, float, str]:
    aliases = {client.id: [_normalize(a) for a in (client.name, *client.aliases)] for client in clients}
    contexts = [_normalize(filename), *(_normalize(parent) for parent in parents)]
    for index, text in enumerate(contexts):
        exact = {
            client_id for client_id, names in aliases.items() if any(f" {a} " in f" {text} " for a in names)
        }
        if len(exact) > 1:
            return None, None, 0.0, "أسماء عدة عملاء متطابقة؛ يلزم الاختيار اليدوي"
        if exact:
            client_id = exact.pop()
            return (
                client_id,
                None,
                1.0,
                "تطابق مؤكد في اسم الملف" if index == 0 else "تطابق مؤكد في المجلد الأقرب",
            )

    def latin(text: str) -> str:
        return _normalize(unidecode(text)).replace("gh", "g").replace("kh", "x")

    def skeleton(text: str) -> str:
        return re.sub("[aeiouwy ]", "", latin(text))

    scores: list[tuple[float, int]] = []
    for client_id, names in aliases.items():
        best = 0.0
        for alias in names:
            if len(alias.replace(" ", "")) < 3:
                continue
            for text in contexts:
                words = text.split()
                count = len(alias.split())
                for width in range(max(1, count - 1), count + 2):
                    for start in range(len(words) - width + 1):
                        candidate = " ".join(words[start : start + width])
                        if len(candidate) < 3 or not any(c.isalpha() for c in candidate):
                            continue
                        score = (
                            max(fuzz.ratio(alias, candidate), fuzz.ratio(latin(alias), latin(candidate)))
                            / 100
                        )
                        # A consonant skeleton captures Ahmed/Ahmad, Mohamed and Omar without confirming them.
                        if len(skeleton(alias)) >= 2 and skeleton(alias) == skeleton(candidate):
                            score = max(score, 0.86)
                        best = max(best, min(score, 0.96))
        scores.append((best, client_id))
    scores.sort(reverse=True)
    if scores and scores[0][0] >= 0.78:
        if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.07:
            return None, None, scores[0][0], "اقتراحات متقاربة لعدة عملاء؛ يلزم الاختيار اليدوي"
        return None, scores[0][1], scores[0][0], "اقتراح تقريبي أو نقل صوتي فقط؛ يلزم التأكيد اليدوي"
    return None, None, 0.0, "لم يتم التعرف على العميل؛ سيتم التخطي ما لم يتم اختياره"


def _month(filename: str, info: os.stat_result, mode: str) -> str:
    if mode == "filename":
        for match in _DATE.finditer(_digits(unicodedata.normalize("NFKC", filename))):
            year, month, day = match.groups()
            try:
                found = date(int(year), int(month), int(day or "1"))
            except ValueError:
                continue
            return f"{found.year:04d}-{found.month:02d}"
    return datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m")


class Organizer:
    def __init__(self, store: Store):
        self.store = store

    def scan(
        self,
        source: Path,
        destination: Path,
        date_mode="filename",
        recursive=True,
        cancel: Cancel | None = None,
        *,
        verify_duplicates: bool = True,
        check_readable: bool = True,
    ) -> ScanResult:
        """Root listing failures propagate; inaccessible descendants yield warnings."""
        if date_mode not in {"filename", "modified"}:
            raise ValueError("طريقة التاريخ غير صالحة")
        source, destination = _roots(source, destination)
        result = ScanResult(source, destination, [], [])
        clients = self.store.clients()
        pending = [source]
        try:
            while pending:
                _check_cancel(cancel)
                folder = pending.pop()
                try:
                    _safe_path(folder)
                    with os.scandir(folder) as entries:
                        children = sorted(entries, key=lambda entry: entry.name.casefold())
                except (OSError, ValueError) as error:
                    if folder == source:
                        raise
                    result.warnings.append(f"تعذر قراءة المجلد {folder}: {error}")
                    continue
                for entry in children:
                    _check_cancel(cancel)
                    path = Path(entry.path)
                    if _within(path, destination):
                        continue
                    try:
                        # DirEntry.stat can omit file IDs on Windows.
                        info = path.lstat()
                        if _is_link(info):
                            result.warnings.append(f"تم تخطي رابط أو نقطة إعادة توجيه: {path}")
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if recursive:
                                pending.append(path)
                            continue
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or path.suffix.casefold() not in SUPPORTED_EXTENSIONS
                        ):
                            continue
                        _component(path.name)
                        parents = list(reversed(path.relative_to(source).parts[:-1]))
                        matched, suggested, confidence, reason = _match(path.stem, parents, clients)
                        item = ScanItem(
                            path,
                            info.st_size,
                            info.st_mtime_ns,
                            _month(path.stem, info, date_mode),
                            matched,
                            suggested,
                            confidence,
                            reason,
                            source_identity=(info.st_dev, info.st_ino),
                        )
                        if (
                            verify_duplicates
                            and matched is not None
                            and self._already_copied(item, self.destination_for(item, destination), cancel)
                        ):
                            item.reason += "؛ توجد نسخة سابقة موثقة وسيتم التخطي"
                        # Polling can defer read probes to execute, where failures can be cached.
                        if check_readable:
                            with path.open("rb"):
                                pass
                        result.items.append(item)
                    except (OSError, ValueError) as error:
                        result.warnings.append(f"تعذر فحص {path}: {error}")
        except _Cancelled:
            result.warnings.append("تم إلغاء الفحص؛ النتائج المعروضة جزئية")
        return result

    def destination_for(self, item: ScanItem, destination: Path) -> Path:
        client = next((client for client in self.store.clients() if client.id == item.client_id), None)
        if client is None:
            raise ValueError("يجب اختيار عميل موجود قبل التنفيذ")
        _component(client.name, maximum=120)
        _component(item.source.name)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", item.month):
            raise ValueError("الشهر غير صالح")
        date.fromisoformat(item.month + "-01")
        root = _absolute(destination)
        _safe_path(root)
        target = root / client.name / item.month / item.source.name
        _safe_path(target)
        return target

    def _already_copied(self, item: ScanItem, target: Path, cancel: Cancel | None) -> bool:
        with self.store._connect() as connection:
            rows = connection.execute(
                """SELECT o.* FROM operations o JOIN batches b ON b.id=o.batch_id
                WHERE o.source_key=? AND o.size=? AND o.mtime_ns=? AND b.mode='copy'
                AND o.status IN ('completed', 'undo_failed') ORDER BY o.id DESC""",
                (_key(item.source), item.size, item.mtime_ns),
            ).fetchall()
        rows = [row for row in rows if _key(Path(row["destination"]).parent) == _key(target.parent)]
        if not rows:
            return False
        source_digest, source_info = _hash(item.source, cancel)
        if (source_info.st_size, source_info.st_mtime_ns) != (item.size, item.mtime_ns) or (
            item.source_identity is not None
            and (source_info.st_dev, source_info.st_ino) != item.source_identity
        ):
            return False
        for row in rows:
            if row["digest"] != source_digest:
                continue
            try:
                self._verify_record(Path(row["destination"]), row, destination=True, cancel=cancel)
                return True
            except (OSError, ValueError):
                continue
        return False

    def _operation(self, operation_id: int, status: str, error: str = "", **values) -> None:
        permitted = {
            "destination",
            "source_dev",
            "source_ino",
            "destination_dev",
            "destination_ino",
            "destination_mtime_ns",
            "digest",
        }
        if not values.keys() <= permitted:
            raise ValueError("Invalid journal columns")
        # Windows file IDs can be 128-bit; SQLite INTEGER is signed 64-bit.
        values = {
            key: hex(value) if key.endswith(("_dev", "_ino")) else value for key, value in values.items()
        }
        columns = ["status=?", "error=?", *(f"{key}=?" for key in values)]
        with self.store._connect() as connection:
            connection.execute(
                f"UPDATE operations SET {', '.join(columns)} WHERE id=?",
                (status, error, *values.values(), operation_id),
            )

    def _verify_record(self, path: Path, row, destination: bool, cancel: Cancel | None) -> os.stat_result:
        expected = _regular(path)
        prefix = "destination" if destination else "source"
        mtime = row["destination_mtime_ns"] if destination else row["mtime_ns"]
        if (expected.st_size, expected.st_mtime_ns, hex(expected.st_dev), hex(expected.st_ino)) != (
            row["size"],
            mtime,
            row[f"{prefix}_dev"],
            row[f"{prefix}_ino"],
        ):
            raise ValueError(f"تغير الملف منذ العملية الأصلية: {path}")
        _verify(path, expected, row["digest"], cancel)
        return expected

    def execute(
        self, scan: ScanResult, mode="copy", progress: Progress | None = None, cancel: Cancel | None = None
    ) -> BatchResult:
        if mode not in {"copy", "move"}:
            raise ValueError("وضع العملية غير صالح")
        source_root, destination_root = _roots(scan.source, scan.destination)
        batch_id = uuid.uuid4().hex
        total = len(scan.items)
        result = BatchResult(batch_id, 0, 0, 0, [])
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO batches(id, created_at, mode, status, total, source_root, destination_root) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    datetime.now(timezone.utc).isoformat(),
                    mode,
                    "running",
                    total,
                    str(source_root),
                    str(destination_root),
                ),
            )
        cancelled = False
        for index, item in enumerate(scan.items):
            if cancel is not None and cancel():
                cancelled = True
                result.skipped += total - index
                break
            _notify(progress, index, total, f"جارٍ معالجة {item.source.name}")
            if cancel is not None and cancel():
                cancelled = True
                result.skipped += total - index
                break
            with self.store._connect() as connection:
                operation_id = connection.execute(
                    "INSERT INTO operations(batch_id, source, source_key, status, size, mtime_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        str(_absolute(item.source)),
                        _key(item.source),
                        "pending",
                        item.size,
                        item.mtime_ns,
                    ),
                ).lastrowid
            target = None
            copied_info = None
            digest = ""
            source_deleted = False
            try:
                _check_cancel(cancel)
                if item.client_id is None:
                    self._operation(operation_id, "skipped", "لم يتم تأكيد العميل")
                    result.skipped += 1
                    continue
                path = _absolute(item.source)
                if not _within(path, source_root) or _within(path, destination_root):
                    raise ValueError("الملف خارج المصدر أو داخل الوجهة")
                if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                    raise ValueError("امتداد الملف غير مدعوم")
                source_info = _regular(path)
                if (source_info.st_size, source_info.st_mtime_ns) != (item.size, item.mtime_ns) or (
                    item.source_identity is not None
                    and (source_info.st_dev, source_info.st_ino) != item.source_identity
                ):
                    raise ValueError("تغير المصدر منذ الفحص؛ أعد الفحص قبل التنفيذ")
                target = self.destination_for(item, destination_root)
                self._operation(
                    operation_id,
                    "pending",
                    destination=str(target),
                    source_dev=source_info.st_dev,
                    source_ino=source_info.st_ino,
                )
                if mode == "copy" and self._already_copied(item, target, cancel):
                    self._operation(
                        operation_id, "duplicate", "توجد نسخة سابقة موثقة؛ لم يتم إنشاء نسخة إضافية"
                    )
                    result.skipped += 1
                    continue
                _check_cancel(cancel)
                parent = _ensure_directory(target.parent)
                original_name = target.name
                suffix = 1
                while True:
                    _check_cancel(cancel)
                    name = (
                        original_name
                        if suffix == 1
                        else f"{Path(original_name).stem} ({suffix}){Path(original_name).suffix}"
                    )
                    _component(name)
                    target = parent / name
                    suffix += 1
                    if _case_entry(target) is not None:
                        continue
                    self._operation(operation_id, "copying", destination=str(target))
                    try:
                        digest, copied_info = _copy_new(path, target, source_info, cancel)
                        break
                    except FileExistsError:
                        continue
                self._operation(
                    operation_id,
                    "verified",
                    digest=digest,
                    destination_dev=copied_info.st_dev,
                    destination_ino=copied_info.st_ino,
                    destination_mtime_ns=copied_info.st_mtime_ns,
                )
                _verify(path, source_info, digest, cancel)
                _verify(target, copied_info, digest, cancel)
                _check_cancel(cancel)
                if mode == "move":
                    _remove_verified(
                        path, source_info, digest, cancel, safeguard=(target, copied_info, digest)
                    )
                    source_deleted = True
                self._operation(operation_id, "completed")
                result.succeeded += 1
            except Exception as error:
                recovery = source_deleted or isinstance(error, _RecoveryRequired)
                if copied_info is not None and not source_deleted:
                    try:
                        # The verified source remains the safeguard before discarding our new copy.
                        _verify(path, source_info, digest)
                        _remove_verified(target, copied_info, digest, safeguard=(path, source_info, digest))
                    except (OSError, ValueError):
                        recovery = True
                if isinstance(error, _Cancelled):
                    cancelled = True
                    result.skipped += total - index
                    status = "recovery_required" if recovery else "cancelled"
                else:
                    result.failed += 1
                    status = "recovery_required" if recovery else "failed"
                message = f"{item.source.name}: {error}"
                if recovery:
                    message += "؛ تم الاحتفاظ بالملف للمراجعة اليدوية"
                result.errors.append(message)
                self._operation(operation_id, status, message)
                if cancelled:
                    break
            finally:
                _notify(progress, index + 1, total, item.source.name)
        status = "cancelled" if cancelled else "partial" if result.failed else "completed"
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE batches SET status=?, succeeded=?, failed=?, skipped=? WHERE id=?",
                (status, result.succeeded, result.failed, result.skipped, batch_id),
            )
        return result

    def undo(
        self, batch_id: str, progress: Progress | None = None, cancel: Cancel | None = None
    ) -> BatchResult:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("العملية غير موجودة")
            if batch["status"] in {"running", "undoing"}:
                raise ValueError("العملية جارية أو انقطعت؛ يلزم فحص سجلها قبل الاستعادة")
            connection.execute("UPDATE batches SET status=? WHERE id=?", ("undoing", batch_id))
        rows = list(reversed(self.store.operations(batch_id)))
        total = len(rows)
        result = BatchResult(batch_id, 0, 0, 0, [])
        for index, row in enumerate(rows):
            if cancel is not None and cancel():
                result.skipped += total - index
                break
            if row["status"] not in {"completed", "undo_failed"}:
                result.skipped += 1
                continue
            source, destination = Path(row["source"]), Path(row["destination"])
            restored_info = None
            removed = False
            _notify(progress, index, total, f"جارٍ التراجع عن {source.name}")
            try:
                _check_cancel(cancel)
                if not _within(source, Path(batch["source_root"])) or not _within(
                    destination, Path(batch["destination_root"])
                ):
                    raise ValueError("مسارات السجل خارج المجلدات الأصلية")
                info = self._verify_record(destination, row, destination=True, cancel=cancel)
                self._operation(row["id"], "undoing")
                if batch["mode"] == "copy":
                    original_info = self._verify_record(source, row, destination=False, cancel=cancel)
                    _remove_verified(
                        destination,
                        info,
                        row["digest"],
                        cancel,
                        safeguard=(source, original_info, row["digest"]),
                    )
                    removed = True
                else:
                    _safe_path(source)
                    if _case_entry(source) is not None:
                        raise ValueError("لا يمكن استعادة النقل: مسار المصدر مشغول بملف آخر")
                    parent = _ensure_directory(source.parent)
                    source = parent / source.name
                    restored_digest, restored_info = _copy_new(destination, source, info, cancel)
                    if restored_digest != row["digest"]:
                        raise ValueError("فشل التحقق من الملف المستعاد")
                    _verify(source, restored_info, row["digest"], cancel)
                    _remove_verified(
                        destination,
                        info,
                        row["digest"],
                        cancel,
                        safeguard=(source, restored_info, row["digest"]),
                    )
                    removed = True
                self._operation(row["id"], "undone")
                result.succeeded += 1
            except Exception as error:
                recovery = removed or isinstance(error, _RecoveryRequired)
                if restored_info is not None and not removed:
                    try:
                        self._verify_record(destination, row, destination=True, cancel=None)
                        _remove_verified(
                            source, restored_info, row["digest"], safeguard=(destination, info, row["digest"])
                        )
                    except (OSError, ValueError):
                        recovery = True
                message = f"{source.name}: {error}"
                if recovery:
                    message += "؛ يلزم فحص الملفات يدوياً"
                self._operation(row["id"], "recovery_required" if recovery else "undo_failed", message)
                result.errors.append(message)
                if isinstance(error, _Cancelled):
                    result.skipped += total - index
                    break
                result.failed += 1
            finally:
                _notify(progress, index + 1, total, source.name)
        with self.store._connect() as connection:
            undone = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE batch_id=? AND status='undone'", (batch_id,)
            ).fetchone()[0]
            remaining = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE batch_id=? "
                "AND status IN ('completed', 'undo_failed', 'recovery_required', 'verified', 'copying', 'undoing')",
                (batch_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE batches SET status=?, undone=? WHERE id=?",
                ("undo_partial" if remaining else "undone", undone, batch_id),
            )
        return result
