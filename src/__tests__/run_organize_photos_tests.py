#!/usr/bin/env python3
"""
Build a small synthetic source tree, run ``src.cli.main`` in-process (dry-run by default),
and verify output against **hardcoded** per-file expectations (rules, timestamps, dest year,
source vs destination CreationTime semantics, touched / not copied).

Usage (from repo root):
  python src/__tests__/run_organize_photos_tests.py
  python src/__tests__/run_organize_photos_tests.py --live

--live copies to a temp dest and sets CreationTime on Windows (requires PowerShell).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_R = str(_REPO_ROOT)
if _R not in sys.path:
    sys.path.insert(0, _R)

from src.cli import main as cli_main


def _exit_code_from_system_exit(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _release_organize_photos_logging() -> None:
    """Close FileHandlers so temp --log paths can be deleted on Windows."""
    log = logging.getLogger("organize_photos")
    for h in list(log.handlers):
        try:
            h.close()
        except OSError:
            pass
    log.handlers.clear()


def _run_cli_main_captured(argv: list[str]) -> tuple[int, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                code = cli_main(argv)
        except SystemExit as e:
            code = _exit_code_from_system_exit(e)
        text = (out_buf.getvalue() or "") + "\n" + (err_buf.getvalue() or "")
        return code, text
    finally:
        _release_organize_photos_logging()


def write_dummy_photo(path: Path) -> None:
    """Minimal bytes; script only needs a file with a photo extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xd9")  # empty JPEG SOI/EOI


def touch_wrong_mtime(path: Path, when: datetime) -> None:
    """Force mtime/atime so structured-path slug vs file time mismatch is visible."""
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def build_source_tree(root: Path) -> None:
    """See README.md for what each path is meant to exercise."""
    write_dummy_photo(root / "filename_clock" / "IMG_20100615_143022.jpg")
    write_dummy_photo(root / "filename_clock" / "shot20100615143022.jpg")
    write_dummy_photo(root / "filename_date_only" / "pics_20130101_album.jpg")
    write_dummy_photo(root / "whatsapp" / "IMG-20180401-WA0001.jpg")
    write_dummy_photo(root / "pixel" / "PXL_20191225_120000451.jpg")
    write_dummy_photo(root / "sep_datetime" / "vacation_2020-06-01_14-30-45.jpg")
    write_dummy_photo(root / "compact_time" / "clip20110808193022.jpg")

    write_dummy_photo(root / "2015" / "08" / "summer.jpg")
    write_dummy_photo(root / "2014" / "solo.jpg")
    in_range = root / "2014" / "in_range.jpg"
    write_dummy_photo(in_range)
    # Folder implies 2014; force file mtime within 2014 so folder rule is suppressed.
    touch_wrong_mtime(in_range, datetime(2014, 6, 1, 10, 0, 0))
    write_dummy_photo(root / "1994" / "06-Aug-1994" / "scan.jpg")
    write_dummy_photo(root / "1994" / "15.06.1994" / "eu.jpg")
    write_dummy_photo(root / "1994-Aug" / "month_named.jpg")

    sp = root / "2020" / "01" / "15" / "20200115-120000" / "mismatch.jpg"
    write_dummy_photo(sp)
    touch_wrong_mtime(sp, datetime(2010, 6, 1, 8, 0, 0))

    write_dummy_photo(root / "z1" / "same.jpg")
    write_dummy_photo(root / "z2" / "same.jpg")
    write_dummy_photo(root / "no_hints" / "plain.jpg")


# ---------------------------------------------------------------------------
# Hardcoded expectations (aligned with src.organize_photos behavior)
#
# - `expected_rule_tags`: bracket kinds printed for that file, in order.
#   Use () for "no dating rule" (move-only / dry-run copy only).
# - `must_contain`: substrings that must appear in that file's verbose block.
# - `must_not_contain`: optional guardrails (e.g. move-only must not claim filename rule).
# - `source_created_before_note` / `dest_copy_created_after_note`: alignment on what the
#   script considers "before" (source ctime in verbose) vs "after" (copy CreationTime).
# - `dest_creation_time_touched`: True when `-> copy would get CreationTime` appears (PowerShell
#   would set the copy's CreationTime from heuristics).
# - `source_never_copied_to_dest`: True for duplicate losers (no dest file; source unchanged).
# - `dest_copy_creation_after_iso`: if not None, must match the verbose line (seconds precision).
#   None means no explicit line; copy would keep source CreationTime (touched=False).
# - `touch_outcome`: "touched" | "untouched_same_as_source" | "untouched_skipped_duplicate"
#   (must match verbose; see `_observed_touch_outcome`).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileExpectation:
    rel: str
    expected_rule_tags: tuple[str, ...]
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...] = ()
    source_created_before_note: str = ""
    dest_copy_created_after_note: str = ""
    dest_creation_time_touched: bool = False
    source_never_copied_to_dest: bool = False
    dest_copy_creation_after_iso: str | None = None
    # Short alignment flag: "touched" | "untouched_same_as_source" | "untouched_skipped_duplicate"
    touch_outcome: str = ""


