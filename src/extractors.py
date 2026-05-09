"""Filename, path, and EXIF parsing (pure functions)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .datetime_policy import (
    attach_exif_offset_if_any,
    is_plausible_capture_date,
    naive_exif_with_gps_local_timezone,
    naive_local,
    safe_datetime,
)
from .models import ExifCaptureInfo, HeuristicConfig, VIDEO_EXTENSIONS

# Filename date patterns (tightened where noted)
_RE_DATETIME_COMPACT = re.compile(
    r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})"
)
_RE_DATE_SEP = re.compile(
    r"(?P<y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})"
    r"(?:[-_T ](?P<H>\d{2})[-:](?P<M>\d{2})[-:](?P<S>\d{2}))?"
)
_RE_DATE_COMPACT8 = re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)")
_RE_APPLE_IMG = re.compile(
    r"(?:^|[_\s-])IMG[_-](?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-](?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})(?!\d)",
    re.IGNORECASE,
)
_RE_PXL = re.compile(
    r"(?:^|[_\s-])PXL[_-](?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-](?P<hms>\d{6})\d*(?!\d)",
    re.IGNORECASE,
)
_RE_WA = re.compile(
    r"(?:^|[_\s-])IMG[_-](?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[_-]WA\d+",
    re.IGNORECASE,
)
_RE_SCREENSHOT_ANDROID = re.compile(
    r"(?:^|[_\s-])Screenshot_(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})-"
    r"(?P<H>\d{2})-(?P<Mi>\d{2})-(?P<S>\d{2})-(?P<ms>\d{3})(?=\D|$)",
    re.IGNORECASE,
)
_RE_DATE_SEP_AT_DOT_TIME = re.compile(
    r"(?P<y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})"
    r"(?:\s+at\s+(?P<h>\d{1,2})\.(?P<mi>\d{2})(?:\.(?P<se>\d{2}))?\s*(?P<ampm>AM|PM)?)?",
    re.IGNORECASE,
)


def _decode_exif_ascii_tag(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            s = v.decode("ascii", "replace").strip()
        except Exception:
            return None
    elif isinstance(v, str):
        s = v.strip()
    else:
        return None
    return s if s else None


def _ratio_to_float(v: object) -> float:
    if isinstance(v, (tuple, list)) and len(v) >= 2:
        try:
            a, b = float(v[0]), float(v[1])
            return a / b if b else a
        except Exception:
            pass
    try:
        return float(v)  # IFDRational
    except Exception:
        return 0.0


def _gps_dms_tuple_to_degrees(parts: object, ref: object | None, *, is_latitude: bool) -> float | None:
    if not isinstance(parts, (tuple, list)) or len(parts) < 3:
        return None
    d = _ratio_to_float(parts[0])
    m = _ratio_to_float(parts[1])
    s = _ratio_to_float(parts[2])
    deg = d + m / 60.0 + s / 3600.0
    r = ref
    if isinstance(r, (bytes, bytearray)):
        r = r.decode("ascii", "replace").strip().upper()
    elif r is not None:
        r = str(r).strip().upper()
    else:
        r = ""
    if is_latitude:
        if r.startswith("S"):
            deg = -deg
    else:
        if r.startswith("W"):
            deg = -deg
    return deg


def _gps_ifd_to_lat_lon(gps_ifd: dict[int, object]) -> tuple[float, float] | None:
    lat_tup = gps_ifd.get(2)
    lon_tup = gps_ifd.get(4)
    if lat_tup is None or lon_tup is None:
        return None
    try:
        lat = _gps_dms_tuple_to_degrees(lat_tup, gps_ifd.get(1), is_latitude=True)
        lon = _gps_dms_tuple_to_degrees(lon_tup, gps_ifd.get(3), is_latitude=False)
    except Exception:
        return None
    if lat is None or lon is None:
        return None
    return (lat, lon)


def _parse_exif_gps_lat_lon(exif: object) -> tuple[float, float] | None:
    try:
        gps = exif.get_ifd(0x8825)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not gps:
        return None
    return _gps_ifd_to_lat_lon(gps)


_APPLE_MOVIE_EPOCH_UTC = datetime(1904, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

_MP4_CONTAINER_TAGS = frozenset(
    {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"edts",
        b"udta",
        b"meta",
        b"ilst",
        b"dinf",
    }
)


def _apple_seconds_to_utc_aware(seconds: int) -> datetime | None:
    """QuickTime ``mvhd`` / ``mdhd`` creation_time (seconds since 1904-01-01 UTC)."""
    if seconds <= 0:
        return None
    try:
        dt = _APPLE_MOVIE_EPOCH_UTC + timedelta(seconds=seconds)
    except OverflowError:
        return None
    if not (1980 <= dt.year <= 2105):
        return None
    return dt


def _parse_video_tag_datetime(s: str) -> datetime | None:
    """Parse ``creation_time`` / container tags from ffprobe (Explorer “Media created” source)."""
    t = s.strip()
    if not t:
        return None
    if t.lower().startswith("utc "):
        t = t[4:].strip()
    if len(t) == 4 and t.isdigit():
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        try:
            return datetime(int(t[:4]), int(t[5:7]), int(t[8:10]), 0, 0, 0, tzinfo=timezone.utc)
        except ValueError:
            return None
    if t[-1:] in ("Z", "z") and "T" in t:
        t = t[:-1] + "+00:00"
    for cand in (t, t.replace("T", " ", 1)):
        try:
            return datetime.fromisoformat(cand)
        except ValueError:
            continue
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
    ):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _ffprobe_embedded_datetime(path: Path) -> tuple[datetime, str] | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [
                exe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        doc = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None

    def try_tags(tags: object, label: str) -> tuple[datetime, str] | None:
        if not isinstance(tags, dict):
            return None
        for key in (
            "creation_time",
            "com.apple.quicktime.creationdate",
            "date",
            "DATE",
            "creation_date",
            "DATE_ENCODED",
            "com.apple.quicktime.creationDate",
        ):
            if key not in tags:
                continue
            dt = _parse_video_tag_datetime(str(tags[key]))
            if dt is not None:
                return (dt, f"ffprobe:{label}.{key}")
        for k, v in tags.items():
            if str(k).lower() == "creation_time":
                dt = _parse_video_tag_datetime(str(v))
                if dt is not None:
                    return (dt, f"ffprobe:{label}.{k}")
        return None

    fmt = doc.get("format")
    if isinstance(fmt, dict):
        got = try_tags(fmt.get("tags"), "format.tags")
        if got is not None:
            return got

    for stream in doc.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") != "video":
            continue
        got = try_tags(stream.get("tags"), "stream.video.tags")
        if got is not None:
            return got
    return None


def _mp4_read_atom_bounds(buf: bytes, pos: int, hard_end: int) -> tuple[int, bytes, int, int] | None:
    if pos + 8 > hard_end:
        return None
    sz0 = int.from_bytes(buf[pos : pos + 4], "big")
    tag = buf[pos + 4 : pos + 8]
    if sz0 == 0:
        sz = hard_end - pos
        hlen = 8
    elif sz0 == 1:
        if pos + 16 > hard_end:
            return None
        sz = int.from_bytes(buf[pos + 8 : pos + 16], "big")
        hlen = 16
    else:
        sz = sz0
        hlen = 8
    if sz < hlen:
        return None
    atom_end = pos + sz
    if atom_end > hard_end:
        return None
    body_start = pos + hlen
    return (atom_end, tag, body_start, atom_end)


def _parse_mvhd_timescale_duration(buf: bytes, b0: int, b1: int) -> datetime | None:
    if b0 >= b1 or b0 + 4 > b1:
        return None
    ver = buf[b0]
    if ver == 0:
        if b0 + 16 > b1:
            return None
        ct = int.from_bytes(buf[b0 + 12 : b0 + 16], "big")
    elif ver == 1:
        if b0 + 24 > b1:
            return None
        ct = int.from_bytes(buf[b0 + 12 : b0 + 20], "big")
    else:
        return None
    return _apple_seconds_to_utc_aware(ct)


def _walk_mp4_for_mvhd(buf: bytes, start: int, end: int) -> datetime | None:
    pos = start
    while pos + 8 <= end:
        part = _mp4_read_atom_bounds(buf, pos, end)
        if part is None:
            break
        atom_end, tag, b0, b1 = part
        if tag == b"mvhd":
            return _parse_mvhd_timescale_duration(buf, b0, b1)
        if tag in _MP4_CONTAINER_TAGS:
            got = _walk_mp4_for_mvhd(buf, b0, b1)
            if got is not None:
                return got
        pos = atom_end
    return None


def _read_bytes_for_mp4_scan(path: Path) -> bytes | None:
    try:
        n = path.stat().st_size
    except OSError:
        return None
    max_full = 96 * 1024 * 1024
    try:
        with path.open("rb") as f:
            if n <= max_full:
                return f.read()
            head = f.read(8 * 1024 * 1024)
            f.seek(max(0, n - 16 * 1024 * 1024))
            tail = f.read()
            return head + tail
    except OSError:
        return None


def _mp4_mvhd_creation_utc(path: Path) -> datetime | None:
    suf = path.suffix.lower()
    if suf not in {".mp4", ".m4v", ".mov", ".3gp"}:
        return None
    buf = _read_bytes_for_mp4_scan(path)
    if not buf:
        return None
    return _walk_mp4_for_mvhd(buf, 0, len(buf))


def _video_embedded_capture_datetime(path: Path) -> tuple[datetime, str] | None:
    got = _ffprobe_embedded_datetime(path)
    if got is not None:
        return got
    mv = _mp4_mvhd_creation_utc(path)
    if mv is not None:
        return (mv, "mp4:mvhd.creation_time")
    return None


def read_exif_capture(path: Path) -> ExifCaptureInfo:
    """
    Return best capture datetime from embedded metadata plus EXIF 2.31 offset tags and GPS.

    - **Video** (extensions in ``VIDEO_EXTENSIONS``): ``ffprobe`` format/stream tags such as
      ``creation_time`` (often shown as “Media created”), then MP4/MOV ``mvhd`` time (Apple epoch)
      if ``ffprobe`` is unavailable or has no usable tags.
    - DateTimeOriginal (0x9003) + OffsetTimeOriginal (0x9011) when present → timezone-aware datetime.
    - If no offset tags but GPS lat/lon parse → IANA zone via ``timezonefinder`` (offline), then
      treat naive EXIF clock as local civil time in that zone (``gps_timezone_name`` in hints).
      If that fails, ``gps_tz_skip`` / ``GPS~no_tz=…`` explains why (e.g. missing ``tzdata``).
    - GPS IFD (0x8825) non-empty → ``has_gps`` (``GPS=yes`` in verbose when no inferred tz line).
    - PNG: PNG text / XMP paths unchanged (no EXIF offset / GPS in most exports).
    """
    suf = path.suffix.lower()
    if suf in VIDEO_EXTENSIONS:
        vm = _video_embedded_capture_datetime(path)
        if vm is not None:
            dt, src = vm
            return ExifCaptureInfo(dt, video_metadata_source=src)

    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        return ExifCaptureInfo(None)

    def _parse_dt_string(s: str) -> datetime | None:
        t = s.strip()
        if not t:
            return None
        if t[-1:] in ("Z", "z") and "T" in t:
            t = t[:-1] + "+00:00"
        for cand in (t, t.replace("T", " ")):
            try:
                return datetime.fromisoformat(cand)
            except ValueError:
                pass
        for fmt in (
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
        ):
            try:
                return datetime.strptime(t, fmt)
            except ValueError:
                continue
        return None

    def _parse_exif_naive_datetime(raw: str) -> datetime | None:
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return None

    empty = ExifCaptureInfo(None)
    exif = None
    try:
        with Image.open(path) as im:
            exif = im.getexif()

            if not exif:
                raw = im.info.get("exif")
                if isinstance(raw, (bytes, bytearray)) and raw:
                    try:
                        ex = Image.Exif()
                        ex.load(raw)  # type: ignore[attr-defined]
                        exif = ex
                    except Exception:
                        exif = exif

            if not exif:
                info = getattr(im, "info", {}) or {}
                for k in (
                    "Creation Time",
                    "CreationTime",
                    "creation_time",
                    "date:create",
                    "date:modify",
                    "DateTimeOriginal",
                    "DateTime",
                ):
                    v = info.get(k)
                    if isinstance(v, str):
                        dt = _parse_dt_string(v)
                        if dt is not None:
                            return ExifCaptureInfo(dt)
                for k in ("XML:com.adobe.xmp", "xmp", "XMP"):
                    v = info.get(k)
                    if isinstance(v, (bytes, bytearray)):
                        try:
                            v = v.decode("utf-8", "replace")
                        except Exception:
                            v = None
                    if isinstance(v, str) and v:
                        m = re.search(
                            r"(?:xmp:CreateDate|photoshop:DateCreated|exif:DateTimeOriginal|xmp:ModifyDate)=\"([^\"]+)\"",
                            v,
                            re.IGNORECASE,
                        )
                        if m:
                            dt = _parse_dt_string(m.group(1))
                            if dt is not None:
                                return ExifCaptureInfo(dt)

            if not exif:
                return empty
    except OSError:
        return empty

    has_gps = False
    try:
        has_gps = bool(exif.get_ifd(0x8825))
    except Exception:
        has_gps = False

    gps_ll = _parse_exif_gps_lat_lon(exif)

    try:
        ifd = exif.get_ifd(0x8769)
    except Exception:
        ifd = {}

    off_o = _decode_exif_ascii_tag(ifd.get(0x9011))
    off_d = _decode_exif_ascii_tag(ifd.get(0x9012))
    off_main = _decode_exif_ascii_tag(exif.get(0x9010))

    gps_tz_used: str | None = None
    gps_tz_skip: str | None = None

    def _combine(raw: str, offset: str | None) -> datetime | None:
        nonlocal gps_tz_used, gps_tz_skip
        gps_tz_used = None
        gps_tz_skip = None
        naive = _parse_exif_naive_datetime(raw)
        if naive is None:
            return None
        dt = attach_exif_offset_if_any(naive, offset)
        if dt.tzinfo is None and gps_ll is not None:
            lat, lon = gps_ll
            dt2, zn, sk = naive_exif_with_gps_local_timezone(dt, lat, lon)
            if zn:
                gps_tz_used = zn
            elif sk:
                gps_tz_skip = sk
            dt = dt2
        return dt

    v9003 = ifd.get(0x9003)
    if isinstance(v9003, str):
        dt = _combine(v9003, off_o)
        if dt is not None:
            return ExifCaptureInfo(
                dt,
                offset_time_original=off_o,
                offset_time_digitized=off_d,
                has_gps=has_gps,
                gps_timezone_name=gps_tz_used,
                gps_tz_skip=gps_tz_skip,
            )

    v9004 = ifd.get(0x9004)
    if isinstance(v9004, str):
        dt = _combine(v9004, off_d or off_o or off_main)
        if dt is not None:
            return ExifCaptureInfo(
                dt,
                offset_time_original=off_o,
                offset_time_digitized=off_d,
                has_gps=has_gps,
                gps_timezone_name=gps_tz_used,
                gps_tz_skip=gps_tz_skip,
            )

    candidates: list[str] = []
    for tag_id in (0x9003, 0x9004):
        value = ifd.get(tag_id)
        if isinstance(value, str):
            candidates.append(value)
    for tag_id, value in ifd.items():
        name = TAGS.get(tag_id)
        if name in ("DateTimeOriginal", "DateTimeDigitized") and isinstance(value, str):
            if value not in candidates:
                candidates.append(value)

    dt_top = exif.get(0x0132)
    if isinstance(dt_top, str) and dt_top not in candidates:
        candidates.append(dt_top)
    for tag_id, value in exif.items():
        if TAGS.get(tag_id) == "DateTime" and isinstance(value, str) and value not in candidates:
            candidates.append(value)

    generic_off = off_o or off_d or off_main
    for raw in candidates:
        dt = _combine(raw, generic_off)
        if dt is not None:
            return ExifCaptureInfo(
                dt,
                offset_time_original=off_o,
                offset_time_digitized=off_d,
                has_gps=has_gps,
                gps_timezone_name=gps_tz_used,
                gps_tz_skip=gps_tz_skip,
            )
    return ExifCaptureInfo(None, has_gps=has_gps)


def try_exif_datetime_original(path: Path) -> datetime | None:
    """Return :attr:`ExifCaptureInfo.best_datetime` (backward-compatible name)."""
    return read_exif_capture(path).best_datetime


def _dedupe_wall_second_prefer_microsecond(
    raw: list[tuple[datetime, bool]],
) -> list[tuple[datetime, bool]]:
    """
    If several parses share the same calendar second, keep the one with the largest
    microsecond value (e.g. Android Screenshot_...-mmm over a coarser _RE_DATE_SEP match).
    """
    buckets: dict[tuple[int, int, int, int, int, int], tuple[datetime, bool]] = {}
    for dt, inc in raw:
        key = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        prev = buckets.get(key)
        if prev is None:
            buckets[key] = (dt, inc)
            continue
        prev_dt, prev_inc = prev
        if dt.microsecond > prev_dt.microsecond:
            buckets[key] = (dt, inc or prev_inc)
        elif dt.microsecond < prev_dt.microsecond:
            buckets[key] = (prev_dt, prev_inc or inc)
        else:
            buckets[key] = (dt, inc or prev_inc)
    return list(buckets.values())


def extract_filename_datetime_entries(
    name: str, now: datetime, cfg: HeuristicConfig
) -> list[tuple[datetime, bool]]:
    """
    Parse date(time) tokens from the filename stem.

    Each entry is (datetime, includes_clock_in_filename). The second flag is True only
    when the matched pattern included an explicit time-of-day (hour/minute/second), not
    when the match is date-only (implicit midnight).

    Same calendar day: if any parse includes a clock, bare YYYYMMDD midnights for that
    day are dropped so ``IMG_20100615_143022`` and ``shot20100615143022`` agree on
    2010-06-15 14:30:22 instead of min() picking midnight from the duplicate 8-digit match.
    """
    stem = Path(name).stem
    raw: list[tuple[datetime, bool]] = []

    def add_from_match_groups(
        y: str,
        m: str,
        d: str,
        H: str | None,
        Mi: str | None,
        S: str | None,
        includes_clock: bool,
    ) -> None:
        if H is None:
            dt = safe_datetime(int(y), int(m), int(d))
        else:
            dt = safe_datetime(
                int(y), int(m), int(d), int(H), int(Mi or 0), int(S or 0)
            )
        if dt and is_plausible_capture_date(dt, now, cfg):
            raw.append((dt, includes_clock))

    for m in _RE_APPLE_IMG.finditer(stem):
        add_from_match_groups(
            m.group("y"),
            m.group("m"),
            m.group("d"),
            m.group("H"),
            m.group("M"),
            m.group("S"),
            True,
        )

    for m in _RE_SCREENSHOT_ANDROID.finditer(stem):
        ms = int(m.group("ms"))
        if ms > 999:
            continue
        dt = safe_datetime(
            int(m.group("y")),
            int(m.group("m")),
            int(m.group("d")),
            int(m.group("H")),
            int(m.group("Mi")),
            int(m.group("S")),
            microsecond=ms * 1000,
        )
        if dt and is_plausible_capture_date(dt, now, cfg):
            raw.append((dt, True))

    for m in _RE_PXL.finditer(stem):
        hms = m.group("hms")
        add_from_match_groups(
            m.group("y"),
            m.group("m"),
            m.group("d"),
            hms[0:2],
            hms[2:4],
            hms[4:6],
            True,
        )

    for m in _RE_WA.finditer(stem):
        add_from_match_groups(
            m.group("y"), m.group("m"), m.group("d"), None, None, None, False
        )

    for m in _RE_DATETIME_COMPACT.finditer(stem):
        add_from_match_groups(
            m.group("y"),
            m.group("m"),
            m.group("d"),
            m.group("H"),
            m.group("M"),
            m.group("S"),
            True,
        )

    for m in _RE_DATE_SEP.finditer(stem):
        H = m.group("H")
        if H is None:
            add_from_match_groups(
                m.group("y"), m.group("m"), m.group("d"), None, None, None, False
            )
        else:
            add_from_match_groups(
                m.group("y"),
                m.group("m"),
                m.group("d"),
                H,
                m.group("M"),
                m.group("S"),
                True,
            )

    for m in _RE_DATE_SEP_AT_DOT_TIME.finditer(stem):
        y, mo, d = m.group("y"), m.group("m"), m.group("d")
        h = m.group("h")
        if h is None:
            continue
        mi = m.group("mi")
        se = m.group("se") or "00"
        ampm = (m.group("ampm") or "").upper()
        hh = int(h)
        if ampm in ("AM", "PM"):
            if hh == 12:
                hh = 0
            if ampm == "PM":
                hh += 12
        add_from_match_groups(y, mo, d, f"{hh:02d}", mi, se, True)

    for m in _RE_DATE_COMPACT8.finditer(stem):
        add_from_match_groups(
            m.group("y"), m.group("m"), m.group("d"), None, None, None, False
        )

    raw = _dedupe_wall_second_prefer_microsecond(raw)

    merged: dict[datetime, bool] = {}
    for dt, inc in raw:
        merged[dt] = merged.get(dt, False) or inc
    items = [(dt, merged[dt]) for dt in sorted(merged.keys())]
    items = _filter_drop_date_only_when_clock_same_calendar_day(items)
    return sorted(items, key=lambda x: x[0])


def _filter_drop_date_only_when_clock_same_calendar_day(
    items: list[tuple[datetime, bool]],
) -> list[tuple[datetime, bool]]:
    """
    If a calendar day has any clock-inclusive parse, drop same-day date-only
    midnights (00:00:00). That removes the duplicate reading of `20100615` as
    YYYYMMDD when `IMG_20100615_143022` or `20100615143022` already supplies the time.
    """
    by_day: dict[tuple[int, int, int], list[tuple[datetime, bool]]] = defaultdict(list)
    for dt, inc in items:
        by_day[(dt.year, dt.month, dt.day)].append((dt, inc))

    out: list[tuple[datetime, bool]] = []
    for group in by_day.values():
        has_clock = any(inc for _, inc in group)
        for dt, inc in group:
            if (
                has_clock
                and not inc
                and dt.hour == 0
                and dt.minute == 0
                and dt.second == 0
            ):
                continue
            out.append((dt, inc))
    return out


def extract_datetimes_from_filename(
    name: str, now: datetime, cfg: HeuristicConfig
) -> list[datetime]:
    """Collect plausible date(time) values embedded in the filename stem (date-only and datetime)."""
    return [d for d, _ in extract_filename_datetime_entries(name, now, cfg)]


def _is_reasonable_year(y: int) -> bool:
    return 1980 <= y <= 2100


def _last_day_of_month(y: int, m: int) -> int:
    if m == 12:
        next_month = date(y + 1, 1, 1)
    else:
        next_month = date(y, m + 1, 1)
    return (next_month - date(y, m, 1)).days


_RE_FOLDER_DD_MON_YYYY = re.compile(
    r"^(?P<day>\d{1,2})-(?P<mon>[A-Za-z]{3})-(?P<py>\d{4})$"
)


def _parse_non_us_date_folder_segment(segment: str) -> tuple[int, int, int] | None:
    """
    Parse one folder name as a single calendar date.

    Accepted (international / ISO; **never** US month-day-year for numeric dates):
    - ISO ``YYYY-MM-DD``
    - European **day-first** ``D-M-YYYY``, ``DD-MM-YYYY`` with ``-``, ``.``, or ``/``
    - ``DD-MMM-YYYY`` and ``DD-MMMM-YYYY`` (English month names), optional spaces

    If numeric ``D-M-YYYY`` would only be valid as US order (e.g. month 15), it is rejected.
    """
    s = segment.strip()
    if not s:
        return None

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if safe_datetime(yy, mm, dd):
            return yy, mm, dd
        return None

    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not _is_reasonable_year(year):
            return None
        if not (1 <= month <= 12):
            return None
        if not (1 <= day <= _last_day_of_month(year, month)):
            return None
        return year, month, day

    mfm = _RE_FOLDER_DD_MON_YYYY.match(s)
    if mfm:
        d0 = int(mfm.group("day"))
        py = int(mfm.group("py"))
        mon_abbrev = mfm.group("mon").title()
        try:
            parsed = datetime.strptime(f"{d0}-{mon_abbrev}-{py}", "%d-%b-%Y")
            return parsed.year, parsed.month, parsed.day
        except ValueError:
            pass

    m = re.fullmatch(
        r"(\d{1,2})[-\s_]+([A-Za-z]{3,12})[-\s_]+(\d{4})",
        s,
    )
    if m:
        d0 = int(m.group(1))
        mon_word = m.group(2).title()
        y0 = int(m.group(3))
        if not _is_reasonable_year(y0):
            return None
        for text, pat in (
            (f"{d0}-{mon_word}-{y0}", "%d-%b-%Y"),
            (f"{d0}-{mon_word}-{y0}", "%d-%B-%Y"),
            (f"{d0} {mon_word} {y0}", "%d %b %Y"),
            (f"{d0} {mon_word} {y0}", "%d %B %Y"),
        ):
            try:
                parsed = datetime.strptime(text, pat)
                return parsed.year, parsed.month, parsed.day
            except ValueError:
                continue

    return None


def _parse_year_month_prefix_folder(segment: str) -> tuple[int, int] | None:
    """
    Year-first month in one folder name: ``1994-Aug``, ``1994-August``, ``1994-08``.
    """
    s = segment.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})$", s)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
        if _is_reasonable_year(yy) and 1 <= mm <= 12:
            return yy, mm
        return None
    m = re.fullmatch(r"(\d{4})-([A-Za-z]{3,12})$", s)
    if m:
        yy = int(m.group(1))
        mon = m.group(2).title()
        if not _is_reasonable_year(yy):
            return None
        for text, pat in (
            (f"1 {mon} {yy}", "%d %b %Y"),
            (f"1 {mon} {yy}", "%d %B %Y"),
        ):
            try:
                dt = datetime.strptime(text, pat)
                return dt.year, dt.month
            except ValueError:
                continue
    return None


def parse_path_calendar(
    parts: list[str],
) -> tuple[int | None, int | None, int | None]:
    """
    From leading path segments, read year / month / day.

    First segment may be ``YYYY`` only, or ``YYYY-Mmm`` / ``YYYY-MM`` (year before month).
    Further segments may be numeric month only, day-only, or a full date string
    (see :func:`_parse_non_us_date_folder_segment`).
    """
    y = mo = da = None
    if not parts:
        return None, None, None
    p0 = parts[0]

    if re.fullmatch(r"\d{4}", p0) and _is_reasonable_year(int(p0)):
        y = int(p0)
        if len(parts) < 2:
            return y, None, None

        p1 = parts[1]
        if re.fullmatch(r"\d{1,2}", p1):
            mm = int(p1)
            if 1 <= mm <= 12:
                mo = mm
        else:
            got = _parse_non_us_date_folder_segment(p1)
            if got is not None:
                gy, gmo, gda = got
                if gy == y:
                    mo, da = gmo, gda

        if mo is not None and da is None and len(parts) >= 3:
            if re.fullmatch(r"\d{1,2}", parts[2]):
                dd = int(parts[2])
                if 1 <= dd <= 31:
                    da = dd
        return y, mo, da

    ym = _parse_year_month_prefix_folder(p0)
    if ym is not None:
        y, mo = ym
        da = None
        if len(parts) >= 2 and re.fullmatch(r"\d{1,2}", parts[1]):
            dd = int(parts[1])
            if 1 <= dd <= _last_day_of_month(y, mo):
                da = dd
        return y, mo, da

    return None, None, None


_RE_SLUG_TS = re.compile(r"^(\d{8})-(\d{6})$")


def match_structured_path(parts: list[str]) -> datetime | None:
    """
    source/<yyyy>/<mm>/<DD>/<yyyymmDD>-<HHMMss>/file
    Returns the datetime encoded in the folder name if the shape matches.
    """
    if len(parts) < 4:
        return None
    y_s, m_s, d_s, slug = parts[0], parts[1], parts[2], parts[3]
    if not (
        re.fullmatch(r"\d{4}", y_s)
        and re.fullmatch(r"\d{2}", m_s)
        and re.fullmatch(r"\d{2}", d_s)
    ):
        return None
    sm = _RE_SLUG_TS.match(slug)
    if not sm:
        return None
    ymd, hms = sm.group(1), sm.group(2)
    try:
        dt = datetime(
            int(ymd[0:4]),
            int(ymd[4:6]),
            int(ymd[6:8]),
            int(hms[0:2]),
            int(hms[2:4]),
            int(hms[4:6]),
        )
    except ValueError:
        return None
    if f"{dt.year:04d}" != y_s or f"{dt.month:02d}" != m_s or f"{dt.day:02d}" != d_s:
        return None
    return dt


def path_has_calendar_hint(parts: list[str]) -> bool:
    return path_implies_date_period(parts) is not None


def path_implies_date_period(
    parts: list[str],
) -> tuple[datetime, datetime] | None:
    """
    Return (start, end) for the period implied by dated folders.

    - YYYY => full year
    - YYYY/MM or YYYY-Mmm => full month
    - YYYY/MM/DD or full-date folder => full day
    """
    y, mo, da = parse_path_calendar(parts)
    if y is None:
        return None
    if mo is None:
        start = datetime(y, 1, 1, 0, 0, 0)
        end = datetime(y, 12, 31, 23, 59, 59)
        return start, end
    if da is None:
        last = _last_day_of_month(y, mo)
        start = datetime(y, mo, 1, 0, 0, 0)
        end = datetime(y, mo, last, 23, 59, 59)
        return start, end
    if not safe_datetime(y, mo, da):
        return None
    start = datetime(y, mo, da, 0, 0, 0)
    end = datetime(y, mo, da, 23, 59, 59)
    return start, end


def path_calendar_divergence_hint(
    rel_parts: list[str],
    resolved: datetime,
    now: datetime,
    cfg: HeuristicConfig,
) -> str | None:
    """
    If directory segments imply a calendar period and the resolved capture time falls
    outside that period, return a short note for post-run logging only (no effect on rules
    or dedupe scoring).
    """
    period = path_implies_date_period(rel_parts)
    if period is None:
        return None
    start, end = period
    if not is_plausible_capture_date(end, now, cfg):
        return None
    # Compare folder-implied periods to capture civil date (strip zone; do not map to host).
    r = resolved.replace(tzinfo=None) if resolved.tzinfo else resolved
    if start <= r <= end:
        return None
    return (
        f"path folders {start.date()}..{end.date()} vs resolved "
        f"{r.isoformat(sep=' ', timespec='seconds')}"
    )


def rel_parts_for_path_dating(file_path: Path, source: Path) -> list[str]:
    """
    Directory segments (excluding filename) for path calendar / hierarchy rules.

    Uses ``relative_to(source)`` when that path already has a calendar hint (normal tree layout).

    If not (e.g. file is directly under a dated leaf ``--source``), uses
    ``relative_to(source.parent)`` when that path yields a calendar hint — without prepending
    an extra root folder name in the common case where subfolders under a tree ``--source``
    already start with ``YYYY`` / ``YYYY-Mmm``.
    """
    rel_src = file_path.relative_to(source)
    parts_src = list(rel_src.parts[:-1])
    if path_has_calendar_hint(parts_src):
        return parts_src
    parent = source.parent
    if parent != source:
        try:
            rel_p = file_path.relative_to(parent)
            parts_p = list(rel_p.parts[:-1])
        except ValueError:
            return parts_src
        if path_has_calendar_hint(parts_p):
            return parts_p
    return parts_src
