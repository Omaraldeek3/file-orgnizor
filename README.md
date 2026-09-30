# منظّم الاستوديو — Smart File Organizer

تطبيق سطح مكتب لويندوز يرتّب ملفات التصميم (CorelDRAW و Photoshop و Illustrator) في أرشيف
مرتّب حسب **العميل ثم الشهر**، محليًا بالكامل ودون رفع أي ملف.

A local-first Windows desktop app that sorts design files into an archive by **client, then month**.
Arabic and English file names are both understood. Nothing leaves your computer.

## What it does

- Scans a work folder for `.cdr`, `.cdt`, `.psd`, `.psb` and `.ai` files.
- Matches each file to a client, even when the name is written differently
  (`Al Noor`, `Alnoor`, `النور`, `El Nour`), using fuzzy Arabic/Latin matching and client aliases.
- Takes the month from a date in the file name, or from the last-modified time.
- Previews every move before it happens. Safe copy is the default; moving originals is optional.
- Keeps a history of every batch, so any run can be undone.
- Optional background assistant that organizes new files once they stop changing.

## Install (Windows)

1. Install Python 3.11–3.14 from python.org and tick "Add Python to PATH".
2. Double-click `setup.bat` once.
3. Double-click `run.bat` to open the app.

Or from a terminal:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python main.py
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT, see [LICENSE](LICENSE).
