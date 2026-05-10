"""Filesystem I/O: times, copy, logging, summaries, media walk."""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator

from .models import MEDIA_EXTENSIONS, SUPERSEDED_SUBDIR

if TYPE_CHECKING:
    from .models import Action


def _local_naive_from_stat_seconds(ts: float) -> datetime | None:
    """
    Convert a :meth:`Path.stat` timestamp to naive local datetime.

    Returns None when the value is non-finite or :func:`datetime.fromtimestamp`
    refuses it (Windows can raise ``OSError`` for out-of-range storage).
    """
    try:
        if math.isnan(ts) or math.isinf(ts):
            return None
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def file_times(path: Path) -> tuple[datetime, datetime]:
    """Return (creation, modification) as naive local datetimes.

    On Windows, ``st_ctime`` is creation time. If either timestamp cannot be
    converted, the other is reused; if both fail, both are set to now.
    """
    st = path.stat()
    c = _local_naive_from_stat_seconds(st.st_ctime)
    m = _local_naive_from_stat_seconds(st.st_mtime)
    log = logging.getLogger("organize_photos")

    if c is None and m is not None:
        log.warning(
            "Unusable creation time (ctime=%s) for %s; using modification time for both.",
            st.st_ctime,
            path,
        )
        c = m
    elif m is None and c is not None:
        log.warning(
            "Unusable modification time (mtime=%s) for %s; using creation time for both.",
            st.st_mtime,
            path,
        )
        m = c
    elif c is None and m is None:
        log.warning(
            "Unusable creation and modification time (ctime=%s, mtime=%s) for %s; "
            "using current local time for both.",
            st.st_ctime,
            st.st_mtime,
            path,
        )
        now = datetime.now()
        return now, now

    assert c is not None and m is not None
    return c, m


def same_size_created_mtime(a: Path, b: Path) -> bool:
    """
    True when both paths exist and match on size, CreationTime, and mtime (seconds precision).
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    if sa.st_size != sb.st_size:
        return False
    ca, ma = file_times(a)
    cb, mb = file_times(b)
    return (
        ca.replace(microsecond=0) == cb.replace(microsecond=0)
        and ma.replace(microsecond=0) == mb.replace(microsecond=0)
    )


def files_identical_bytes(a: Path, b: Path) -> bool:
    """True when both files exist, same size, and byte-for-byte equal."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    bufsize = 1024 * 1024
    with a.open("rb") as f1, b.open("rb") as f2:
        while True:
            c1 = f1.read(bufsize)
            c2 = f2.read(bufsize)
            if c1 != c2:
                return False
            if not c1:
                return True


def resolve_organize_destination(
    source_file: Path,
    *,
    fp_resolved: Path,
    canonical_dest: Path,
    dry_run_claims: dict[Path, Path] | None = None,
) -> tuple[Path, str]:
    """
    Pick the destination path for one organize copy under ``canonical_dest``.

    Returns ``(target, kind)`` where ``kind`` is:

    - ``already_at_dest`` — resolved source equals canonical path (nothing to write).
    - ``skip_identical`` — ``target`` already holds the same bytes; do not overwrite.
    - ``copy`` — write to ``target`` (path does not exist yet for this session).

    If something already exists at the canonical basename with **different** bytes, the next
    free name ``stem_1``, ``stem_2``, … in the same folder is chosen (filesystem + optional
    ``dry_run_claims`` reserve planned paths when nothing exists on disk yet).

    ``dry_run_claims`` maps resolved dest path → resolved source path for destinations
    “claimed” earlier in the same dry-run pass (so duplicate sources match without a disk copy).
    """
    cand0 = canonical_dest
    try:
        if cand0.resolve() == fp_resolved:
            return cand0, "already_at_dest"
    except OSError:
        pass

    parent = cand0.parent
    stem = cand0.stem
    suf = cand0.suffix

    def key(p: Path) -> Path:
        try:
            return p.resolve()
        except OSError:
            return p

    max_suffix = 1_000_000
    for i in range(max_suffix):
        cand = cand0 if i == 0 else parent / f"{stem}_{i}{suf}"
        rk = key(cand)
        try:
            is_same_path = rk == fp_resolved
        except OSError:
            is_same_path = False
        if is_same_path:
            return cand, "already_at_dest"

        if cand.is_file():
            if rk != fp_resolved and files_identical_bytes(source_file, cand):
                return cand, "skip_identical"
            continue

        if dry_run_claims is not None and rk in dry_run_claims:
            occupied_by = dry_run_claims[rk]
            try:
                if rk != fp_resolved and files_identical_bytes(source_file, occupied_by):
                    return cand, "skip_identical"
            except OSError:
                pass
            continue

        try:
            if cand.exists():
                continue
        except OSError:
            continue

        return cand, "copy"

    raise RuntimeError(
        f"Could not allocate a destination name under {parent} "
        f"for stem {stem!r} (attempted suffixes 0..{max_suffix})"
    )


