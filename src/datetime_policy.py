"""
Plausibility bounds and timezone-naive normalization shared by extractors and rules.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

from .models import HeuristicConfig

# EXIF and filename timestamps are usually timezone-naive; mtime is local.
TZ_NAIVE_VS_MODIFIED_EQUIV_MAX = timedelta(days=1)

_RE_EXIF_OFFSET = re.compile(r"^([+-])(\d{1,2}):(\d{2})$")


def parse_exif_offset_string(s: str) -> tzinfo | None:
    """
    EXIF 2.31 OffsetTime / OffsetTimeOriginal values are ASCII like ``+08:00`` or ``-05:00``.
    """
    m = _RE_EXIF_OFFSET.match(s.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    hh, mm = int(m.group(2)), int(m.group(3))
    return timezone(sign * timedelta(hours=hh, minutes=mm))


def attach_exif_offset_if_any(dt: datetime, offset_str: str | None) -> datetime:
    """Interpret naive EXIF datetime as local civil time in ``offset_str`` (if parseable)."""
    if dt.tzinfo is not None or not offset_str:
        return dt
    tz = parse_exif_offset_string(offset_str)
    if tz is None:
        return dt
    return dt.replace(tzinfo=tz)


def safe_datetime(
    y: int,
    m: int,
    d: int,
    H: int = 0,
    Mi: int = 0,
    S: int = 0,
    *,
    microsecond: int = 0,
) -> datetime | None:
    try:
        return datetime(y, m, d, H, Mi, S, microsecond)
    except ValueError:
        return None


def naive_local(dt: datetime) -> datetime:
    """
    XMP/ISO metadata may yield timezone-aware datetimes; file times from Python are naive local.
    Convert aware values to naive local wall time so comparisons do not raise TypeError.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def is_plausible_capture_date(dt: datetime, now: datetime, cfg: HeuristicConfig) -> bool:
    dt = naive_local(dt)
    now = naive_local(now) if now.tzinfo is not None else now
    if dt.year < cfg.min_year or dt.year > cfg.max_year:
        return False
    if dt > now + cfg.future_slack:
        return False
    return True


def exif_ambiguous_vs_modified(exif: datetime, modified: datetime) -> bool:
    """True when EXIF and mtime are close but not identical (likely TZ / naive-EXIF skew)."""
    exif = naive_local(exif)
    modified = naive_local(modified)
    if exif == modified:
        return False
    return abs(exif - modified) <= TZ_NAIVE_VS_MODIFIED_EQUIV_MAX


def filename_ambiguous_vs_modified(dt: datetime, modified: datetime) -> bool:
    """True when filename datetime and mtime are close but not identical (likely TZ mismatch)."""
    dt = naive_local(dt)
    modified = naive_local(modified)
    if dt == modified:
        return False
    return abs(dt - modified) <= TZ_NAIVE_VS_MODIFIED_EQUIV_MAX


def has_two_correlated_file_indicators(
    *,
    fn_entries: list[tuple[datetime, bool]],
    exif_original: datetime | None,
    modified: datetime,
    now: datetime,
    cfg: HeuristicConfig,
) -> bool:
    """
    Return True when at least two capture indicators on the file corroborate each other.

    Indicators considered:
    - mtime
    - earliest parsed filename datetime (if any, plausible)
    - EXIF DateTimeOriginal (if any, plausible)

    Corroboration is defined as being within ±1 day (or equal).
    """
    cands: list[datetime] = [modified]

    fn_plaus = [d for d, _inc in fn_entries if is_plausible_capture_date(d, now, cfg)]
    if fn_plaus:
        cands.append(min(fn_plaus))

    if exif_original is not None and is_plausible_capture_date(exif_original, now, cfg):
        cands.append(exif_original)

    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if abs(cands[i] - cands[j]) <= TZ_NAIVE_VS_MODIFIED_EQUIV_MAX:
                return True
    return False


def best_capture_time_for_folder_vs_metadata(
    *,
    fn_entries: list[tuple[datetime, bool]],
    exif_original: datetime | None,
    modified: datetime,
    now: datetime,
    cfg: HeuristicConfig,
) -> datetime:
    """
    Pick a single timestamp for comparing against folder dating.

    Prefer timezone-naive capture signals (filename, EXIF) when they are plausible and not
    ambiguous vs mtime (±1 day). When multiple capture signals exist, use the earliest.

    Falls back to mtime when no reliable capture signal exists.
    """
    cands: list[datetime] = []

    for d, inc in fn_entries:
        if not is_plausible_capture_date(d, now, cfg):
            continue
        if inc and filename_ambiguous_vs_modified(d, modified):
            continue
        cands.append(d)

    if (
        exif_original is not None
        and is_plausible_capture_date(exif_original, now, cfg)
        and not exif_ambiguous_vs_modified(exif_original, modified)
    ):
        cands.append(exif_original)

    return min(cands) if cands else modified
