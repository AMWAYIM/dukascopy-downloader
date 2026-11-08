# Dukascopy Tick Downloader

Downloads historical tick data from the Dukascopy JETTA API into local
**MT5-ready binary files** and imports ticks into **MetaTrader 5**
custom symbols via a bundled MQL5 script.

## Install

```powershell
pip install -r requirements.txt
```

Requires Python 3.10+.

If you are upgrading from an older Parquet-based install, run the one-time
migration after installing:

```powershell
python main.py migrate
```

(`pyarrow` is only needed for that migration step.)

## Web UI

```powershell
python main.py web
# open http://127.0.0.1:8080
```

Minimal white-themed interface for search, **bulk download** (multiple symbols in one
job), MT5 import, gap scan/repair, stored-data library, and live job progress.

## Usage (CLI)

```powershell
# Find an instrument (1600+ in the bundled catalog)
python main.py search gold
python main.py search eurusd

# Download ticks into binary hour files (resumable, concurrent, auto-retrying)
python main.py download EURUSD 2025-01-01 2025-06-30

# One-time: convert legacy .parquet data to .bin
python main.py migrate

# Check for and repair holes in the dataset
python main.py gaps EURUSD 2025-01-01 2025-06-30 --repair

# Scan the entire recorded range (no dates needed)
python main.py gaps EURUSD --all
python main.py gaps EURUSD --all --repair
python main.py gaps EURUSD --all --repair --refetch-empty   # also re-request empty hours

# What is stored locally?
python main.py status EURUSD

# Export a complete stored range to one MT5 tick CSV (tab-separated, no header)
python main.py export-csv EURUSD 2017-01-01 2026-08-21 --output EURUSD_ticks.csv
```

`download` options: `--workers N` (default 15, max 64), `--force`, `--profile`

## How it works

```
plan hours -> fetch JETTA JSON (parallel) -> decode -> verify -> .bin (atomic)
                                                                  |
                 SQLite ledger: completed / empty / failed  <-----+
                                                                  |
                              MT5 import  <-- link hour .bin files --+
```

- **One `.bin` file per instrument-hour** (`data/EURUSD/2025/01/02/14.bin`),
  stored in the same bin_v1 layout MT5 imports. Written atomically via temp file
  + rename and never overwritten.
- **SQLite ledger** (`data/metadata.db`) records every hour's state. Completed
  and empty hours are never re-downloaded, so interrupted runs resume for free
  and progress is never lost.
- **Retry manager**: exponential backoff with jitter for network errors and
  5xx responses; corrupt payloads (JSON/structure/verification failures)
  trigger a fresh fetch. Hours that still fail get extra retry rounds, then
  remain flagged for `gaps --repair`. Temporary failures never abort a run.
- **Planner** clamps ranges to each instrument's earliest available data and
  skips the not-yet-published most recent hours. Hours with no tick data are
  recorded as empty after an empty JETTA response.
- **Verification** checks record structure, timestamp monotonicity and bounds,
  positive prices, and plausible spreads before anything is persisted.
- **Instrument catalog** (`config/instruments.json`) carries display names used
  to resolve JETTA instrument codes, plus data start dates.

## MT5 import

Downloads are already MT5-ready. Import hard-links each hour `.bin` into the
MT5 job folder (no concat copy) and launches MetaTrader 5 with
`DukascopyTickImport.mq5`, which reads `hours.txt` and imports each file
via `CustomTicksReplace`. Large histories are split into bounded batches; the
first batch replaces the custom symbol history and following batches append
their non-overlapping dates. The controller waits for MT5's explicit completion
state rather than treating a quiet progress update as success.

### CSV fallback / manual import

If you prefer MT5's native tick importer, export any complete downloaded range
to one tab-separated, headerless CSV:

```powershell
python main.py export-csv EURUSD 2017-01-01 2026-08-21 --output EURUSD_ticks.csv
```

The command verifies that the selected range has no missing or failed hours,
writes atomically, and preserves millisecond timestamps. In MT5, open the
custom symbol's **Ticks** tab and choose **Import Ticks**. Use the default
six-column mapping: date, time, bid, ask, last, volume.

## Data folder (tick data root)

All downloaded data lives in one **data root** — ticks
(`<SYMBOL>/<year>/<month>/<day>/<hour>.bin`), the SQLite ledger
(`metadata.db`), staging, CSV exports, logs, and web settings. By default
that is the repo's `data/` directory; on a machine where your main drive is
tight you can point it elsewhere.

Resolution order (first match wins):

1. `DUKE_DATA_DIR` environment variable (session override — handy for scripts)
2. `<repo>/.dukascopy/data.json` (persistent; written by `data-config` or the Settings page)
3. Default: `<repo>/data`

Show the effective root:

```powershell
python main.py data-config --show
```

Set a persistent root (absolute path; validated, fail-fast):

```powershell
python main.py data-config --set D:\tickdata
python main.py data-config --clear    # back to the repo default
```

The app refuses to start if the configured root cannot be used — there is no
silent fallback.

### Moving existing data

To move the current dataset to a new root with the app stopped:

```powershell
python main.py migrate-data --to D:\tickdata --delete-source
```

`migrate-data` copies every file, verifies size counts, then deletes the old
root only with `--delete-source`. It refuses while the web server is running
and on non-empty destinations unless `--force`.

With the web UI, **Settings → Tick data folder** shows the root, lets you set
it (a restart banner appears until you restart), and runs the same migration as
a background copy+verify job — the old copy is kept; delete it later with the
CLI command above.

## Choosing paths without typing

The Settings tab and `configure-paths.ps1` let you pick the three key paths
from native Windows dialogs instead of typing them:

- MT5 `terminal64.exe`
- MT5 data folder
- Tick data root

**In the web UI:** each field has a **Browse…** button that opens the standard
Windows dialog and fills + saves the path immediately. When the chosen MT5
data folder equals the terminal folder, portable mode is enabled automatically
(with a visible notice). Browse is only available when the UI is opened from
the machine running the app (localhost); otherwise the buttons hide. Only one
dialog can be open at a time (a second Browse click is ignored until the first
closes), and cancelling never hides the buttons.

**From PowerShell** (no browser needed):

```powershell
.\installer\configure-paths.ps1
```

It prints the current values first, then lets you configure 1/2/3/All and
choose **Browse** or **Auto-detect** for each path. `install.ps1` offers it at
the end of a fresh install.

## Layout

```
core/
  models/        instrument, tick, tick_batch, hour-task
  services/      instrument_search, planner, download_engine,
                 retry_manager, decoder, verification, gap_scanner
storage/         tick_storage, tick_format, metadata_db, parquet_migration
export/          mt5_tick_publisher, mt5_importer, mt5_csv
mt5/             DukascopyTickImport.mq5
config/          settings, instruments.json
cli/             commands
scripts/         migrate_parquet_to_bin.py
data/            binary tick store + SQLite ledger   (default data root; see Data folder)
```

## Git workflow (fork)

This repository is a fork of
[`Pin8al/dukascopy-downloader`](https://github.com/Pin8al/dukascopy-downloader).
`main` is configured to **pull/rebase from `upstream`** and **push to
`origin`** (your fork), so:

```powershell
# Pull latest upstream changes, rebasing local commits on top
git pull

# Push local commits to your fork (never to upstream)
git push
```

- `git pull` never creates merge commits (`pull.rebase` is enabled).
- If a rebase conflicts on a file you also changed, resolve and continue with
  `git rebase --continue`.
- The download job writes only to gitignored `data/` and `.dukascopy/`, so it
  is unaffected by these git operations.
