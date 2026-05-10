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


def slot_target_display(move_year: int, filename: str) -> str:
    """
    Relative dest path for logs and JSON (``YYYY/originalBasename``).

    Slot *keys* in state may be casefolded on Windows; this keeps human-facing paths
    and ``target_relative`` aligned with on-disk names from the source file.
    """
    return f"{move_year}/{filename}".replace("\\", "/")


def _display_target_from_winner(w: dict[str, Any], canonical_slot: str) -> str:
    """Prefer ``year/originalFilename`` from winner dict; else ``canonical_slot``."""
    fn = w.get("filename")
    if isinstance(fn, str) and fn and "/" not in fn and "/" in canonical_slot:
        year, _ = canonical_slot.split("/", 1)
        return f"{year}/{fn}"
    return canonical_slot


def _merge_slot_values_by_score(a: dict[str, Any], b: dict[str, Any], nk: str) -> dict[str, Any]:
    wa, wb = a.get("winner") or {}, b.get("winner") or {}
    sa, sb = int(wa.get("score", 0)), int(wb.get("score", 0))
    qa, qb = int(wa.get("sequence", 10**9)), int(wb.get("sequence", 10**9))
    pick_a = sa > sb or (sa == sb and qa <= qb)
    chosen, other = (a, b) if pick_a else (b, a)
    merged: dict[str, Any] = {
        "winner": chosen.get("winner"),
        "history": list(chosen.get("history", [])) + list(other.get("history", [])),
        "canonical_slot": nk,
    }
    wc = merged.get("winner")
    if isinstance(wc, dict):
        wc["target_relative"] = _display_target_from_winner(wc, nk)
    return merged


def expand_slots_to_canonical(slots: dict[str, Any]) -> dict[str, Any]:
    """
    After loading JSON, re-key ``slots`` to canonical :func:`normalize_slot_key_for_dedupe` keys.

    Disk format may use display-cased keys (``target_relative``) plus ``canonical_slot`` on each
    value; legacy files use canonical keys only.
    """
    out: dict[str, Any] = {}
    for k, v in slots.items():
        if not isinstance(v, dict):
            continue
        raw = k.strip().replace("\\", "/")
        canon_src = v.get("canonical_slot")
        if isinstance(canon_src, str) and canon_src.strip():
            nk = normalize_slot_key_for_dedupe(canon_src.strip().replace("\\", "/"))
        else:
            nk = normalize_slot_key_for_dedupe(raw)
        vv = dict(v)
        vv["canonical_slot"] = nk
        if nk not in out:
            out[nk] = vv
        else:
            out[nk] = _merge_slot_values_by_score(out[nk], vv, nk)
    return out


def slots_for_json_export(slots: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize ``slots`` with outer keys matching ``winner.target_relative`` (original basename
    casing). Each value includes ``canonical_slot`` for load round-trip.
    """
    out: dict[str, Any] = {}
    used: set[str] = set()
    for sk, v in slots.items():
        if not isinstance(v, dict):
            continue
        w = v.get("winner")
        if isinstance(w, dict):
            tr = w.get("target_relative")
            if isinstance(tr, str) and tr.strip():
                disp = tr.strip().replace("\\", "/")
            else:
                disp = _display_target_from_winner(w, sk)
        else:
            disp = sk
        key = disp
        if key in used:
            key = sk
        used.add(key)
        vv = dict(v)
        vv["canonical_slot"] = v.get("canonical_slot") or sk
        out[key] = vv
    return out


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
                    w["target_relative"] = _display_target_from_winner(w, nk)
                v["canonical_slot"] = nk
            out[nk] = v
            continue
        merged = _merge_slot_values_by_score(out[nk], v, nk)
        out[nk] = merged
    state["slots"] = out
