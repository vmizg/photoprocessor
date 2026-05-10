"""
Infer IANA or fixed-offset timezone for naive EXIF from bracketing folder neighbors.

Only used when GPS did not supply a zone (same situation as ``--fallback-timezone``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .datetime_policy import apply_neighbor_inferred_tz, is_plausible_capture_date
from .models import ExifCaptureInfo, HeuristicConfig, NeighborInferredTz

# Reject anchor pairs closer than this in UTC (bursts / duplicate seconds).
_MIN_ANCHOR_UTC_SEPARATION = timedelta(seconds=1)


@dataclass(frozen=True)
class _FolderEntry:
    rel_posix: str
    fname: str
    fname_key: str
    cap: ExifCaptureInfo


def _zone_bucket(dt: datetime) -> tuple[str, str | int] | None:
    """Comparable timezone identity for two anchors (IANA key or fixed offset seconds)."""
    if dt.tzinfo is None:
        return None
    iana_key = getattr(dt.tzinfo, "key", None)
    if iana_key:
        return ("iana", iana_key)
    off = dt.tzinfo.utcoffset(dt)
    if off is None:
        return None
    return ("fixed", int(off.total_seconds()))


def _spec_from_bucket(bucket: tuple[str, str | int]) -> NeighborInferredTz | None:
    kind, val = bucket
    if kind == "iana":
        if not isinstance(val, str) or not val:
            return None
        return NeighborInferredTz(iana=val)
    if kind == "fixed":
        if not isinstance(val, int):
            return None
        return NeighborInferredTz(fixed_offset_total_seconds=val)
    return None


def _eligible_anchor(cap: ExifCaptureInfo, now: datetime, cfg: HeuristicConfig) -> bool:
    dt = cap.best_datetime
    if dt is None or dt.tzinfo is None:
        return False
    return is_plausible_capture_date(dt, now, cfg)


def _eligible_target(cap: ExifCaptureInfo, now: datetime, cfg: HeuristicConfig) -> bool:
    """Naive plausible EXIF and no GPS-derived zone (would otherwise use fallback TZ)."""
    dt = cap.best_datetime
    if dt is None or dt.tzinfo is not None:
        return False
    if (cap.gps_timezone_name or "").strip():
        return False
    return is_plausible_capture_date(dt, now, cfg)


def _strict_alpha_between(before: str, mid: str, after: str) -> bool:
    kb, km, ka = before.casefold(), mid.casefold(), after.casefold()
    return kb < km < ka


def _infer_for_sorted_folder(
    entries: list[_FolderEntry], now: datetime, cfg: HeuristicConfig
) -> dict[str, NeighborInferredTz]:
    out: dict[str, NeighborInferredTz] = {}
    n = len(entries)
    if n < 3:
        return out

    anchor_idx = [i for i, e in enumerate(entries) if _eligible_anchor(e.cap, now, cfg)]
    for i, e in enumerate(entries):
        if not _eligible_target(e.cap, now, cfg):
            continue
        j = max((x for x in anchor_idx if x < i), default=None)
        k = min((x for x in anchor_idx if x > i), default=None)
        if j is None or k is None:
            continue
        ej, ek = entries[j], entries[k]
        if not _strict_alpha_between(ej.fname, e.fname, ek.fname):
            continue
        dt_j = ej.cap.best_datetime
        dt_k = ek.cap.best_datetime
        if dt_j is None or dt_k is None:
            continue
        bj, bk = _zone_bucket(dt_j), _zone_bucket(dt_k)
        if bj is None or bj != bk:
            continue
        t_lo = dt_j.timestamp()
        t_hi = dt_k.timestamp()
        if t_hi - t_lo <= _MIN_ANCHOR_UTC_SEPARATION.total_seconds():
            continue
        spec = _spec_from_bucket(bj)
        if spec is None:
            continue
        dt_mid = e.cap.best_datetime
        if dt_mid is None:
            continue
        aware_mid = apply_neighbor_inferred_tz(dt_mid, spec)
        if aware_mid is None or aware_mid.tzinfo is None:
            continue
        t_mid = aware_mid.timestamp()
        if not (t_lo < t_mid < t_hi):
            continue
        out[e.rel_posix] = spec
    return out


def build_neighbor_tz_map(
    paths: list[Path],
    source: Path,
    exif_by_path: dict[Path, ExifCaptureInfo],
    now: datetime,
    cfg: HeuristicConfig,
) -> dict[str, NeighborInferredTz]:
    """
    For each relative path under ``source``, return a :class:`NeighborInferredTz` when the
    heuristic matches; omit keys when inference does not apply.
    """
    by_parent: dict[str, list[_FolderEntry]] = defaultdict(list)
    for p in paths:
        try:
            rel = p.relative_to(source)
        except ValueError:
            continue
        cap = exif_by_path.get(p)
        if cap is None:
            continue
        parent = rel.parent.as_posix()
        fname = rel.name
        by_parent[parent].append(
            _FolderEntry(
                rel_posix=rel.as_posix(),
                fname=fname,
                fname_key=fname.casefold(),
                cap=cap,
            )
        )

    out: dict[str, NeighborInferredTz] = {}
    for _parent, rows in by_parent.items():
        rows.sort(key=lambda r: (r.fname_key, r.fname))
        folder_hits = _infer_for_sorted_folder(rows, now, cfg)
        out.update(folder_hits)
    return out
