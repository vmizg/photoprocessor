# Photoprocessor — extended context for Cursor / future chats

This document summarizes how the tooling in this folder works, design decisions, and recent changes. **Open this workspace root** (`photoprocessor`) in Cursor so the agent sees these paths. (An older copy may still exist under `floorplanner/scripts/` — treat this folder as the source of truth if both exist.)

---

## 1. What’s in this repo

| Path | Purpose |
|------|---------|
| **`organize_photos.py`** (repo root) | Thin entry: delegates to **`src.cli`**. Prefer `python organize_photos.py …` from the repo root so you never import `src` by hand. |
| **`src/`** | Implementation package (`cli`, `extractors`, `rules`, `dedupe`, `state_repo`, `io_ops`, …). Also **`python -m src.organize_photos`**. |
| **`src/__tests__/run_organize_photos_tests.py`** | Large integration test: builds a temp tree, runs the root script (dry-run by default), asserts per-file output; optional `--live` real copies + CreationTime. |
| **`src/__tests__/README.md`** | Describes synthetic fixtures. |
| **`onedrive-download.mjs`**, **`clean.py`**, **`dedupe.py`**, **`analyze_photo_paths.py`** | Related utilities (not covered in detail here). |

**Run tests** from this directory:

```bash
python src/__tests__/run_organize_photos_tests.py
python src/__tests__/run_organize_photos_tests.py --live
```

Point tests at a different entry script:

```bash
set ORGANIZE_PHOTOS_SCRIPT=C:\path\to\organize_photos.py
python src/__tests__/run_organize_photos_tests.py
```

---

## 2. `organize_photos` / `src` — core contract

- **Python 3.10+**, **Windows** for CreationTime behavior (`ctypes` `SetFileTime`, PowerShell fallback). Env: **`ORGANIZE_USE_POWERSHELL_CREATION_TIME=1`** forces PowerShell-only path for setting creation time.
- **Source is read-only**: no moves, no timestamp edits on originals.
- **Optional Pillow**: EXIF `DateTimeOriginal` and PNG/text/XMP fallbacks via `_try_exif_datetime_original`.
- **EXIF offsets**: `parse_exif_offset_string` accepts `±HH:MM` (including half-hour zones), compact `±HHMM`, and `Z` / `UTC`. When GPS is used to infer IANA time but `ZoneInfo` or **timezonefinder** fails, verbose hints include **`GPS~no_tz=…`** (reason string), not a silent fallback.
- **Copy timestamps**: For `filename_earlier_than_metadata` / `exif_earlier_than_metadata`, `filesystem_instant_for_rule` attaches EXIF’s offset (or GPS IANA) to naive rule output so `.timestamp()` / Windows file times encode the **same UTC instant** as capture metadata, not “stem digits in the PC’s zone.” PowerShell CreationTime fallback uses Unix milliseconds for the same instant.
- **Year folder** for output: derived from the chosen “capture” time (`final_created` / archive year), not only from filename in isolation.

---

## 3. Dating pipeline (high level)

1. **Per file**, read `created` / `modified` (Windows: “creation” vs last write as exposed by the script), optional **EXIF** datetime.
2. **`rel_parts_for_path_dating`**: directory segments used for **logging** (path-vs-resolved divergence hints) — may use `relative_to(source)` or, when the tree has no calendar hint, `relative_to(source.parent)` so a **dated leaf** as `--source` (e.g. `…/1994-Aug/file.jpg`) still contributes folder context. Folder layout does **not** change dating rules or dedupe scores.
3. **`decide_actions(...)`** returns a list of **`Action`** objects; the **last** action with `new_created` wins for setting time (see `primary_time_action`). Rules are ordered; early returns are intentional.

Rough order inside **`decide_actions`** (simplified):

| Step | Idea |
|------|------|
| EXIF earlier than `min(created, modified)` | `exif_earlier_than_metadata` if not “ambiguous vs mtime” (±1 day logic). |
| Filename datetime **earlier** than that minimum | `filename_earlier_than_metadata` with `filename_includes_time_in_name` when pattern had a clock. TZ-ish ambiguity: filename clock within ±1 day of mtime can suppress filename override. |
| Various **`fallback_created_from_modified`** paths | When EXIF/filename are ambiguous vs mtime but mtime is consistent with capture signals, or `modified < created` with weak hints. |
| **Structured path** `…/yyyy/mm/DD/yyyymmdd-HHMMss/…` | Slug vs mtime agreement → `structured_path_align_created`; mismatch → logging + `structured_path_oldest_signal` (oldest of slug, created, modified). |
| Last resort | `fallback_created_from_modified` if no useful filename/path hints and `modified < created`. |

