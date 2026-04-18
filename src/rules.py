"""Decision engine: map file facts + heuristics to actions and confidence scores."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .datetime_policy import (
    TZ_NAIVE_VS_MODIFIED_EQUIV_MAX,
    best_capture_time_for_folder_vs_metadata,
    exif_ambiguous_vs_modified,
    filename_ambiguous_vs_modified,
    has_two_correlated_file_indicators,
    is_plausible_capture_date,
    naive_local,
)
from .extractors import (
    _RE_APPLE_IMG,
    _RE_PXL,
    _RE_SCREENSHOT_ANDROID,
    _RE_WA,
    extract_filename_datetime_entries,
    is_apple_library_structured_path,
    match_structured_path,
    path_has_calendar_hint,
    path_implies_date_period,
)
from .models import Action, Decision, FileFacts, HeuristicConfig


def decide_actions(
    rel_parts: list[str],
    filename: str,
    created: datetime,
    modified: datetime,
    exif_original: datetime | None,
    now: datetime,
    cfg: HeuristicConfig,
) -> list[Action]:
    actions: list[Action] = []
    if exif_original is not None:
        exif_original = naive_local(exif_original)
    earliest = min(created, modified)
    path_hint_suppressed_by_file_time = False
    filename_hint_suppressed_by_mtime = False
    exif_hint_suppressed_by_mtime = False

    fn_entries = [
        (naive_local(d), inc) for d, inc in extract_filename_datetime_entries(filename, now, cfg)
    ]
    fn_dates = [d for d, _ in fn_entries]

    if not is_apple_library_structured_path(rel_parts):
        period = path_implies_date_period(rel_parts)
        if period is not None:
            start, end = period
            if is_plausible_capture_date(end, now, cfg):
                if has_two_correlated_file_indicators(
                    fn_entries=fn_entries,
                    exif_original=exif_original,
                    modified=modified,
                    now=now,
                    cfg=cfg,
                ):
                    path_hint_suppressed_by_file_time = True
                else:
                    best_file_time = best_capture_time_for_folder_vs_metadata(
                        fn_entries=fn_entries,
                        exif_original=exif_original,
                        modified=modified,
                        now=now,
                        cfg=cfg,
                    )
                    if best_file_time > end:
                        actions.append(
                            Action(
                                "path_hierarchy_trusted",
                                f"path folders imply {end.isoformat(sep=' ')} (overrides EXIF for manual layout)",
                                new_created=end,
                            )
                        )
                        return actions
                    path_hint_suppressed_by_file_time = True

    exif_ok = (
        exif_original is not None
        and is_plausible_capture_date(exif_original, now, cfg)
        and exif_original < earliest
        and not exif_ambiguous_vs_modified(exif_original, modified)
    )
    if (
        exif_original is not None
        and is_plausible_capture_date(exif_original, now, cfg)
        and exif_ambiguous_vs_modified(exif_original, modified)
    ):
        exif_hint_suppressed_by_mtime = True

    if exif_ok:
        actions.append(
            Action(
                "exif_earlier_than_metadata",
                f"EXIF original {exif_original.isoformat(sep=' ')} < earliest meta {earliest}",
                new_created=exif_original,
            )
        )
        return actions

    qualifying_entries = [(d, inc) for d, inc in fn_entries if d < earliest]
    if any(inc and filename_ambiguous_vs_modified(d, modified) for d, inc in fn_entries):
        filename_hint_suppressed_by_mtime = True
    qualifying_entries = [
        (d, inc)
        for d, inc in qualifying_entries
        if not (inc and filename_ambiguous_vs_modified(d, modified))
    ]
    if qualifying_entries:
        target, includes_clock = min(qualifying_entries, key=lambda x: x[0])
        # Filename tokens are naive local-at-capture; min(created,modified) is this copy's
        # filesystem time. Large calendar gaps (re-downloads) are not same-day clock comparison.
        gap_days = (earliest - target).days
        extra = ""
        if gap_days > 120 or earliest.year != target.year:
            extra = (
                f" [calendar gap {gap_days}d: comparison uses full date+time, not same-day "
                f"05:13 vs 11:13 only; stem is camera-local; file times are when this copy was saved]"
            )
        detail = (
            f"parsed filename {target.isoformat(sep=' ')} < min(created,modified) "
            f"{earliest.isoformat(sep=' ')} (filename is naive local-at-capture).{extra}"
        )
        actions.append(
            Action(
                "filename_earlier_than_metadata",
                detail,
                new_created=target,
                filename_includes_time_in_name=includes_clock,
            )
        )
        return actions

    if modified < created and (
        exif_hint_suppressed_by_mtime or filename_hint_suppressed_by_mtime
    ):
        actions.append(
            Action(
                "fallback_created_from_modified",
                f"capture indicators near mtime; modified {modified} < created {created}",
                new_created=modified,
            )
        )
        return actions

    if modified < created and is_plausible_capture_date(modified, now, cfg):
        corroborates_mtime = False
        if exif_original is not None and is_plausible_capture_date(exif_original, now, cfg):
            corroborates_mtime = abs(exif_original - modified) <= TZ_NAIVE_VS_MODIFIED_EQUIV_MAX
        if not corroborates_mtime:
            for d, _inc in fn_entries:
                if is_plausible_capture_date(d, now, cfg) and abs(d - modified) <= TZ_NAIVE_VS_MODIFIED_EQUIV_MAX:
                    corroborates_mtime = True
                    break
        if corroborates_mtime:
            actions.append(
                Action(
                    "fallback_created_from_modified",
                    f"capture indicators match mtime; modified {modified} < created {created}",
                    new_created=modified,
                )
            )
            return actions

    slug_dt = match_structured_path(rel_parts)

    if slug_dt is not None:
        delta = abs((modified - slug_dt).total_seconds())
        if delta < 1.0:
            if created > modified:
                actions.append(
                    Action(
                        "structured_path_align_created",
                        f"created {created} > modified {modified}; set created = modified",
                        new_created=modified,
                    )
                )
                return actions
        else:
            actions.append(
                Action(
                    "structured_path_mismatch",
                    f"path slug {slug_dt.isoformat(sep=' ')} vs mtime {modified} "
                    f"(abs delta={delta:.1f}s) - not applying rule 2",
                )
            )
            oldest = min(slug_dt, created, modified)
            actions.append(
                Action(
                    "structured_path_oldest_signal",
                    f"path slug vs mtime conflict; oldest of slug / created / modified -> "
                    f"{oldest.isoformat(sep=' ')}",
                    new_created=oldest,
                )
            )
            return actions

    if (not fn_dates or filename_hint_suppressed_by_mtime or exif_hint_suppressed_by_mtime) and (
        not path_has_calendar_hint(rel_parts) or path_hint_suppressed_by_file_time
    ):
        if modified < created:
            actions.append(
                Action(
                    "fallback_created_from_modified",
                    f"no filename/path date; modified {modified} < created {created}",
                    new_created=modified,
                )
            )
        return actions

    return actions


def decide(facts: FileFacts, now: datetime, cfg: HeuristicConfig) -> Decision:
    """Pure decision stage: ``collect facts → decide``."""
    planned = decide_actions(
        list(facts.rel_parts),
        facts.filename,
        facts.created,
        facts.modified,
        facts.exif_original,
        now,
        cfg,
    )
    return Decision.from_actions(planned)


def filename_has_anchor(filename: str) -> bool:
    stem = Path(filename).stem
    return bool(
        _RE_APPLE_IMG.search(stem)
        or _RE_PXL.search(stem)
        or _RE_WA.search(stem)
        or _RE_SCREENSHOT_ANDROID.search(stem)
    )


def primary_time_action(planned: list[Action]) -> Action | None:
    """Last action that sets CreationTime (normal rules emit at most one such action)."""
    last: Action | None = None
    for a in planned:
        if a.new_created is not None:
            last = a
    return last


def confidence_score(planned: list[Action], filename: str) -> tuple[int, str]:
    """
    Return (score, rule_key) for how trustworthy the resolved date is.
    Higher = more confident. Tie-break: lower sequence wins (first processed).

    Path hierarchy (dated folders, not Apple library slug layout) scores above EXIF.
    Filename rule: a *clock in the filename* (explicit H:M:S from regex) scores higher than
    date-only tokens. Anchored camera patterns (IMG_/PXL_) add a small boost on top.
    """
    primary = primary_time_action(planned)
    if primary is None:
        return 30, "move_only_no_time_change"

    kind = primary.kind
    if kind == "path_hierarchy_trusted":
        return 105, kind
    if kind == "exif_earlier_than_metadata":
        return 100, kind
    if kind == "structured_path_align_created":
        return 90, kind
    if kind == "structured_path_oldest_signal":
        return 55, kind
    if kind == "filename_earlier_than_metadata":
        inc = primary.filename_includes_time_in_name or False
        anchored = filename_has_anchor(filename)
        if inc:
            if anchored:
                return 94, f"{kind}_anchor_with_clock"
            return 92, f"{kind}_with_clock"
        if anchored:
            return 85, f"{kind}_anchored_date_only"
        return 75, f"{kind}_date_only"
    if kind == "path_calendar_recovery":
        return 60, kind
    if kind == "fallback_created_from_modified":
        return 40, kind
    return 30, kind