_FILETIME_EPOCH_1970 = 116444736000000000


def _local_dt_to_filetime_100ns(when: datetime) -> int:
    t = when.replace(microsecond=0)
    return int(_FILETIME_EPOCH_1970 + t.timestamp() * 10_000_000)


def _set_creation_time_powershell(path: Path, when: datetime) -> None:
    """Set CreationTime from the same UTC instant as ``when.timestamp()`` (aware or naive)."""
    p = str(path.resolve())
    p_ps = p.replace("'", "''")
    ms = int(round(when.timestamp() * 1000))
    cmd = (
        f"(Get-Item -LiteralPath '{p_ps}').CreationTime = "
        f"([DateTimeOffset]::FromUnixTimeMilliseconds([long]{ms})).LocalDateTime"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise OSError(f"Set CreationTime failed ({r.returncode}): {err or 'PowerShell error'}")


def _set_creation_time_ctypes(path: Path, when: datetime) -> None:
    if sys.platform != "win32":
        raise OSError("ctypes CreationTime is Windows-only")

    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    ft_val = _local_dt_to_filetime_100ns(when)
    ft = FILETIME()
    ft.dwLowDateTime = ft_val & 0xFFFFFFFF
    ft.dwHighDateTime = (ft_val >> 32) & 0xFFFFFFFF

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80

    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    SetFileTime = kernel32.SetFileTime
    SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    SetFileTime.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    p = str(path.resolve())
    handle = CreateFileW(
        p,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        if not SetFileTime(handle, ctypes.byref(ft), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        CloseHandle(handle)


def set_creation_time_windows(path: Path, when: datetime, dry_run: bool) -> None:
    if dry_run:
        return
    if os.environ.get("ORGANIZE_USE_POWERSHELL_CREATION_TIME") == "1":
        _set_creation_time_powershell(path, when)
        return
    if sys.platform == "win32":
        try:
            _set_creation_time_ctypes(path, when)
            return
        except OSError as e:
            log = logging.getLogger("organize_photos")
            log.warning(
                "ctypes SetFileTime failed (%s); falling back to PowerShell for %s",
                e,
                path,
            )
    _set_creation_time_powershell(path, when)


def dest_path_from_slot_relative(dest: Path, target_relative: str) -> Path:
    """Build path under dest from 'YYYY/name.ext' posix string."""
    norm = target_relative.replace("\\", "/")
    p = PurePosixPath(norm)
    return dest.joinpath(*p.parts) if p.parts else dest


def is_prior_organizer_output(
    file_path: Path,
    dest_root: Path,
    *,
    min_year: int,
    max_year: int,
) -> bool:
    try:
        rel = file_path.relative_to(dest_root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if parts[0] == SUPERSEDED_SUBDIR:
        return True
    if len(parts) == 2:
        y_s = parts[0]
        if len(y_s) == 4 and y_s.isdigit():
            y = int(y_s)
            if min_year <= y <= max_year:
                return True
    return False


def _is_rel_under_skip_prefix(rel_posix: str, skip_prefixes: tuple[str, ...]) -> bool:
    if not skip_prefixes:
        return False
    for sp in skip_prefixes:
        if rel_posix == sp or rel_posix.startswith(sp + "/"):
            return True
    return False


def normalize_skip_path_arg(s: str) -> str:
    t = s.strip().replace("\\", "/")
    while t.startswith("/"):
        t = t[1:]
    t = t.strip()
    if not t:
        raise argparse.ArgumentTypeError("empty path")
    parts: list[str] = []
    for seg in PurePosixPath(t).parts:
        if seg == "..":
            raise argparse.ArgumentTypeError("--skip-path must not contain '..'")
        if seg in (".", ""):
            continue
        parts.append(seg)
    if not parts:
        raise argparse.ArgumentTypeError("empty path")
    return str(PurePosixPath(*parts))


def normalize_include_glob_arg(s: str) -> str:
    """
    Normalize ``--include-glob`` patterns: forward slashes, no ``..`` segments.
    Glob metacharacters (``*``, ``?``, ``**``, ``[]``) are preserved.
    """
    t = s.strip().replace("\\", "/")
    while t.startswith("/"):
        t = t[1:]
    t = t.strip()
    if not t:
        raise argparse.ArgumentTypeError("empty glob pattern")
    for seg in PurePosixPath(t).parts:
        if seg == "..":
            raise argparse.ArgumentTypeError("--include-glob must not contain '..'")
    return str(PurePosixPath(t))


def _rel_matches_include_globs(rel_posix: str, patterns: tuple[str, ...]) -> bool:
    """
    True if ``rel_posix`` matches any glob (pathlib semantics, including ``**``).

    pathlib does not treat ``**/*.ext`` as matching a top-level ``file.ext``; for patterns
    of the form ``**/rest`` where ``rest`` has no further ``/``, we also try ``rest`` alone
    so ``--include-glob \"**/*.mp4\"`` includes files directly under ``--source``.
    """
    if not patterns:
        return True
    rel = PurePosixPath(rel_posix)
    for pat in patterns:
        if rel.match(pat):
            return True
        if pat.startswith("**/"):
            rest = pat[3:]
            if "/" not in rest and rel.match(rest):
                return True
    return False


def iter_media_files(
    root: Path,
    *,
    recurse: bool = True,
    skip_path_prefixes: tuple[str, ...] = (),
    include_globs: tuple[str, ...] = (),
) -> Iterator[Path]:
    exts = MEDIA_EXTENSIONS
    root_r = root.resolve()

    def rel_to_root(p: Path) -> str:
        return p.relative_to(root_r).as_posix()

    def file_allowed(rel_posix: str) -> bool:
        if skip_path_prefixes and _is_rel_under_skip_prefix(rel_posix, skip_path_prefixes):
            return False
        if include_globs and not _rel_matches_include_globs(rel_posix, include_globs):
            return False
        return True

    if not skip_path_prefixes:
        if recurse:
            for p in root_r.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in exts:
                    continue
                if file_allowed(rel_to_root(p)):
                    yield p
        else:
            for p in root_r.iterdir():
                if not p.is_file() or p.suffix.lower() not in exts:
                    continue
                if file_allowed(rel_to_root(p)):
                    yield p
        return

    if not recurse:
        for p in root_r.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if file_allowed(rel_to_root(p)):
                yield p
        return

    for dirpath, dirnames, filenames in os.walk(root_r, topdown=True):
        dpath = Path(dirpath)
        rel_dir = rel_to_root(dpath) if dpath != root_r else ""

        keep_dirs: list[str] = []
        for name in dirnames:
            sub = f"{rel_dir}/{name}" if rel_dir else name
            if _is_rel_under_skip_prefix(sub, skip_path_prefixes):
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs

        for name in filenames:
            p = dpath / name
            if p.suffix.lower() not in exts:
                continue
            rel_file = f"{rel_dir}/{name}" if rel_dir else name
            if file_allowed(rel_file):
                yield p


def unique_dest(path: Path, *, reserved: set[Path] | None = None) -> Path:
    def taken(p: Path) -> bool:
        if p.exists():
            return True
        if reserved:
            try:
                key = p.resolve()
            except OSError:
                key = p
            if key in reserved:
                return True
        return False

    if not taken(path):
        return path
    base = path.stem
    suf = path.suffix
    parent = path.parent
    n = 1
    while True:
        cand = parent / f"{base}_{n}{suf}"
        if not taken(cand):
            return cand
        n += 1


def setup_logging(log_file: Path | None, verbose: bool) -> logging.Logger:
    log = logging.getLogger("organize_photos")
    log.handlers.clear()
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        from rich.logging import RichHandler  # type: ignore

        ch: logging.Handler = RichHandler(
            rich_tracebacks=False,
            show_path=False,
            show_level=True,
            show_time=True,
            omit_repeated_times=False,
        )
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    except Exception:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
    log.addHandler(ch)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


def default_log_path() -> Path:
    script_dir = Path(__file__).resolve().parent.parent
    logs_dir = script_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir / f"organize_photos_{stamp}.log"


DEFAULT_LOG_ARG_SENTINEL = "__DEFAULT__"


def resolve_log_path_arg(v: str) -> Path:
    if v == DEFAULT_LOG_ARG_SENTINEL:
        return default_log_path()
    return Path(v).expanduser().resolve()


def _pc_local_utc_offset_colon() -> str:
    """Current OS zone offset like ``+02:00`` (for annotating naive wall times)."""
    dt = datetime.now().astimezone()
    s = dt.strftime("%z")
    if len(s) == 5 and s[0] in "+-":
        return f"{s[:3]}:{s[3:]}"
    return s or "unknown"


def _fmt_naive_dt_with_pc_zone(dt: datetime) -> str:
    """Naive source/copy wall time with explicit PC zone, e.g. ``2023-10-25 07:15:00 [+03:00]``."""
    return f"{dt.isoformat(sep=' ', timespec='seconds')}{_pc_local_utc_offset_colon()}"


def _fmt_set_created_display(dt: datetime | None) -> str:
    """Aware datetimes include offset in ISO string; naive rules get `` [+xx:xx]`` for PC zone."""
    if dt is None:
        return "-"
    if dt.tzinfo is not None:
        return dt.isoformat(sep=" ", timespec="seconds")
    return _fmt_naive_dt_with_pc_zone(dt)


def _fmt_exif_display(exif_original: datetime | None, exif_capture_hint: str | None) -> str:
    if exif_original is not None:
        return exif_original.isoformat(sep=" ", timespec="seconds")
    if exif_capture_hint:
        for part in exif_capture_hint.split(";"):
            part = part.strip()
            if part.startswith("tz~GPS="):
                return f"-  [{part.split('=', 1)[1]}]"
        return f"-  ({exif_capture_hint})"
    return "-"


def emit_file_summary(
    *,
    out: Any,
    rel_posix: str,
    created: datetime,
    modified: datetime,
    exif_original: datetime | None,
    planned: list["Action"],
    dest_text: str,
    outcome_text: str,
    exif_capture_hint: str | None = None,
    materialized_created: datetime | None = None,
) -> None:
    from .rules import primary_time_action

    primary = primary_time_action(planned)
    rule = primary.kind if primary is not None else "move_only_no_time_change"
    disp = (
        materialized_created
        if materialized_created is not None
        else (primary.new_created if primary is not None else None)
    )
    change = _fmt_set_created_display(disp)

    exif_s = _fmt_exif_display(exif_original, exif_capture_hint)
    created_s = _fmt_naive_dt_with_pc_zone(created)
    modified_s = _fmt_naive_dt_with_pc_zone(modified)

    out.write(f"{rel_posix}\n")
    out.write(f"  created={created_s}  modified={modified_s}  exif={exif_s}\n")
    out.write(f"  rule={rule}  set_created={change}  dest={dest_text}\n")
    out.write(f"  {outcome_text}\n\n")


def emit_to_both(
    *,
    out_stream: Any,
    run_log: Any,
    **kwargs: Any,
) -> None:
    """Write the same summary to stdout and the run log (DRY)."""
    emit_file_summary(out=out_stream, **kwargs)
    emit_file_summary(out=run_log, **kwargs)


def record_skip_duplicate(
    *,
    source_relative: str,
    competing_target: str,
    incumbent_score: int,
    candidate_score: int,
    candidate_rule: str,
    incumbent_source: Any,
    sequence: int,
) -> dict[str, Any]:
    return {
        "source_relative": source_relative,
        "reason": "skipped_duplicate_lower_or_equal_score",
        "competing_target": competing_target,
        "incumbent_score": incumbent_score,
        "candidate_score": candidate_score,
        "candidate_rule": candidate_rule,
        "incumbent_source": incumbent_source,
        "sequence": sequence,
    }


def record_skip_identical(
    *,
    source_relative: str,
    competing_target: str,
    incumbent_source: Any,
    sequence: int,
) -> dict[str, Any]:
    return {
        "source_relative": source_relative,
        "reason": "skipped_identical_to_dest",
        "competing_target": competing_target,
        "incumbent_source": incumbent_source,
        "sequence": sequence,
    }


def record_winner(
    *,
    source_relative: str,
    score: int,
    rule: str,
    sequence: int,
    original_created: str | None,
    original_modified: str | None,
    final_created: str | None,
    final_modified: str | None,
    target_relative: str,
    exif_original: str | None,
    filename: str,
    materialized_timezone: str | None = None,
    exif_timezone: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "source_relative": source_relative,
        "score": score,
        "rule": rule,
        "sequence": sequence,
        "original_created": original_created,
        "original_modified": original_modified,
        "final_created": final_created,
        "final_modified": final_modified,
        "target_relative": target_relative,
        "exif_original": exif_original,
        "filename": filename,
    }
    if materialized_timezone:
        d["materialized_timezone"] = materialized_timezone
    if exif_timezone:
        d["exif_timezone"] = exif_timezone
    return d
