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


def save_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON with atomic replace (same Windows retry behavior as organize_state.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
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


def save_state_atomic(path: Path, state: dict[str, Any]) -> None:
    _t0 = time.perf_counter()
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json_atomic(path, state)
    _elapsed = time.perf_counter() - _t0
    if _elapsed > 3.0:
        logging.getLogger("organize_photos").warning(
            "Slow state save: %.2fs to %s (large organize_state.json or slow disk)",
            _elapsed,
            path,
        )


def dt_iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return d.isoformat(sep=" ", timespec="seconds")
