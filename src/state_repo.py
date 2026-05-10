"""Atomic load/save for organize_state.json."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import STATE_VERSION


def iso_timezone_label(dt: datetime | None) -> str | None:
    """
    IANA name for :class:`zoneinfo.ZoneInfo`, else a ``UTC±HH:MM`` style label; ``None`` if naive.
    """
    if dt is None or dt.tzinfo is None:
        return None
    key = getattr(dt.tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    try:
        off = dt.tzinfo.utcoffset(dt)
    except Exception:
        return None
    if off is None:
        return None
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    if ss:
        return f"UTC{sign}{hh:02d}:{mm:02d}:{ss:02d}"
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def default_state(source: Path, dest: Path) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(source),
        "dest": str(dest),
        "sequence": 0,
        "slots": {},
        "skipped": [],
    }


def load_state_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def save_json_atomic(path: Path, obj: Any, *, compact: bool = False) -> None:
    """Write JSON with atomic replace (same Windows retry behavior as organize_state.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    last_err: Exception | None = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            if not tmp.exists():
                break
            time.sleep(0.05 * (2**i))
    raise PermissionError(
        f"Could not atomically replace JSON file {path} (is it open/locked by another process?)."
    ) from last_err


def save_state_atomic(path: Path, state: dict[str, Any], *, compact: bool = True) -> None:
    from .dedupe import slots_for_json_export

    _t0 = time.perf_counter()
    to_save = dict(state)
    to_save["updated_at"] = datetime.now().isoformat(timespec="seconds")
    slots = state.get("slots")
    if isinstance(slots, dict):
        to_save["slots"] = slots_for_json_export(slots)
    save_json_atomic(path, to_save, compact=compact)
    _elapsed = time.perf_counter() - _t0
    if _elapsed > 3.0:
        logging.getLogger("organize_photos").warning(
            "Slow state save: %.2fs to %s (large organize_state.json or slow disk)",
            _elapsed,
            path,
        )


class StateFlushBatcher:
    """
    Batch disk writes of organize_state.json: each mutation is cheap in memory, but
    serializing the whole state after every file is O(n²) over a long run.

    Call :meth:`after_mutation` after each in-memory state change; a flush runs every
    ``every`` mutations. Always call :meth:`end` after the main loop so the last batch
    is persisted.
    """

    __slots__ = ("path", "every", "_pending")

    def __init__(self, path: Path | None, every: int) -> None:
        self.path = path
        self.every = max(1, every)
        self._pending = 0

    def after_mutation(self, state: dict[str, Any]) -> None:
        if self.path is None:
            return
        self._pending += 1
        if self._pending >= self.every:
            save_state_atomic(self.path, state)
            self._pending = 0

    def end(self, state: dict[str, Any]) -> None:
        if self.path is None:
            return
        if self._pending > 0:
            save_state_atomic(self.path, state)
            self._pending = 0


def dt_iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return d.isoformat(sep=" ", timespec="seconds")
