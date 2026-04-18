"""
Plausibility bounds and timezone-naive normalization shared by extractors and rules.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

from .models import HeuristicConfig

# EXIF and filename timestamps are usually timezone-naive; mtime is local.
TZ_NAIVE_VS_MODIFIED_EQUIV_MAX = timedelta(days=1)

# When EXIF vs clock-in-name disagree only slightly, prefer filename (camera stem).
EXIF_VS_FILENAME_CLOCK_MAX_DELTA = timedelta(minutes=1)

_RE_EXIF_OFFSET = re.compile(r"^([+-])(\d{1,2}):(\d{2})$")
# Compact EXIF-style offsets without a colon, e.g. ``+0530`` for +5:30 (India).
_RE_EXIF_OFFSET_COMPACT = re.compile(r"^([+-])(\d{2})(\d{2})$")


def _tz_from_offset_hours_minutes(sign: int, hh: int, mm: int) -> timezone | None:
    if mm < 0 or mm > 59 or hh < 0 or hh > 15:
        return None
    return timezone(sign * timedelta(hours=hh, minutes=mm))


def parse_exif_offset_string(s: str) -> tzinfo | None:
    """
    EXIF 2.31 OffsetTime / OffsetTimeOriginal values are ASCII like ``+08:00``, ``-05:00``,
    ``+05:30`` (half-hour offsets), ``+0530`` (compact), or ``Z`` / ``UTC``.
    """
    t = s.strip()
    if not t:
        return None
    ul = t.upper()
    if ul in ("Z", "UTC"):
        return timezone.utc
    m = _RE_EXIF_OFFSET.match(t)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hh, mm = int(m.group(2)), int(m.group(3))
        return _tz_from_offset_hours_minutes(sign, hh, mm)
    m2 = _RE_EXIF_OFFSET_COMPACT.match(t)
    if m2:
        sign = -1 if m2.group(1) == "-" else 1
        hh, mm = int(m2.group(2)), int(m2.group(3))
        return _tz_from_offset_hours_minutes(sign, hh, mm)
    return None


def attach_exif_offset_if_any(dt: datetime, offset_str: str | None) -> datetime:
    """Interpret naive EXIF datetime as local civil time in ``offset_str`` (if parseable)."""
    if dt.tzinfo is not None or not offset_str:
        return dt
    tz = parse_exif_offset_string(offset_str)
    if tz is None:
        return dt
    return dt.replace(tzinfo=tz)


_tz_finder_singleton: object | None = None


def _timezone_finder_instance():
    """Lazy singleton; ``None`` if ``timezonefinder`` is not installed."""
    global _tz_finder_singleton
    if _tz_finder_singleton is False:
        return None
    if _tz_finder_singleton is not None:
        return _tz_finder_singleton
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        _tz_finder_singleton = False
        return None
    _tz_finder_singleton = TimezoneFinder()
    return _tz_finder_singleton


def infer_timezone_name_from_gps(lat: float, lon: float) -> str | None:
    """
    Map a WGS84 point to an IANA timezone name (offline polygons via ``timezonefinder``).

    Returns ``None`` for oceans / lookup failure / missing dependency.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    tf = _timezone_finder_instance()
    if tf is None:
        return None
    try:
        return tf.timezone_at(lat=lat, lng=lon)
    except Exception:
        return None


