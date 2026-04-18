"""Filesystem I/O: times, copy/backup, logging, summaries, media walk."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
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


def file_times(path: Path) -> tuple[datetime, datetime]:
    """Return (creation, modification) as naive local datetimes."""
    st = path.stat()
    return datetime.fromtimestamp(st.st_ctime), datetime.fromtimestamp(st.st_mtime)


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


_FILETIME_EPOCH_1970 = 116444736000000000


def _local_dt_to_filetime_100ns(when: datetime) -> int:
    t = when.replace(microsecond=0)
    return int(_FILETIME_EPOCH_1970 + t.timestamp() * 10_000_000)


def _set_creation_time_powershell(path: Path, when: datetime) -> None:
    s = when.strftime("%Y-%m-%d %H:%M:%S")
    p = str(path.resolve())
    p_ps = p.replace("'", "''")
    cmd = (
        f"(Get-Item -LiteralPath '{p_ps}').CreationTime = "
        "[DateTime]::ParseExact("
        f"'{s}','yyyy-MM-dd HH:mm:ss',[System.Globalization.CultureInfo]::InvariantCulture)"
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


def unique_superseded_name(dest: Path, stem: str, suffix: str) -> Path:
    sup = dest / SUPERSEDED_SUBDIR
    for n in range(10_000):
        h = hashlib.sha256(f"{stem}{n}{datetime.now().timestamp()}".encode()).hexdigest()[:8]
        cand = sup / f"{stem}_{h}{suffix}"
        if not cand.exists():
            return cand
    return sup / f"{stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"


def iter_media_files(
    root: Path,
    *,
    recurse: bool = True,
    skip_path_prefixes: tuple[str, ...] = (),
) -> Iterator[Path]:
    exts = MEDIA_EXTENSIONS
    root_r = root.resolve()

    def rel_to_root(p: Path) -> str:
        return p.relative_to(root_r).as_posix()

    if not skip_path_prefixes:
        if recurse:
            for p in root_r.rglob("*"):
                if p.is_file() and p.suffix.lower() in exts:
                    yield p
        else:
            for p in root_r.iterdir():
                if p.is_file() and p.suffix.lower() in exts:
                    yield p
        return

    if not recurse:
        for p in root_r.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if _is_rel_under_skip_prefix(rel_to_root(p), skip_path_prefixes):
                continue
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
            if _is_rel_under_skip_prefix(rel_file, skip_path_prefixes):
                continue
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


def backup_path_for(backup_root: Path, rel: Path, use_flat_hash: bool) -> Path:
    if use_flat_hash:
        h = hashlib.sha256(str(rel.as_posix()).encode("utf-8")).hexdigest()[:24]
        return backup_root / f"{h}{rel.suffix.lower()}"
    out = backup_root / rel
    return out


def append_backup_manifest(
    backup_root: Path, rel: Path, backup_abs: Path, flat: bool
) -> None:
    man = backup_root / "manifest.jsonl"
    rec = {
        "source_relative": rel.as_posix(),
        "backup": backup_abs.name if flat else backup_abs.relative_to(backup_root).as_posix(),
    }
    with man.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


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
) -> None:
    from .rules import primary_time_action

    primary = primary_time_action(planned)
    rule = primary.kind if primary is not None else "move_only_no_time_change"
    change = (
        primary.new_created.isoformat(sep=" ", timespec="seconds")
        if (primary is not None and primary.new_created is not None)
        else "-"
    )

    exif_s = exif_original.isoformat(sep=" ", timespec="seconds") if exif_original else "-"
    if exif_capture_hint:
        exif_s = f"{exif_s} ({exif_capture_hint})" if exif_s != "-" else f"({exif_capture_hint})"
    created_s = created.isoformat(sep=" ", timespec="seconds")
    modified_s = modified.isoformat(sep=" ", timespec="seconds")

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
) -> dict[str, Any]:
    return {
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
