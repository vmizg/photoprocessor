"""Decision engine: map file facts + heuristics to actions and confidence scores."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .datetime_policy import (
    TZ_NAIVE_VS_MODIFIED_EQUIV_MAX,
    apply_neighbor_inferred_tz,
    attach_gps_iana_zone_to_naive_exif,
    exif_ambiguous_vs_modified,
    exif_correlates_with_clock_filename,
    exif_filename_clock_preference,
    filename_ambiguous_vs_modified,
    is_plausible_capture_date,
    naive_local,
    should_exclude_filename_precedence_candidate,
)
from .extractors import (
    _RE_APPLE_IMG,
    _RE_PXL,
    _RE_SCREENSHOT_ANDROID,
    _RE_WA,
    extract_filename_datetime_entries,
    match_structured_path,
)
from .models import Action, Decision, FileFacts, HeuristicConfig, NeighborInferredTz


def decide_actions(
    rel_parts: list[str],
    filename: str,
    created: datetime,
    modified: datetime,
    exif_original: datetime | None,
    now: datetime,
    cfg: HeuristicConfig,
    *,
    exif_gps_timezone_name: str | None = None,
    path_anchor_year: int | None = None,
    neighbor_inferred_tz: NeighborInferredTz | None = None,
) -> list[Action]:
    actions: list[Action] = []
    # Host-local normalization for filesystem / plausibility; stem correlation uses raw EXIF
    # (see exif_civil_clock_for_stem_compare) so aware + offset is not mapped to OS timezone.
    exif_as_read = exif_original
    if exif_original is not None:
        exif_original = naive_local(exif_original)
    earliest = min(created, modified)
    filename_hint_suppressed_by_mtime = False
    exif_hint_suppressed_by_mtime = False

    # Embedded capture authoritative when plausible: (1) explicit offset / Z on metadata, or
    # (2) naive EXIF/video clock plus GPS-derived IANA zone from capture location.
    if exif_as_read is not None and is_plausible_capture_date(
        naive_local(exif_as_read), now, cfg
    ):
        if exif_as_read.tzinfo is not None:
            actions.append(
                Action(
                    "embedded_timezone_authoritative",
                    "Embedded capture time includes a timezone; authoritative over filesystem, "
                    "filename, and path heuristics",
                    new_created=exif_as_read,
                )
            )
            return actions
        aware_gps = attach_gps_iana_zone_to_naive_exif(
            exif_as_read, exif_gps_timezone_name
        )
        if aware_gps is not None and aware_gps.tzinfo is not None:
            actions.append(
                Action(
                    "embedded_timezone_authoritative",
                    "Embedded capture time interpreted in GPS-derived IANA timezone; authoritative "
                    "over filesystem, filename, and path heuristics",
                    new_created=aware_gps,
                )
            )
            return actions

        if neighbor_inferred_tz is not None:
            aware_nb = apply_neighbor_inferred_tz(exif_as_read, neighbor_inferred_tz)
            if aware_nb is not None and aware_nb.tzinfo is not None:
                actions.append(
                    Action(
                        "neighbor_folder_tz_inference",
                        "Naive EXIF strictly between same-timezone folder neighbors in alphabetical "
                        "order (UTC instants), with minimum anchor spacing; GPS did not yield a zone.",
                        new_created=aware_nb,
                    )
                )
                return actions

    fn_entries = [
        (naive_local(d), inc) for d, inc in extract_filename_datetime_entries(filename, now, cfg)
    ]
    fn_dates = [d for d, _ in fn_entries]

    # Sub-minute stem vs EXIF civil clock (see exif_correlates_with_clock_filename).
    # Naive stem vs naive min(created,modified) can look an hour apart on the wall even when
    # they are the same instant (e.g. stem/EXIF 09:23 +02 vs filesystem 10:23 local +03);
    # correlation means we still trust the filename clock over that naive ambiguity window.
    stem_correlates_with_exif_clock = (
        exif_as_read is not None
        and bool(fn_entries)
        and exif_correlates_with_clock_filename(exif_as_read, fn_entries)
    )

    clock_pref: tuple[str, datetime] | None = None
    if stem_correlates_with_exif_clock and exif_as_read is not None:
        clock_pref = exif_filename_clock_preference(exif_as_read, fn_entries)

    _gps_zone = (exif_gps_timezone_name or "").strip()
    has_tz_aware_capture_metadata = (exif_as_read is not None and exif_as_read.tzinfo is not None) or bool(
        _gps_zone
    )

    # Stem↔EXIF correlate within 1 minute + EXIF offset: if the filename clock’s instant (in
    # that offset) matches filesystem time within 60s, trust stem vs EXIF by sub-minute
    # direction (clock_pref). Replaces a separate “strong correlation fallback” — same idea,
    # timezone-aware (06:49 local vs 12:49 +08 can still match as one instant).
    if (
        stem_correlates_with_exif_clock
        and exif_as_read is not None
        and exif_as_read.tzinfo is not None
        and clock_pref is not None
    ):
        earliest_local = earliest.astimezone()
        instant_matches: list[tuple[datetime, bool]] = []
        for d, inc in fn_entries:
            if not inc:
                continue
            try:
                d_inst = d.replace(tzinfo=exif_as_read.tzinfo)
            except Exception:
                continue
            if abs(d_inst.timestamp() - earliest_local.timestamp()) < 60.0:
                instant_matches.append((d, inc))
        if instant_matches:
            if clock_pref[0] == "exif":
                actions.append(
                    Action(
                        "exif_matches_filename_clock",
                        "EXIF offset present; stem↔EXIF correlate; filesystem instant within 60s; "
                        "EXIF civil clock is earlier within 1 minute of filename — prefer EXIF",
                        new_created=exif_as_read,
                    )
                )
                return actions
            target, includes_clock = min(instant_matches, key=lambda x: x[0])
            actions.append(
                Action(
                    "filename_refines_metadata_seconds",
                    f"filename {target.isoformat(sep=' ')} refines filesystem time "
                    f"{earliest.isoformat(sep=' ')} (EXIF offset; stem↔EXIF correlate; instant "
                    f"within 60s; prefer filename clock within sub-minute window)",
                    new_created=target,
                    filename_includes_time_in_name=includes_clock,
                )
            )
            return actions

    qualifying_filename_for_precedence = [
        (d, inc)
        for d, inc in fn_entries
        if d < earliest
        and not should_exclude_filename_precedence_candidate(
            d,
            inc,
            earliest,
            has_tz_aware_capture_metadata=has_tz_aware_capture_metadata,
            stem_correlates_with_exif_clock=stem_correlates_with_exif_clock,
        )
    ]
    # Sub-minute skew vs clock-in-name → prefer filename (see EXIF_VS_FILENAME_CLOCK_MAX_DELTA).
    prefer_filename_within_minute = (
        clock_pref is not None and clock_pref[0] == "filename"
    )
    suppress_exif_for_filename_clock = (
        exif_as_read is not None
        and bool(qualifying_filename_for_precedence)
        and stem_correlates_with_exif_clock
        and prefer_filename_within_minute
    )

    exif_ok = (
        exif_original is not None
        and is_plausible_capture_date(exif_original, now, cfg)
        and exif_original < earliest
        and not exif_ambiguous_vs_modified(exif_original, modified)
        and not suppress_exif_for_filename_clock
    )
    if (
        exif_original is not None
        and is_plausible_capture_date(exif_original, now, cfg)
        and exif_ambiguous_vs_modified(exif_original, modified)
    ):
        exif_hint_suppressed_by_mtime = True

    if exif_ok:
        # Preserve offset-aware EXIF for correct filesystem instant; else naive-only EXIF.
        nc = (
            exif_as_read
            if (exif_as_read is not None and exif_as_read.tzinfo is not None)
            else exif_original
        )
        actions.append(
            Action(
                "exif_earlier_than_metadata",
                f"EXIF original {exif_original.isoformat(sep=' ')} < earliest meta {earliest}",
                new_created=nc,
            )
        )
        return actions

    qualifying_entries = list(qualifying_filename_for_precedence)
    if any(
        inc
        and filename_ambiguous_vs_modified(d, earliest)
        and not stem_correlates_with_exif_clock
        for d, inc in fn_entries
    ):
        filename_hint_suppressed_by_mtime = True
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

    if path_anchor_year is not None:
        slug_dt = match_structured_path(rel_parts)
        if slug_dt is not None and slug_dt.year == path_anchor_year:
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

    if not fn_dates or filename_hint_suppressed_by_mtime or exif_hint_suppressed_by_mtime:
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


def decide(
    facts: FileFacts,
    now: datetime,
    cfg: HeuristicConfig,
    *,
    path_anchor_year: int | None = None,
) -> Decision:
    """Pure decision stage: ``collect facts → decide``."""
    planned = decide_actions(
        list(facts.rel_parts),
        facts.filename,
        facts.created,
        facts.modified,
        facts.exif_original,
        now,
        cfg,
        exif_gps_timezone_name=facts.exif_gps_timezone_name,
        path_anchor_year=path_anchor_year,
        neighbor_inferred_tz=facts.neighbor_inferred_tz,
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

    Filename rule: a *clock in the filename* (explicit H:M:S from regex) scores higher than
    date-only tokens. Anchored camera patterns (IMG_/PXL_) add a small boost on top.
    """
    primary = primary_time_action(planned)
    if primary is None:
        return 30, "move_only_no_time_change"

    kind = primary.kind
    if kind == "embedded_timezone_authoritative":
        return 100, kind
    if kind == "neighbor_folder_tz_inference":
        return 96, kind
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
    if kind == "filename_refines_metadata_seconds":
        anchored = filename_has_anchor(filename)
        if anchored:
            return 93, f"{kind}_anchored"
        return 91, kind
    if kind == "exif_matches_filename_clock":
        return 97, kind
    if kind == "fallback_created_from_modified":
        return 40, kind
    return 30, kind