def _zoneinfo_by_name(name: str):
    """Resolve IANA name; needs ``tzdata`` on Windows for ``ZoneInfo`` lookups."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo  # type: ignore[import-not-found]
        except ImportError:
            return None
    try:
        return ZoneInfo(name)  # type: ignore[misc]
    except Exception:
        return None


def naive_exif_with_gps_local_timezone(
    naive_dt: datetime, lat: float, lon: float
) -> tuple[datetime, str | None, str | None]:
    """
    EXIF without OffsetTime: treat naive clock as local civil time at ``(lat, lon)`` (EXIF spec).

    Returns ``(aware_dt, iana_name, None)`` on success.

    On failure, returns ``(naive_dt, None, reason)`` where ``reason`` explains why GPS-based
    timezone was not applied (for verbose hints / logging). ``reason`` is ``None`` when this
    path does not apply (e.g. datetime already aware).
    """
    if naive_dt.tzinfo is not None:
        return naive_dt, None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return naive_dt, None, "invalid GPS coordinates"
    tf = _timezone_finder_instance()
    if tf is None:
        return naive_dt, None, "no timezonefinder"
    try:
        name = tf.timezone_at(lat=lat, lng=lon)
    except Exception:
        return naive_dt, None, "timezonefinder error"
    if not name:
        return naive_dt, None, "no IANA zone"
    zi = _zoneinfo_by_name(name)
    if zi is None:
        return naive_dt, None, "zone data missing (tzdata)"
    try:
        return naive_dt.replace(tzinfo=zi), name, None
    except Exception:
        return naive_dt, None, "invalid civil time in zone"


def filesystem_instant_for_rule(
    new_created: datetime,
    rule_kind: str,
    exif_best: datetime | None,
    gps_iana: str | None,
) -> datetime:
    """
    Rules often emit **naive** wall times (filename digits, stripped EXIF). For
    ``filename_earlier_than_metadata`` / ``exif_earlier_than_metadata``, attach the capture
    timezone from EXIF (offset tags) or GPS IANA so :meth:`datetime.timestamp` and Windows
    file times encode the **same UTC instant** as metadata, not “those digits in the PC's zone.”
    """
    if new_created.tzinfo is not None:
        return new_created
    if rule_kind not in ("filename_earlier_than_metadata", "exif_earlier_than_metadata"):
        return new_created
    if exif_best is not None and exif_best.tzinfo is not None:
        return new_created.replace(tzinfo=exif_best.tzinfo)
    if gps_iana:
        zi = _zoneinfo_by_name(gps_iana)
        if zi is not None:
            return new_created.replace(tzinfo=zi)
    return new_created


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


def exif_civil_clock_for_stem_compare(exif: datetime) -> datetime:
    """
    Naive civil time to compare against camera filename digits (IMG_YYYYMMDD_HHMMSS, etc.).

    EXIF DateTimeOriginal plus OffsetTimeOriginal describes **local wall time in the capture
    offset**, the same convention as typical camera stems. For that case, compare those calendar
    components to the parsed filename — **not** ``naive_local`` (host OS timezone), which would
    falsely separate e.g. stem ``...140000`` from EXIF ``14:00:00+08:00`` on a machine in UTC.

    If ``exif`` is naive (no offset tag), return it unchanged.

    If ``exif`` is timezone-aware, return the same clock fields with ``tzinfo`` stripped (civil
    time as recorded). This does not resolve XMP ``Z`` vs local-stem ambiguity; offset tags align
    EXIF with stem when both follow EXIF 2.31 semantics.
    """
    if exif.tzinfo is not None:
        return exif.replace(tzinfo=None)
    return exif


def exif_correlates_with_clock_filename(
    exif: datetime,
    fn_entries: list[tuple[datetime, bool]],
) -> bool:
    """
    True when EXIF and a clock-bearing filename time differ by at most
    ``EXIF_VS_FILENAME_CLOCK_MAX_DELTA`` but are not identical.

    Pass **EXIF as read from metadata** (may be timezone-aware). Comparison uses
    :func:`exif_civil_clock_for_stem_compare` so aware EXIF is not converted to the process
    timezone before measuring distance to filename parses.

    Within this narrow window, ``exif_earlier_than_metadata`` alone is misleading (sub-minute
    skew between stem and EXIF); the filename branch should pick the embedded name time instead.
    """
    exif_wall = exif_civil_clock_for_stem_compare(exif)
    for fn_dt, inc in fn_entries:
        if not inc:
            continue
        fn_dt = naive_local(fn_dt)
        if exif_wall == fn_dt:
            continue
        if abs(exif_wall - fn_dt) <= EXIF_VS_FILENAME_CLOCK_MAX_DELTA:
            return True
    return False