# Order here is documentation only; validation uses `rel` to find the right output block.
DRY_RUN_FILE_EXPECTATIONS: tuple[FileExpectation, ...] = (
    FileExpectation(
        "2014/solo.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/solo.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Dated folder alone does not set CreationTime; move-only without other signals.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "2014/in_range.jpg",
        ("fallback_created_from_modified",),
        (
            "fallback_created_from_modified",
            "set_created=2014-06-01 10:00:00",
            "DRY-RUN would copy",
            "dest=by-year/2014/in_range.jpg",
        ),
        must_not_contain=(),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Folder implies 2014; mtime within 2014 drives fallback (folders do not override).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2014-06-01 10:00:00",
        touch_outcome="touched",
    ),
    FileExpectation(
        "2015/08/summer.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/summer.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="YYYY/MM/ folders are advisory only; no CreationTime change without other signals.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "1994/06-Aug-1994/scan.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/scan.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Dated folders do not set CreationTime; move-only without EXIF/filename/mtime rule.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "1994/15.06.1994/eu.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/eu.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Dated folders do not set CreationTime; move-only without other signals.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "1994-Aug/month_named.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/month_named.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Leading folder YYYY-Mmm is advisory only for dating rules.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "2020/01/15/20200115-120000/mismatch.jpg",
        ("structured_path_oldest_signal",),
        (
            "structured_path_oldest_signal",
            "set_created=2010-06-01 08:00:00",
            "DRY-RUN would copy",
            "dest=by-year/2010/mismatch.jpg",
        ),
        must_not_contain=(),
        source_created_before_note="created= ~ test run; modified= forced to 2010-06-01 (slug vs mtime mismatch).",
        dest_copy_created_after_note="Oldest of path slug, created, modified wins -> 2010-06-01 08:00:00 (mtime).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2010-06-01 08:00:00",
        touch_outcome="touched",
    ),
    FileExpectation(
        "compact_time/clip20110808193022.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2011-08-08 19:30:22",
            "DRY-RUN would copy",
            "2011",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2011-08-08 19:30:22 (filename beats metadata).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2011-08-08 19:30:22",
        touch_outcome="touched",
    ),
    FileExpectation(
        "filename_clock/IMG_20100615_143022.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2010-06-15 14:30:22",
            "DRY-RUN would copy",
            "2010",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2010-06-15 14:30:22 (IMG_YYYYMMDD_HHMMSS).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2010-06-15 14:30:22",
        touch_outcome="touched",
    ),
    FileExpectation(
        "filename_clock/shot20100615143022.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2010-06-15 14:30:22",
            "DRY-RUN would copy",
            "2010",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2010-06-15 14:30:22 (compact clock in stem).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2010-06-15 14:30:22",
        touch_outcome="touched",
    ),
    FileExpectation(
        "filename_date_only/pics_20130101_album.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2013-01-01 00:00:00",
            "DRY-RUN would copy",
            "2013",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2013-01-01 00:00:00 (date-only in filename).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2013-01-01 00:00:00",
        touch_outcome="touched",
    ),
    FileExpectation(
        "no_hints/plain.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/plain.jpg",
        ),
        must_not_contain=(
            "[filename_earlier_than_metadata]",
            "[structured_path_mismatch]",
            "[exif_earlier_than_metadata]",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="No new_created; copy keeps source CreationTime (move-only / score 30).",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "pixel/PXL_20191225_120000451.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2019-12-25 12:00:00",
            "DRY-RUN would copy",
            "2019",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2019-12-25 12:00:00 (PXL stem time).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2019-12-25 12:00:00",
        touch_outcome="touched",
    ),
    FileExpectation(
        "sep_datetime/vacation_2020-06-01_14-30-45.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2020-06-01 14:30:45",
            "DRY-RUN would copy",
            "2020",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2020-06-01 14:30:45 (sep date+time in stem).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2020-06-01 14:30:45",
        touch_outcome="touched",
    ),
    FileExpectation(
        "whatsapp/IMG-20180401-WA0001.jpg",
        ("filename_earlier_than_metadata",),
        (
            "set_created=2018-04-01 00:00:00",
            "DRY-RUN would copy",
            "2018",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Copy would get CreationTime 2018-04-01 00:00:00 (WhatsApp-style stem).",
        dest_creation_time_touched=True,
        dest_copy_creation_after_iso="2018-04-01 00:00:00",
        touch_outcome="touched",
    ),
    FileExpectation(
        "z1/same.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year/{current_year}/same.jpg",
        ),
        must_not_contain=("[filename_earlier_than_metadata]",),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="No new_created; copy keeps source CreationTime (dedupe incumbent wins later tie).",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
    FileExpectation(
        "z2/same.jpg",
        (),
        (
            "SKIP duplicate:",
            "{current_year}/same.jpg",
            "loses to incumbent",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="No destination file; duplicate slot already filled by z1/same.jpg.",
        dest_creation_time_touched=False,
        source_never_copied_to_dest=True,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_skipped_duplicate",
    ),
)

# Global footer (exact line suffix pattern — year in "Files processed" may vary if files change)
EXPECTED_PROCESSED_COUNT = 17
EXPECTED_ERRORS_ZERO = "Errors: 0"

# --source is the dated leaf ``1994-Aug`` (not the repo-wide tree root): path rules must
# still see ``1994-Aug`` via relative_to(source.parent).
NARROW_SOURCE_EXPECTATIONS: tuple[FileExpectation, ...] = (
    FileExpectation(
        "month_named.jpg",
        (),
        (
            "DRY-RUN would copy",
            "dest=by-year-narrow/{current_year}/month_named.jpg",
        ),
        source_created_before_note="Source created= is filesystem ctime (new temp file, ~ test run).",
        dest_copy_created_after_note="Dated folder is --source; folders are advisory only for dating rules.",
        dest_creation_time_touched=False,
        dest_copy_creation_after_iso=None,
        touch_outcome="untouched_same_as_source",
    ),
)
EXPECTED_NARROW_PROCESSED_COUNT = 1


def run_organizer(
    source: Path,
    dest: Path,
    dry_run: bool,
    state_file: Path | None,
    skip_paths: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, str]:
    if state_file is not None:
        log_path = state_file.parent / f"organizer_{state_file.stem}.log"
    else:
        log_path = source.parent / "organizer_default.log"
    argv: list[str] = [
        "--source",
        str(source),
        "--dest",
        str(dest),
        "--log",
        str(log_path),
        # Deterministic processing order (tie-breaks for duplicate slots) matches original suite.
        "--sort-files",
        "--progress-every",
        "0",
    ]
    if skip_paths:
        for sp in skip_paths:
            argv.extend(["--skip-path", sp])
    if extra_args:
        argv.extend(extra_args)
    if dry_run:
        argv.append("--dry-run")
    if state_file is not None:
        argv.extend(["--state-file", str(state_file)])
    # Do not pass -v: organizer now prints a stable 4-line-per-file summary by default.
    return _run_cli_main_captured(argv)


def _paragraph_for_rel(output: str, rel: str) -> str | None:
    """Return the paragraph (4-line block) for one source-relative path."""
    text = output.replace("\r\n", "\n")
    for para in text.split("\n\n"):
        lines = para.strip().split("\n")
        if not lines:
            continue
        first = lines[0].strip()
        if first == rel:
            return para
    return None


def _tags_in_block(block: str) -> list[str]:
    """Extract rule_kind from `rule=...` summary line."""
    m = re.search(r"\brule=([a-z_]+)\b", block)
    return [m.group(1)] if m else []


def _parse_source_created_line(block: str) -> datetime | None:
    m = re.search(r"\bcreated=([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\b", block)
    if not m:
        return None
    return datetime.fromisoformat(m.group(1))


def _parse_verbose_dest_copy_creation(block: str) -> datetime | None:
    """Datetime from `set_created=...` if present and not '-'."""
    m = re.search(r"\bset_created=([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\b", block)
    if not m:
        return None
    v = m.group(1).strip()
    if v == "-":
        return None
    return datetime.fromisoformat(v)


def _dt_to_seconds(d: datetime) -> datetime:
    return d.replace(microsecond=0)


def _check_creation_expectations(
    exp: FileExpectation,
    rel: str,
    block: str,
    errors: list[str],
) -> None:
    """Assert verbose CreationTime before/after semantics vs `dest_*` fields."""
    src_created = _parse_source_created_line(block)
    if src_created is None:
        errors.append(f"{rel}: could not parse source created= line")
        return

    dest_line = _parse_verbose_dest_copy_creation(block)
    has_ct_line = dest_line is not None

    if exp.dest_creation_time_touched != has_ct_line:
        errors.append(
            f"{rel}: expected dest_creation_time_touched={exp.dest_creation_time_touched}, "
            f"but verbose {'has' if has_ct_line else 'lacks'} "
            f"`-> copy would get CreationTime`"
        )

    if exp.source_never_copied_to_dest:
        if "DRY-RUN would copy" in block:
            errors.append(f"{rel}: duplicate loser must not print DRY-RUN would copy")
        if has_ct_line:
            errors.append(f"{rel}: duplicate loser must not set copy CreationTime line")

    elif exp.dest_copy_creation_after_iso is not None:
        want = datetime.fromisoformat(exp.dest_copy_creation_after_iso)
        if dest_line is None:
            errors.append(
                f"{rel}: expected copy CreationTime {exp.dest_copy_creation_after_iso!r}, got no line"
            )
        elif _dt_to_seconds(dest_line) != _dt_to_seconds(want):
            errors.append(
                f"{rel}: expected copy CreationTime {want}, got {dest_line}"
            )
    elif not exp.source_never_copied_to_dest:
        if has_ct_line:
            errors.append(
                f"{rel}: expected no explicit copy CreationTime (same as source), got {dest_line}"
            )

    if exp.touch_outcome:
        obs = _observed_touch_outcome(block)
        if exp.touch_outcome != obs:
            errors.append(
                f"{rel}: touch_outcome {exp.touch_outcome!r} does not match verbose "
                f"(observed {obs!r})"
            )


def _observed_touch_outcome(block: str) -> str:
    if "SKIP duplicate" in block:
        return "untouched_skipped_duplicate"
    if "set_created=" in block and "set_created=-" not in block:
        return "touched"
    return "untouched_same_as_source"


def check_expectations(output: str, errors: list[str]) -> None:
    if f"Done. Files processed: {EXPECTED_PROCESSED_COUNT}." not in output:
        errors.append(
            f"expected exactly {EXPECTED_PROCESSED_COUNT} files processed (check DRY_RUN_FILE_EXPECTATIONS length)"
        )
    if EXPECTED_ERRORS_ZERO not in output:
        errors.append(f"expected {EXPECTED_ERRORS_ZERO!r} in output")

    seen_rels: set[str] = set()
    for exp in DRY_RUN_FILE_EXPECTATIONS:
        rel = exp.rel
        seen_rels.add(rel)
        block = _paragraph_for_rel(output, rel)
        if block is None:
            errors.append(f"no verbose paragraph found for source file {rel!r}")
            continue

        cy = datetime.now().year
        for sub in exp.must_contain:
            need = sub.format(current_year=cy)
            if need not in block:
                errors.append(f"{rel}: missing required substring {need!r}\n--- block ---\n{block}\n---")

        for sub in exp.must_not_contain:
            if sub in block:
                errors.append(f"{rel}: must not contain {sub!r}")

        _check_creation_expectations(exp, rel, block, errors)

        tags = _tags_in_block(block)
        # `[dry-run]` is not captured as a single tag (hyphen). Match known dating rule kinds only.
        rule_set = {
            "filename_earlier_than_metadata",
            "structured_path_mismatch",
            "structured_path_oldest_signal",
            "structured_path_align_created",
            "exif_earlier_than_metadata",
            "fallback_created_from_modified",
        }
        found_rules = [t for t in tags if t in rule_set]

        if exp.expected_rule_tags == ():
            if found_rules:
                errors.append(
                    f"{rel}: expected no dating rule tags, got {found_rules} (full tags {tags})"
                )
        else:
            want = list(exp.expected_rule_tags)
            if found_rules != want:
                errors.append(
                    f"{rel}: expected rule tag(s) {want}, got {found_rules} (all bracket tags: {tags})"
                )

    # Ensure every expectation has a unique rel
    if len(seen_rels) != len(DRY_RUN_FILE_EXPECTATIONS):
        errors.append("duplicate rel in DRY_RUN_FILE_EXPECTATIONS")


def check_narrow_source_expectations(output: str, errors: list[str]) -> None:
    if f"Done. Files processed: {EXPECTED_NARROW_PROCESSED_COUNT}." not in output:
        errors.append(
            f"expected exactly {EXPECTED_NARROW_PROCESSED_COUNT} files in narrow-source dry-run"
        )
    if EXPECTED_ERRORS_ZERO not in output:
        errors.append(f"narrow-source run: expected {EXPECTED_ERRORS_ZERO!r} in output")

    for exp in NARROW_SOURCE_EXPECTATIONS:
        rel = exp.rel
        block = _paragraph_for_rel(output, rel)
        if block is None:
            errors.append(f"narrow source: no verbose paragraph for {rel!r}")
            continue
        cy = datetime.now().year
        for sub in exp.must_contain:
            need = sub.format(current_year=cy)
            if need not in block:
                errors.append(
                    f"narrow {rel}: missing required substring {need!r}\n--- block ---\n{block}\n---"
                )
        for sub in exp.must_not_contain:
            if sub in block:
                errors.append(f"narrow {rel}: must not contain {sub!r}")
        _check_creation_expectations(exp, rel, block, errors)
        tags = _tags_in_block(block)
        rule_set = {
            "filename_earlier_than_metadata",
            "structured_path_mismatch",
            "structured_path_oldest_signal",
            "structured_path_align_created",
            "exif_earlier_than_metadata",
            "fallback_created_from_modified",
        }
        found_rules = [t for t in tags if t in rule_set]
        want = list(exp.expected_rule_tags)
        if found_rules != want:
            errors.append(
                f"narrow {rel}: expected rule tag(s) {want}, got {found_rules} "
                f"(all bracket tags: {tags})"
            )


def run_skip_path_cli_smoke_test(errors: list[str]) -> None:
    """Two JPEGs under --source; --skip-path removes the subtree from the scan."""
    with tempfile.TemporaryDirectory(prefix="organize_skip_path_") as tmp:
        root = Path(tmp) / "src"
        dest = Path(tmp) / "out"
        state_a = Path(tmp) / "state_a.json"
        state_b = Path(tmp) / "state_b.json"
        write_dummy_photo(root / "keep" / "a.jpg")
        write_dummy_photo(root / "ignored_tree" / "deep" / "b.jpg")
        code_all, out_all = run_organizer(root, dest, True, state_a, skip_paths=None)
        code_sk, out_sk = run_organizer(
            root, dest, True, state_b, skip_paths=["ignored_tree"]
        )
        if code_all != 0:
            errors.append(f"skip-path smoke (no skip) exit {code_all}")
        if code_sk != 0:
            errors.append(f"skip-path smoke (with skip) exit {code_sk}")
        if "Done. Files processed: 2." not in out_all:
            errors.append(
                "skip-path smoke: expected 2 files without --skip-path\n" + out_all[:800]
            )
        if "Done. Files processed: 1." not in out_sk:
            errors.append(
                "skip-path smoke: expected 1 file with --skip-path ignored_tree\n" + out_sk[:800]
            )
        sk_norm = out_sk.replace("\\", "/")
        if "ignored_tree/deep/b.jpg" in sk_norm:
            errors.append(
                "skip-path smoke: skipped file should not appear in organizer output"
            )


def run_organize_photos_import_library_tests(errors: list[str]) -> None:
    """Import ``src.organize_photos`` and exercise parsers, rules, scoring, I/O helpers, iterators."""
    sd = str(_REPO_ROOT)
    pushed = False
    if sd not in sys.path:
        sys.path.insert(0, sd)
        pushed = True
    try:
        import argparse as argparse_mod
        import types

        import src.organize_photos as op

        now = datetime(2026, 4, 13, 12, 0, 0)
        cfg = op.HeuristicConfig()

        # --- GPS → IANA timezone (timezonefinder; skip if unavailable) ---
        from src.datetime_policy import (
            infer_timezone_name_from_gps as _gps_tz,
            naive_exif_with_gps_local_timezone,
            parse_exif_offset_string,
        )
        from src.extractors import _gps_ifd_to_lat_lon
        from src.models import ExifCaptureInfo

        munich = _gps_tz(48.137154, 11.576124)
        if munich is not None and munich != "Europe/Berlin":
            errors.append(
                f"timezonefinder Munich: expected Europe/Berlin, got {munich!r}"
            )

        if _gps_tz(200.0, 0.0) is not None:
            errors.append("infer_timezone_name_from_gps: invalid lat should return None")
        ny_tz = _gps_tz(40.7128, -74.0060)
        if ny_tz is not None and ny_tz != "America/New_York":
            errors.append(f"timezonefinder NYC: expected America/New_York, got {ny_tz!r}")

        # EXIF GPS IFD → lat/lon (synthetic dict, same shape as Pillow)
        berlin_gps = {
            1: b"N",
            2: ((52, 1), (31, 1), (0, 1)),
            3: b"E",
            4: ((13, 1), (24, 1), (0, 1)),
        }
        ll = _gps_ifd_to_lat_lon(berlin_gps)
        if ll is None:
            errors.append("_gps_ifd_to_lat_lon: expected Berlin-ish coordinates")
        else:
            blat, blon = ll
            if abs(blat - (52 + 31 / 60.0)) > 0.001 or abs(blon - (13 + 24 / 60.0)) > 0.001:
                errors.append(f"_gps_ifd_to_lat_lon DMS decode wrong: {ll!r}")

        south_west = {
            1: b"S",
            2: ((10, 1), (0, 1), (0, 1)),
            3: b"W",
            4: ((20, 1), (0, 1), (0, 1)),
        }
        ll_sw = _gps_ifd_to_lat_lon(south_west)
        if ll_sw is None or ll_sw[0] >= 0 or ll_sw[1] >= 0:
            errors.append(f"_gps_ifd_to_lat_lon S/W refs: expected negative lat/lon, got {ll_sw!r}")

        # naive_exif + GPS → aware + zone name (needs zoneinfo or backports.zoneinfo on Py<3.9)
        _zi_available = True
        try:
            from zoneinfo import ZoneInfo  # noqa: F401
        except ImportError:
            try:
                from backports.zoneinfo import ZoneInfo  # noqa: F401
            except ImportError:
                _zi_available = False
        if munich is not None and _zi_available:
            naive_t = datetime(2022, 6, 15, 14, 30, 0)
            aw, zn, sk = naive_exif_with_gps_local_timezone(naive_t, 48.137154, 11.576124)
            if sk is not None:
                errors.append(
                    f"naive_exif_with_gps_local_timezone: unexpected skip reason {sk!r}"
                )
            if zn != "Europe/Berlin":
                errors.append(
                    f"naive_exif_with_gps_local_timezone: expected Europe/Berlin, got {zn!r}"
                )
            if aw.tzinfo is None:
                errors.append("naive_exif_with_gps_local_timezone: expected timezone-aware datetime")

        def _fixed_offset_hours(tz, dt=None) -> float | None:
            if tz is None:
                return None
            u = tz.utcoffset(dt or datetime(2020, 6, 15, 12, 0, 0))
            if u is None:
                return None
            return u.total_seconds() / 3600.0

        for label, s, want_h, want_m in (
            ("+05:30", "+05:30", 5, 30),
            ("+0530 compact", "+0530", 5, 30),
            ("+12:45", "+12:45", 12, 45),
            ("Z", "Z", 0, 0),
            ("UTC", "UTC", 0, 0),
            ("-04:30", "-04:30", -4, -30),
        ):
            tz = parse_exif_offset_string(s)
            if tz is None:
                errors.append(f"parse_exif_offset_string {label}: got None")
                continue
            h = _fixed_offset_hours(tz)
            ex = want_h + want_m / 60.0
            if h is None or abs(h - ex) > 1e-6:
                errors.append(
                    f"parse_exif_offset_string {label}: want offset hours {ex}, got {h!r}"
                )
        if parse_exif_offset_string("+99:00") is not None:
            errors.append("parse_exif_offset_string should reject implausible +99:00")
        if parse_exif_offset_string("+08:99") is not None:
            errors.append("parse_exif_offset_string should reject invalid minutes")

        from datetime import timezone as _tz

        from src.datetime_policy import filesystem_instant_for_rule as _fs_inst

        exif_plus8 = datetime(
            2022, 10, 25, 14, 41, 38, tzinfo=_tz(timedelta(hours=8))
        )
        fn_stem = datetime(2022, 10, 25, 14, 41, 35)
        fs_out = _fs_inst(
            fn_stem, "filename_earlier_than_metadata", exif_plus8, "Asia/Singapore"
        )
        want_ts = datetime(
            2022, 10, 25, 14, 41, 35, tzinfo=_tz(timedelta(hours=8))
        ).timestamp()
        if fs_out.tzinfo is None or abs(fs_out.timestamp() - want_ts) > 1.0:
            errors.append(
                f"filesystem_instant_for_rule: expected +08 instant, got {fs_out!r}"
            )
        fs_plain = _fs_inst(
            datetime(2010, 6, 1, 8, 0, 0),
            "structured_path_oldest_signal",
            exif_plus8,
            None,
        )
        if fs_plain.tzinfo is not None:
            errors.append("filesystem_instant_for_rule must not attach TZ for non filename/exif rules")

        # ExifCaptureInfo hint_string: tz~GPS replaces redundant GPS=yes
        cap = ExifCaptureInfo(
            datetime(2020, 1, 1, 12, 0, 0),
            has_gps=True,
            gps_timezone_name="Europe/Berlin",
        )
        hs = cap.hint_string() or ""
        if "tz~GPS=Europe/Berlin" not in hs:
            errors.append(f"ExifCaptureInfo hint missing tz~GPS: {hs!r}")
        if "GPS=yes" in hs:
            errors.append("ExifCaptureInfo hint should not add GPS=yes when tz~GPS is set")
        cap2 = ExifCaptureInfo(datetime(2020, 1, 1, 12, 0, 0), has_gps=True)
        hs2 = cap2.hint_string() or ""
        if "GPS=yes" not in hs2:
            errors.append(f"ExifCaptureInfo has_gps only: expected GPS=yes in {hs2!r}")
        cap_skip = ExifCaptureInfo(
            datetime(2020, 1, 1, 12, 0, 0),
            has_gps=True,
            gps_tz_skip="no timezonefinder",
        )
        hs_skip = cap_skip.hint_string() or ""
        if "GPS~no_tz=no timezonefinder" not in hs_skip:
            errors.append(f"ExifCaptureInfo gps_tz_skip hint: {hs_skip!r}")
        if "GPS=yes" in hs_skip:
            errors.append("ExifCaptureInfo should prefer GPS~no_tz over GPS=yes")

        # --- EXIF vs mtime 1-day tolerance ---
        exif = datetime(2010, 6, 14, 8, 0, 0)
        modified = datetime(2010, 6, 15, 6, 0, 0)
        acts = op.decide_actions([], "x.jpg", modified, modified, exif, now, cfg)
        if any(a.kind == "exif_earlier_than_metadata" for a in acts):
            errors.append(
                "exif vs mtime within 1 day should not trigger exif_earlier_than_metadata"
            )

        exif_far = datetime(2008, 1, 1, 12, 0, 0)
        modified_far = datetime(2010, 6, 15, 6, 0, 0)
        acts_far = op.decide_actions(
            [], "x.jpg", modified_far, modified_far, exif_far, now, cfg
        )
        if not any(a.kind == "exif_earlier_than_metadata" for a in acts_far):
            errors.append(
                "EXIF well before mtime (>1 day) should still trigger exif_earlier_than_metadata"
            )

        exif_24h = datetime(2010, 6, 14, 12, 0, 0)
        mod_24h = datetime(2010, 6, 15, 12, 0, 0)
        if (mod_24h - exif_24h) != timedelta(days=1):
            errors.append("unit test bug: expected exactly 24h between exif_24h and mod_24h")
        acts_24h = op.decide_actions([], "x.jpg", mod_24h, mod_24h, exif_24h, now, cfg)
        if any(a.kind == "exif_earlier_than_metadata" for a in acts_24h):
            errors.append(
                "EXIF exactly 1 day before mtime should not trigger exif_earlier_than_metadata"
            )

        exif_over = datetime(2010, 6, 14, 11, 59, 59)
        mod_over = datetime(2010, 6, 15, 12, 0, 0)
        if (mod_over - exif_over) <= timedelta(days=1):
            errors.append("unit test bug: expected >24h between exif_over and mod_over")
        acts_over = op.decide_actions([], "x.jpg", mod_over, mod_over, exif_over, now, cfg)
        if not any(a.kind == "exif_earlier_than_metadata" for a in acts_over):
            errors.append(
                "EXIF >24h before mtime should still trigger exif_earlier_than_metadata"
            )

        # EXIF vs filename clock within 1 minute (camera DCIM): prefer filename, not exif-alone.
        fs_dcim = datetime(2023, 11, 19, 4, 7, 0)
        exif_dcim = datetime(2022, 11, 19, 10, 7, 3)
        acts_dcim = op.decide_actions(
            [],
            "IMG_20221119_100659.jpg",
            fs_dcim,
            fs_dcim,
            exif_dcim,
            now,
            cfg,
        )
        if any(a.kind == "exif_earlier_than_metadata" for a in acts_dcim):
            errors.append(
                "EXIF within 1min of filename clock should not use exif_earlier_than_metadata alone"
            )
        pk_dcim = op.primary_time_action(acts_dcim)
        if pk_dcim is None or pk_dcim.kind != "filename_earlier_than_metadata":
            errors.append(
                "DCIM skew: expected filename_earlier_than_metadata, got "
                f"{[a.kind for a in acts_dcim]}"
            )
        elif pk_dcim.new_created != datetime(2022, 11, 19, 10, 6, 59):
            errors.append(
                f"DCIM skew: expected filename 10:06:59, got {pk_dcim.new_created}"
            )

        # Same stem; EXIF >1 minute from filename clock → keep exif_earlier_than_metadata.
        exif_wide_skew = datetime(2022, 11, 19, 10, 9, 0)
        acts_exif_beats_fn = op.decide_actions(
            [],
            "IMG_20221119_100659.jpg",
            fs_dcim,
            fs_dcim,
            exif_wide_skew,
            now,
            cfg,
        )
        pk_wide = op.primary_time_action(acts_exif_beats_fn)
        if pk_wide is None or pk_wide.kind != "exif_earlier_than_metadata":
            errors.append(
                "EXIF >1min from filename clock: expected exif_earlier_than_metadata, got "
                f"{[a.kind for a in acts_exif_beats_fn]}"
            )
        elif pk_wide.new_created != exif_wide_skew:
            errors.append(
                f"EXIF >1min skew: expected EXIF time {exif_wide_skew}, got {pk_wide.new_created}"
            )

        # Aware EXIF (+08): stem correlation must use civil clock, not host OS local.
        exif_plus8 = datetime(
            2022, 11, 19, 10, 7, 3, tzinfo=timezone(timedelta(hours=8))
        )
        acts_plus8 = op.decide_actions(
            [],
            "IMG_20221119_100659.jpg",
            fs_dcim,
            fs_dcim,
            exif_plus8,
            now,
            cfg,
        )
        if any(a.kind == "exif_earlier_than_metadata" for a in acts_plus8):
            errors.append(
                "aware EXIF +08 vs stem: expected filename precedence, not exif_earlier_than_metadata"
            )
        pk_p8 = op.primary_time_action(acts_plus8)
        if pk_p8 is None or pk_p8.kind != "filename_earlier_than_metadata":
            errors.append(
                f"aware +08 stem compare: expected filename rule, got {[a.kind for a in acts_plus8]}"
            )
        elif pk_p8.new_created != datetime(2022, 11, 19, 10, 6, 59):
            errors.append(
                f"aware +08: expected filename 10:06:59, got {pk_p8.new_created}"
            )

        # --- Filename timestamp vs mtime 1-day tolerance (timezone naive) ---
        # If filename includes a clock but is within ±1 day of mtime, prefer mtime (do not override).
        created = datetime(2020, 1, 2, 12, 0, 0)
        modified = datetime(2020, 1, 1, 12, 0, 0)
        acts_fn_amb = op.decide_actions(
            [],
            "IMG_20200101_090000.jpg",
            created,
            modified,
            None,
            now,
            cfg,
        )
        if any(a.kind == "filename_earlier_than_metadata" for a in acts_fn_amb):
            errors.append(
                "filename vs mtime within 1 day should not trigger filename_earlier_than_metadata"
            )
        if not any(a.kind == "fallback_created_from_modified" for a in acts_fn_amb):
            errors.append(
                "filename ambiguous: expected fallback_created_from_modified to run"
            )

        # If filename clock is >1 day earlier than mtime, filename rule should still apply.
        acts_fn_ok = op.decide_actions(
            [],
            "IMG_20200101_090000.jpg",
            created,
            datetime(2020, 1, 5, 12, 0, 0),
            None,
            now,
            cfg,
        )
        if not any(a.kind == "filename_earlier_than_metadata" for a in acts_fn_ok):
            errors.append(
                "filename >1 day from mtime should still trigger filename_earlier_than_metadata"
            )

        # Real-world combined case: year folder + filename clock + EXIF present but ambiguous vs mtime.
        # Expect fallback to mtime (not EXIF, not filename), and folder rule suppressed since mtime is within the year.
        rel_parts_year = ["2019"]
        created = datetime(2026, 1, 1, 0, 0, 0)
        # Make mtime slightly later than filename time so filename qualifies as earlier-than-earliest,
        # but still within the ±1 day ambiguity window.
        modified = datetime(2019, 12, 9, 8, 0, 0)
        # EXIF is within ±1 day of mtime but not equal => ambiguous
        exif_amb = datetime(2019, 12, 9, 6, 55, 0)
        acts_combo = op.decide_actions(
            rel_parts_year,
            "IMG_20191209_074218.jpg",
            created,
            modified,
            exif_amb,
            now,
            cfg,
        )
        kinds_combo = [a.kind for a in acts_combo]
        if "exif_earlier_than_metadata" in kinds_combo:
            errors.append("combo case: EXIF ambiguous vs mtime must not be used")
        if "filename_earlier_than_metadata" in kinds_combo:
            errors.append("combo case: filename within ±1 day of mtime must not be used")
        if not any(a.kind == "fallback_created_from_modified" for a in acts_combo):
            errors.append("combo case: expected fallback_created_from_modified")

        # Real-world failure case from analysis: year folder later than filename timestamp.
        # Folder must not override; filename should be used.
        acts_shot = op.decide_actions(
            ["2019"],
            "Screen Shot 2015-08-23 at 3.17.22 PM.png",
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2024, 6, 16, 11, 21, 48),
            None,
            now,
            cfg,
        )
        kinds_shot = [a.kind for a in acts_shot]
        if "path_hierarchy_trusted" in kinds_shot:
            errors.append("screenshot case: path_hierarchy_trusted must not be emitted")
        if not any(a.kind == "filename_earlier_than_metadata" for a in acts_shot):
            errors.append("screenshot case: expected filename_earlier_than_metadata")

        # Real-world camera case: mtime and filename match, but EXIF is a few hours off (TZ).
        # Prefer mtime (align created->modified) instead of leaving created at copy time.
        acts_tz = op.decide_actions(
            ["2020"],
            "IMG_20200511_111118.jpg",
            datetime(2026, 4, 14, 13, 12, 6),
            datetime(2020, 5, 11, 6, 11, 20),
            datetime(2020, 5, 11, 11, 11, 18),
            now,
            cfg,
        )
        if not any(a.kind == "fallback_created_from_modified" for a in acts_tz):
            errors.append("tz camera case: expected fallback_created_from_modified to prefer mtime")

        # --- parse_path_calendar ---
        y, m, d = op.parse_path_calendar(["2014"])
        if (y, m, d) != (2014, None, None):
            errors.append(f"parse_path_calendar YYYY: got {(y,m,d)}")
        y, m, d = op.parse_path_calendar(["1994-Aug"])
        if y != 1994 or m != 8 or d is not None:
            errors.append(f"parse_path_calendar YYYY-Mmm: got {(y,m,d)}")
        y, m, d = op.parse_path_calendar(["nope"])
        if y is not None:
            errors.append("parse_path_calendar should reject junk first segment")
        # Invalid YYYY/MM/DD should not count as usable path hint.
        if op.path_has_calendar_hint(["2024", "02", "31"]):
            errors.append("invalid calendar path 2024/02/31 must not be a usable hint")
        acts_bad_day = op.decide_actions(
            ["2024", "02", "31"],
            "plain.jpg",
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2020, 1, 1, 12, 0, 0),
            None,
            now,
            cfg,
        )
        if not any(a.kind == "fallback_created_from_modified" for a in acts_bad_day):
            errors.append("invalid calendar path should allow fallback_created_from_modified")

        # --- Real-world folder patterns (F:\\Marias_Photos) ---
        # Keep this list to distinct patterns (no private filenames).
        real_world_leading_dirs = [
            ["2017"],
            ["2018"],
            ["2019"],
            ["2020"],
            ["2021"],
        ]
        for parts in real_world_leading_dirs:
            y, _m, _d = op.parse_path_calendar(parts)
            if y is None:
                errors.append(f"real-world pattern should yield year: {parts}")

        # --- Real-world filename patterns (F:\\Marias_Photos) ---
        # These are synthetic representatives of patterns observed in that library scan.
        # Each tuple is (filename, expected_dt_iso, expects_clock).
        real_world_filenames = [
            ("IMG_20191128_195142.jpg", "2019-11-28 19:51:42", True),
            ("20190103_124901.jpg", "2019-01-03 12:49:01", True),
            ("20181203_171348.mp4", "2018-12-03 17:13:48", True),
            ("VID_20200101_001221.mp4", "2020-01-01 00:12:21", True),
            ("Screenshot_20181008-181058.png", "2018-10-08 18:10:58", True),
            (
                "Screenshot_2024-01-28-07-04-48-525_com.overdrive.mobile.android.libby.jpg",
                "2024-01-28 07:04:48.525000",
                True,
            ),
            ("Screen Shot 2015-08-23 at 3.17.22 PM.png", "2015-08-23 15:17:22", True),
            ("4-up on 2011-03-16 at 17.32 #10.jpg", "2011-03-16 17:32:00", True),
            ("Movie on 2011-08-10 at 09.32.mov", "2011-08-10 09:32:00", True),
        ]
        for fn, want_iso, want_clock in real_world_filenames:
            entries = op.extract_filename_datetime_entries(fn, now, cfg)
            if not entries:
                errors.append(f"real-world filename should parse a date: {fn!r}")
                continue
            got_dt, got_clock = entries[0]
            if "." in want_iso:
                got_cmp = got_dt.isoformat(sep=" ")
            else:
                got_cmp = got_dt.isoformat(sep=" ", timespec="seconds")
            if got_cmp != want_iso:
                errors.append(
                    f"real-world filename {fn!r}: expected {want_iso}, got {got_dt} (all {entries})"
                )
            if bool(got_clock) != bool(want_clock):
                errors.append(
                    f"real-world filename {fn!r}: expected includes_clock={want_clock}, got {got_clock}"
                )

        # --- rel_parts_for_path_dating ---
        with tempfile.TemporaryDirectory(prefix="op_rel_") as tr:
            troot = Path(tr)
            leaf = troot / "2001-Jan"
            leaf.mkdir()
            f = leaf / "p.jpg"
            f.write_bytes(b"\xff\xd8\xff\xd9")
            rp = op.rel_parts_for_path_dating(f.resolve(), leaf.resolve())
            if rp != ["2001-Jan"]:
                errors.append(f"rel_parts_for_path_dating leaf source: {rp!r}")

        # --- decide_actions: Apple structured path align created ---
        apple_parts = ["2020", "01", "15", "20200115-120000"]
        slug_dt = datetime(2020, 1, 15, 12, 0, 0)
        mod_a = slug_dt
        cre_a = slug_dt + timedelta(hours=1)
        acts_ap = op.decide_actions(
            apple_parts, "x.jpg", cre_a, mod_a, None, now, cfg
        )
        kinds_ap = [a.kind for a in acts_ap]
        if kinds_ap != ["structured_path_align_created"]:
            errors.append(f"expected structured_path_align_created, got {kinds_ap}")

        # --- decide_actions: fallback_created_from_modified ---
        acts_fb = op.decide_actions(
            [],
            "plain.bin",
            datetime(2020, 1, 2),
            datetime(2010, 1, 1),
            None,
            now,
            cfg,
        )
        if not any(a.kind == "fallback_created_from_modified" for a in acts_fb):
            errors.append("expected fallback_created_from_modified")

        # --- decide_actions: EXIF equals mtime but created newer -> prefer mtime ---
        acts_exif_eq_mtime = op.decide_actions(
            ["2022"],
            "IMG_20200101_001102.jpg",
            datetime(2022, 1, 30, 16, 17, 51),
            datetime(2020, 1, 1, 0, 11, 2),
            datetime(2020, 1, 1, 0, 11, 2),
            now,
            cfg,
        )
        if not any(a.kind == "fallback_created_from_modified" for a in acts_exif_eq_mtime):
            errors.append("exif==mtime: expected fallback_created_from_modified to prefer mtime")

        # --- confidence_score / primary_time_action / filename_has_anchor ---
        sc, rk = op.confidence_score([], "a.jpg")
        if sc != 30 or rk != "move_only_no_time_change":
            errors.append(f"confidence move-only: {(sc,rk)}")
        exif_leg = [
            op.Action(
                "exif_earlier_than_metadata",
                "d",
                new_created=datetime(2010, 1, 1),
            )
        ]
        if op.confidence_score(exif_leg, "a.jpg")[0] != 100:
            errors.append("confidence exif_earlier_than_metadata")
        if not op.filename_has_anchor("IMG_20100615_143022.jpg"):
            errors.append("filename_has_anchor IMG_")
        if op.filename_has_anchor("plain.jpg"):
            errors.append("filename_has_anchor false positive")

        # --- is_prior_organizer_output ---
        with tempfile.TemporaryDirectory(prefix="op_prior_") as tp:
            dr = Path(tp).resolve()
            yf = dr / "2015" / "z.jpg"
            yf.parent.mkdir(parents=True)
            yf.write_bytes(b"x")
            if not op.is_prior_organizer_output(yf, dr, min_year=1990, max_year=2100):
                errors.append("is_prior_organizer_output flat YEAR/file")
            old = dr / "1899" / "a.jpg"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"x")
            if op.is_prior_organizer_output(old, dr, min_year=1990, max_year=2100):
                errors.append("is_prior_organizer_output should ignore year out of range")
            sup = dr / op.SUPERSEDED_SUBDIR / "b.jpg"
            sup.parent.mkdir(parents=True)
            sup.write_bytes(b"x")
            if not op.is_prior_organizer_output(sup, dr, min_year=1990, max_year=2100):
                errors.append("is_prior_organizer_output _superseded")

        # --- dest_path_from_slot_relative ---
        dest_root = Path("D:/fake_dest")
        got = op.dest_path_from_slot_relative(dest_root, r"2020\foo.jpg")
        if got != dest_root / "2020" / "foo.jpg":
            errors.append(f"dest_path_from_slot_relative: {got}")

        # --- slot_key_for Windows normalization (Unicode + case-insensitive) ---
        if sys.platform == "win32":
            k1 = op.slot_key_for(2024, "A\u0301.JPG")  # A + combining accent
            k2 = op.slot_key_for(2024, "\u00e1.jpg")  # precomposed á
            if k1 != k2:
                errors.append(
                    "slot_key_for should normalize Unicode/case on Windows (A\u0301.JPG vs á.jpg)"
                )
            if op.normalize_slot_key_for_dedupe("2020/Photo.JPG") != "2020/photo.jpg":
                errors.append("normalize_slot_key_for_dedupe should casefold basename on Windows")
            st_m = {
                "slots": {
                    "2020/Photo.JPG": {
                        "winner": {
                            "score": 40,
                            "sequence": 1,
                            "target_relative": "2020/Photo.JPG",
                        },
                        "history": [],
                    }
                }
            }
            op.migrate_state_slot_keys(st_m)
            if "2020/photo.jpg" not in st_m["slots"]:
                errors.append("migrate_state_slot_keys should rewrite legacy mixed-case slot keys")
            elif st_m["slots"]["2020/photo.jpg"]["winner"].get("target_relative") != "2020/photo.jpg":
                errors.append("migrate_state_slot_keys should set winner target_relative to canonical key")

        # --- _try_exif_datetime_original should preserve UTC 'Z' timezone ---
        saved_modules = {
            "PIL": sys.modules.get("PIL"),
            "PIL.ExifTags": sys.modules.get("PIL.ExifTags"),
        }
        fake_pil = types.ModuleType("PIL")
        fake_exif_tags = types.ModuleType("PIL.ExifTags")
        fake_exif_tags.TAGS = {}

        class _FakeImg:
            def __init__(self) -> None:
                self.info = {"Creation Time": "2024-06-21T04:40:06Z"}

            def __enter__(self) -> "_FakeImg":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def getexif(self) -> dict[str, str]:
                return {}

        class _FakeImageModule:
            @staticmethod
            def open(_path: Path) -> _FakeImg:
                return _FakeImg()

            class Exif:  # pragma: no cover - shape-only for compatibility
                def load(self, _raw: bytes) -> None:
                    return None

        fake_pil.Image = _FakeImageModule
        sys.modules["PIL"] = fake_pil
        sys.modules["PIL.ExifTags"] = fake_exif_tags
        try:
            z_dt = op._try_exif_datetime_original(Path("fake.jpg"))
            if z_dt is None:
                errors.append("_try_exif_datetime_original should parse ISO-8601 Z values")
            elif z_dt.tzinfo is None or z_dt.utcoffset() != timedelta(0):
                errors.append(
                    "_try_exif_datetime_original should preserve UTC tzinfo for trailing Z"
                )
        finally:
            for mod_name, mod in saved_modules.items():
                if mod is None:
                    sys.modules.pop(mod_name, None)
                else:
                    sys.modules[mod_name] = mod

        # --- normalize_skip_path_arg ---
        if op.normalize_skip_path_arg("a/b\\c") != "a/b/c":
            errors.append("normalize_skip_path_arg slash mix")
        try:
            op.normalize_skip_path_arg("a/../b")
            errors.append("normalize_skip_path_arg should reject ..")
        except argparse_mod.ArgumentTypeError:
            pass

        # --- iter_media_files ---
        with tempfile.TemporaryDirectory(prefix="op_iter_") as ti:
            ir = Path(ti)
            write_dummy_photo(ir / "top.jpg")
            write_dummy_photo(ir / "deep" / "in.jpg")
            (ir / "nope.txt").write_text("x", encoding="utf-8")
            (ir / "vid.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
            n_all = len(list(op.iter_media_files(ir)))
            if n_all != 3:
                errors.append(f"iter_media_files expected 3 media files, got {n_all}")
            n_top = len(list(op.iter_media_files(ir, recurse=False)))
            if n_top != 2:
                errors.append(f"iter_media_files no-recurse expected 2, got {n_top}")
            n_skip = len(
                list(op.iter_media_files(ir, skip_path_prefixes=("deep",)))
            )
            if n_skip != 2:
                errors.append(f"iter_media_files skip deep expected 2, got {n_skip}")

        # --- state JSON ---
        with tempfile.TemporaryDirectory(prefix="op_state_") as ts:
            sp = Path(ts) / "state.json"
            st = op.default_state(Path("/s"), Path("/d"))
            if "slots" not in st or st.get("version") != op.STATE_VERSION:
                errors.append("default_state shape")
            op.save_state_atomic(sp, st)
            loaded = op.load_state_file(sp)
            if not loaded or loaded.get("version") != op.STATE_VERSION:
                errors.append("save/load state roundtrip")
            sp.write_text("{not json", encoding="utf-8")
            if op.load_state_file(sp) is not None:
                errors.append("load_state_file invalid JSON should be None")

        # --- match_structured_path negative ---
        if op.match_structured_path(["2020", "01"]) is not None:
            errors.append("match_structured_path too short should be None")

        # --- unique_dest + reserved (dry-run legacy collision) ---
        with tempfile.TemporaryDirectory(prefix="op_ud_") as tud:
            ydir = Path(tud) / "2026"
            ydir.mkdir()
            base = ydir / "z.jpg"
            taken: set[Path] = set()
            u0 = op.unique_dest(base, reserved=taken)
            if u0 != base:
                errors.append("unique_dest first slot should be base name")
            taken.add(u0.resolve())
            u1 = op.unique_dest(base, reserved=taken)
            if u1.name != "z_1.jpg":
                errors.append(f"unique_dest second slot expected z_1.jpg, got {u1.name}")

    except ImportError as e:
        errors.append(f"import library tests: import failed: {e}")
    finally:
        if pushed and sys.path and sys.path[0] == sd:
            sys.path.pop(0)


def run_cli_edge_case_tests(errors: list[str]) -> None:
    """Argparse validation, --no-recurse, media types, --no-dedupe-scoring."""
    with tempfile.TemporaryDirectory(prefix="op_cli_") as tmp:
        base = Path(tmp)
        src = base / "src"
        dest = base / "dest"
        src.mkdir()
        dest.mkdir()

        # Invalid --skip-path (..)
        code_bad, _ = _run_cli_main_captured(
            [
                "--source",
                str(src),
                "--dest",
                str(dest),
                "--skip-path",
                "a/../b",
                "--dry-run",
            ]
        )
        if code_bad == 0:
            errors.append("CLI should reject --skip-path with ..")

        # --no-recurse: only top-level media
        write_dummy_photo(src / "visible.jpg")
        write_dummy_photo(src / "nested" / "hidden.jpg")
        code_nr, out_nr = run_organizer(
            src, dest, True, base / "st1.json", extra_args=["--no-recurse"]
        )
        if code_nr != 0:
            errors.append(f"--no-recurse exit {code_nr}")
        if "Done. Files processed: 1." not in out_nr:
            errors.append(f"--no-recurse expected 1 file, output tail:\n{out_nr[-600:]}")

        # .mp4 counted; .txt ignored (new dest/state)
        dest2 = base / "dest2"
        dest2.mkdir()
        write_dummy_photo(src / "nested" / "also.mp4")
        (src / "readme.txt").write_text("no", encoding="utf-8")
        code_m, out_m = run_organizer(src, dest2, True, base / "st2.json")
        if "Done. Files processed: 3." not in out_m:
            errors.append(
                f"expected 3 media files (top jpg, nested jpg, mp4), got:\n{out_m[-700:]}"
            )

        # --silence-skipped: do not print blocks for skipped duplicates (dedupe scoring on by default)
        src_s = base / "src_silence"
        dest_s = base / "dest_silence"
        src_s.mkdir()
        dest_s.mkdir()
        write_dummy_photo(src_s / "a" / "same.jpg")
        write_dummy_photo(src_s / "b" / "same.jpg")
        code_s, out_s = run_organizer(
            src_s, dest_s, True, base / "st_s.json", extra_args=["--silence-skipped"]
        )
        if code_s != 0:
            errors.append(f"--silence-skipped exit {code_s}")
        if "SKIP duplicate:" in out_s:
            errors.append("--silence-skipped should not print SKIP duplicate blocks")
        if "Skipped duplicate:" in out_s:
            errors.append("--silence-skipped should not log Skipped duplicate at INFO level")

        # --no-dedupe-scoring: second same-name file gets _1
        src3 = base / "src3"
        dest3 = base / "dest3"
        src3.mkdir()
        dest3.mkdir()
        write_dummy_photo(src3 / "a" / "dup.jpg")
        write_dummy_photo(src3 / "b" / "dup.jpg")
        code_d, out_d = run_organizer(
            src3, dest3, True, base / "st3.json", extra_args=["--no-dedupe-scoring"]
        )
        if code_d != 0:
            errors.append(f"--no-dedupe-scoring exit {code_d}")
        if "dup_1.jpg" not in out_d.replace("\\", "/"):
            errors.append(
                "expected unique_dest suffix dup_1.jpg for second copy in legacy mode"
            )

        # Windows: dedupe scoring should collapse case/Unicode-equivalent slot names.
        if sys.platform == "win32":
            src4 = base / "src4"
            dest4 = base / "dest4"
            src4.mkdir()
            dest4.mkdir()
            write_dummy_photo(src4 / "a" / "A\u0301.JPG")
            write_dummy_photo(src4 / "b" / "\u00e1.jpg")
            code_w, out_w = run_organizer(src4, dest4, True, base / "st4.json")
            if code_w != 0:
                errors.append(f"windows slot normalization case exit {code_w}")
            if "SKIP duplicate:" not in out_w:
                errors.append(
                    "windows slot normalization: expected duplicate skip for Unicode/case equivalent names"
                )


def _configure_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def main() -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--live",
        action="store_true",
        help="Also run without --dry-run (writes copies under dest; Windows CreationTime).",
    )
    args = ap.parse_args()

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="organize_photos_test_") as tmp:
        base = Path(tmp)
        source = base / "source"
        dest = base / "by-year"
        state = base / "state.json"
        build_source_tree(source)

        print("=== Dry-run ===\n")
        code, out = run_organizer(source, dest, dry_run=True, state_file=state)
        print(out)
        if code != 0:
            errors.append(f"dry-run exit code {code}")
        check_expectations(out, errors)

        print("\n=== Dry-run (--source = dated leaf 1994-Aug) ===\n")
        dest_narrow = base / "by-year-narrow"
        state_narrow = base / "state_narrow.json"
        code_n, out_n = run_organizer(
            source / "1994-Aug", dest_narrow, dry_run=True, state_file=state_narrow
        )
        print(out_n)
        if code_n != 0:
            errors.append(f"narrow-source dry-run exit code {code_n}")
        check_narrow_source_expectations(out_n, errors)

        if state.is_file():
            print(f"\n(state file written: {state.stat().st_size} bytes)")

        if args.live:
            dest_live = base / "by-year-live"
            state_live = base / "state_live.json"
            if dest_live.exists():
                shutil.rmtree(dest_live)
            print("\n=== Live copy (no dry-run) ===\n")
            code2, out2 = run_organizer(
                source, dest_live, dry_run=False, state_file=state_live
            )
            print(out2)
            if code2 != 0:
                errors.append(f"live run exit code {code2}")
            years = [p.name for p in dest_live.iterdir() if p.is_dir()]
            if not years:
                errors.append("live run produced no year directories under dest")
            else:
                print(f"Year folders created: {sorted(years)}")
            for rel in [
                "filename_clock/IMG_20100615_143022.jpg",
                "filename_clock/shot20100615143022.jpg",
                "z2/same.jpg",
                "2020/01/15/20200115-120000/mismatch.jpg",
            ]:
                if not (source / rel).is_file():
                    errors.append(f"source file missing (should be untouched): {rel}")

            dup_path = state_live.with_name("duplicates.json")
            if not dup_path.is_file():
                errors.append("live run: expected duplicates.json beside --state-file")
            else:
                rep = json.loads(dup_path.read_text(encoding="utf-8"))
                if rep.get("count") != 1:
                    errors.append(
                        f"duplicates.json count: expected 1, got {rep.get('count')!r}"
                    )
                dups = rep.get("duplicates") or []
                if (
                    len(dups) != 1
                    or dups[0].get("source_relative") != "z2/same.jpg"
                    or dups[0].get("reason") != "skipped_duplicate_lower_or_equal_score"
                ):
                    errors.append(f"duplicates.json entries unexpected: {dups!r}")

    if errors:
        print("\nFAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\n=== --skip-path CLI smoke ===\n")
    run_skip_path_cli_smoke_test(errors)
    if errors:
        print("\nFAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\n=== organize_photos import library tests ===\n")
    run_organize_photos_import_library_tests(errors)
    if errors:
        print("\nFAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\n=== CLI edge cases ===\n")
    run_cli_edge_case_tests(errors)
    if errors:
        print("\nFAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\nOK: hardcoded expectations met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