Constants like **`_TZ_NAIVE_VS_MODIFIED_EQUIV_MAX`** (and friends) define “same instant” windows between naive EXIF/filename and mtime.

After a run, the CLI may log a **Path calendar review hint** when dated folders imply a period that does not contain the resolved time (see `path_calendar_divergence_hint` in `extractors.py`). This is advisory only.

**Invalid folder dates** (e.g. `2024/02/31`): `path_implies_date_period` returns `None`; `path_has_calendar_hint` is false, so bad paths **do not** block fallback behavior.

---

## 4. Filename parsing (`extract_filename_datetime_entries`)

Returns `(datetime, includes_clock)` entries, merged and filtered:

- **`_filter_drop_date_only_when_clock_same_calendar_day`**: if both a midnight date-only and a clock time exist for the same calendar day, drop the date-only midnight so `min()` does not pick midnight.
- **`_dedupe_wall_second_prefer_microsecond`**: multiple patterns can yield the same wall-clock second; keep the parse with **larger `microsecond`** (e.g. Android ms vs generic `DATE_SEP` without sub-second).

**Pattern order matters for specificity** (earlier = more specific hooks first in the loop):

1. **`_RE_APPLE_IMG`** — `IMG_YYYYMMDD_HHMMSS`, etc.
2. **`_RE_SCREENSHOT_ANDROID`** — `Screenshot_YYYY-MM-DD-HH-MM-SS-mmm_…` (3-digit ms → `microsecond = ms * 1000`). Placed early so it is not “beaten” conceptually by looser hyphen patterns.
3. **`_RE_PXL`**, **`_RE_WA`**, **`_RE_DATETIME_COMPACT`**, **`_RE_DATE_SEP`**, **`_RE_DATE_SEP_AT_DOT_TIME`**, **`_RE_DATE_COMPACT8`**.

**`filename_has_anchor`**: boosts confidence for “known camera / screenshot” stems — includes Apple, PXL, WhatsApp pattern, and **`_RE_SCREENSHOT_ANDROID`**.

---

## 5. Confidence scores (dedupe)

`confidence_score(planned, filename)` maps the **primary** `Action` to an integer **score** (higher wins). Tie-break in the main loop: **lower `sequence`** (earlier processed file) wins.

Approximate scale (see source for exact strings):

| Situation | Score (typical) |
|-----------|-----------------|
| `exif_earlier_than_metadata` | 100 |
| `structured_path_align_created` | 90 |
| `filename_earlier_than_metadata` with clock + anchor | 94 |
| with clock, no anchor | 92 |
| date-only + anchor | 85 |
| date-only | 75 |
| `structured_path_oldest_signal` | 55 |
| `fallback_created_from_modified` | 40 |
| No time change / move only | 30 |

---

## 6. Duplicate handling (default)

- **Canonical destination**: `dest/<YEAR>/<basename>` — same logical file as OneDrive/NTFS **case-insensitive** name.
- **Slot key**: `slot_key_for(year, filename)` → `"<year>/<filename>"` with **Windows** normalization: **NFC + `casefold()`** on the basename so registry keys align with the filesystem.
- **State file** (default): `<dest>/organize_state.json` (unless `--state-file`). Contains `slots`, `skipped`, `sequence`, etc. **`STATE_VERSION = 1`**.
- **Winner/loser**: Higher score occupies the slot; equal score → **earlier sequence** wins. Loser: **no copy**, source unchanged.
- **Supersede**: If a newcomer **beats** an existing winner, the old file may be moved under **`_superseded/`** (non–dry-run).
- **Pre-existing dest file** not in state: seeded as **`MYSTERY_INCUMBENT_SCORE` (30)** so ties behave predictably.
- **Skip identical copy**: Fast path when incumbent exists on disk and bytes/size/times match — recorded as `skipped_identical_to_dest`.

### 6.1 State migration (Windows)

