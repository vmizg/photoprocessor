"""Slot-key normalization and state key migration (Windows case/Unicode)."""

from __future__ import annotations

import sys
import unicodedata
from typing import Any

def normalize_slot_key_for_dedupe(slot_key: str) -> str:
    """
    Canonical duplicate-registry key ``<year>/<filename>``.

    On Windows, NFC + casefold the filename segment so the registry matches
    case-insensitive paths. Older ``organize_state.json`` files may use mixed-case
    keys; :func:`migrate_state_slot_keys` rewrites them on load so lookups still hit.
    """
    norm = slot_key.strip().replace("\\", "/")
    if sys.platform != "win32":
        return norm
    if "/" not in norm:
        return norm
    year, fname = norm.split("/", 1)
    if not fname:
        return norm
    fname = unicodedata.normalize("NFC", fname).casefold()
    return f"{year}/{fname}"


def slot_key_for(move_year: int, filename: str) -> str:
    return normalize_slot_key_for_dedupe(f"{move_year}/{filename}")


def migrate_state_slot_keys(state: dict[str, Any]) -> None:
    """Rewrite ``state[\"slots\"]`` keys to :func:`normalize_slot_key_for_dedupe` form (Windows)."""
    if sys.platform != "win32":
        return
    slots = state.get("slots")
    if not isinstance(slots, dict) or not slots:
        return
    out: dict[str, Any] = {}
    for k, v in slots.items():
        nk = normalize_slot_key_for_dedupe(k)
        if nk not in out:
            if isinstance(v, dict):
                w = v.get("winner")
                if isinstance(w, dict):
                    w["target_relative"] = nk
            out[nk] = v
            continue
        a, b = out[nk], v
        wa, wb = a.get("winner") or {}, b.get("winner") or {}
        sa, sb = int(wa.get("score", 0)), int(wb.get("score", 0))
        qa, qb = int(wa.get("sequence", 10**9)), int(wb.get("sequence", 10**9))
        pick_a = sa > sb or (sa == sb and qa <= qb)
        chosen, other = (a, b) if pick_a else (b, a)
        merged = {
            "winner": chosen.get("winner"),
            "history": list(chosen.get("history", [])) + list(other.get("history", [])),
        }
        wc = merged.get("winner")
        if isinstance(wc, dict):
            wc["target_relative"] = nk
        out[nk] = merged
    state["slots"] = out