Older state JSON used **mixed-case** slot keys. After `casefold()` was added to `slot_key_for`, **`slots.get(sk)` missed** those entries → **duplicates were copied again** instead of skipped.

**Fix:** `migrate_state_slot_keys(state)` runs after load, rewrites keys with **`normalize_slot_key_for_dedupe`**, updates `winner.target_relative`, and merges rare duplicate keys that normalize to the same slot.

### 6.2 `duplicates.json`

After a **non–dry-run** with dedupe enabled, **`duplicates.json`** is written beside **`--state-file`** (same directory, `with_name("duplicates.json")`). Default: `<dest>/duplicates.json` next to `<dest>/organize_state.json`.

Contents are **only skips appended in that run** (slice from `skipped_before_run`), plus metadata (`generated_at`, `source`, `dest`, `state_file`, `count`, `duplicates`). Dry-run does **not** write state or this file.

### 6.3 User-visible paths on skip

Duplicate-skip summaries use **`dest/<year>/<original fname>`** for the `dest=` line so casing matches the source basename; internal **`sk`** in logs may still show the **canonical** lowercase form in the “slot …” phrase.

---

## 7. EXIF / PNG metadata notes

- **`_try_exif_datetime_original`**: Pillow EXIF; PNG `info` keys / XMP packet sniffing for “Date taken”.
- **ISO-8601 with `Z`**: trailing `Z` is converted to **`+00:00`** before `fromisoformat` so the value is **timezone-aware**; later code uses **`_naive_local`** where needed to compare to naive file times.
- **±1 day** gating: reduces bad overrides when EXIF is “a TZ off” from mtime.

---

## 8. CLI flags (non-exhaustive)

| Flag | Role |
|------|------|
| `--source`, `--dest` | Roots; default dest can equal source (see help). |
| `--dry-run` | No copies, no on-disk state (state stays in memory). |
| `--state-file` | Override state JSON path. |
| `--no-dedupe-scoring` | Legacy: **`_1`, `_2`** suffix collisions instead of score slots. |
| `--no-recurse` | Top-level files only under `--source`. |
| `--skip-path` | Repeatable; skip subtree (posix-ish, no `..`). |
| `--sort-files` | Sort all media paths before processing (deterministic order; memory + delay). |
| `--silence-skipped` | Quieter logs for identical-dest skips (still recorded in state/log). |
| `--min-year`, `--max-year` | Heuristic bounds. |
| `--preserve-source-mtime` | Keep dest mtime as source when not aligning to chosen datetime. |
| `--progress-every` | Progress log interval; `0` disables. |

---

## 9. Testing and expectations

- **`DRY_RUN_FILE_EXPECTATIONS`**: Large table driving assertions (rule substring, dest year, duplicate skip text, etc.).
- **Library tests** (same file): `parse_path_calendar`, `decide_actions` edge cases, **`slot_key_for`** / **`migrate_state_slot_keys`** on Windows, fake Pillow for **`Z`** EXIF, `normalize_skip_path_arg`, `iter_media_files`, CLI smoke.
- **Windows-only**: Unicode/case-equivalent basenames (`A\u0301.JPG` vs `á.jpg`) must collapse to one dedupe slot.

---

## 10. Operational tips

- **Large libraries**: Default streaming walk; `--sort-files` only if you need deterministic tie order and can afford memory/latency.
- **OneDrive / indexing**: State save uses atomic replace with retries (`save_json_atomic` / `save_state_atomic`) for Windows file locking.
- **Logs**: Default log under `logs/organize_photos_<timestamp>.log` at the repo root (see `src/io_ops.default_log_path`), or override with `--log`.

---

## 11. Layout notes

- **Entry**: repo-root **`organize_photos.py`** (delegates to **`src.cli`**) or **`python -m src.organize_photos`**.
- **Code**: **`src/`** package.
- **Tests**: **`src/__tests__/`** (`run_organize_photos_tests.py`, `README.md`).

---

## 12. Optional follow-ups (from prior reviews)

- Stronger test: assert **on-disk `organize_state.json` slot keys** are normalized after a run on Windows (not only behavior).
- Any **end-to-end** check for `duplicates.json` content when using a custom `--state-file` outside `--dest`.

---

*Last expanded to support handoff between chats; adjust dates and filenames when the codebase moves again.*
