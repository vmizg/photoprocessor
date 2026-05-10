"""Argument parsing and main orchestration (collect → decide → allocate dest → execute)."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .dedupe import (
    expand_slots_to_canonical,
    migrate_state_slot_keys,
    slot_key_for,
    slot_target_display,
)
from .datetime_policy import filesystem_instant_for_rule, normalize_iana_timezone_name
from .extractors import (
    path_calendar_divergence_hint,
    read_exif_capture,
    rel_parts_for_path_dating,
)
from .neighbor_tz import build_neighbor_tz_map
from .io_ops import (
    DEFAULT_LOG_ARG_SENTINEL,
    emit_to_both,
    file_times,
    is_prior_organizer_output,
    iter_media_files,
    normalize_include_glob_arg,
    normalize_skip_path_arg,
    record_skip_identical,
    record_winner,
    resolve_log_path_arg,
    resolve_organize_destination,
    set_creation_time_windows,
    setup_logging,
)
from .models import (
    FileFacts,
    HeuristicConfig,
)
from .rules import confidence_score, decide_actions, primary_time_action
from .state_repo import (
    StateFlushBatcher,
    default_state,
    dt_iso,
    iso_timezone_label,
    load_state_file,
    save_json_atomic,
)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Copy photos and videos from --source to --dest by year; "
        "set CreationTime on copies only. Source files are never modified."
    )
    ap.add_argument(
        "--source",
        type=Path,
        default=Path("source"),
        help=(
            'Folder to scan (default: "./source"). '
            "If folders under --source lack a calendar year, path segments for review hints "
            "may use the path from --source's parent (so a dated leaf as --source still counts)."
        ),
    )
    ap.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Root for <YEAR>/ output folders (default: same as --source)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions only; no copies, no dest/year folders, no state JSON on disk.",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument(
        "--log",
        nargs="?",
        const=DEFAULT_LOG_ARG_SENTINEL,
        default=DEFAULT_LOG_ARG_SENTINEL,
        type=resolve_log_path_arg,
        metavar="FILE",
        help="Write output/logging to FILE (default: logs/organize_photos_<timestamp>.log under repo root). "
        "Pass --log without a filename to use the default.",
    )
    ap.add_argument(
        "--min-year",
        type=int,
        default=1990,
        help="Ignore EXIF/filename dates before this year (default: 1990).",
    )
    ap.add_argument(
        "--max-year",
        type=int,
        default=2100,
        help="Ignore EXIF/filename dates after this year (default: 2100).",
    )
    ap.add_argument(
        "--year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Optional batch hint: use structured folder slug dating only when the slug year "
        "matches YYYY (pattern …/YYYY/MM/DD/yyyymmdd-HHMMss/… under --source). "
        "If omitted, folder layout is not used for capture dating. "
        "Path calendar review hints are emitted only when --year is set.",
    )
    ap.add_argument(
        "--fallback-timezone",
        type=normalize_iana_timezone_name,
        default=None,
        metavar="IANA",
        help="When embedded capture metadata exists but has no timezone (and GPS did not yield one), "
        "interpret that capture wall time in this IANA timezone instead of the local machine timezone, "
        "only after same-folder neighbor inference (alphabetical bracket + UTC-between anchors) does not apply. "
        'Example: --fallback-timezone "Europe/Berlin". Default: use local machine timezone.',
    )
    ap.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="JSON state (per-dest slots, skipped-identical). Default: <dest>/organize_state.json. "
        "When the organizer runs (not --dry-run), duplicates.json is written beside this file.",
    )
    ap.add_argument(
        "--state-save-every",
        type=int,
        default=50,
        metavar="N",
        help="Persist organize_state.json after every N in-memory state updates (skip/slot). "
        "Default 50 avoids rewriting a megabyte JSON on every file when skipped[] grows. "
        "Use 1 for maximum crash safety (slow on large state). Always flushes once at end of run.",
    )
    ap.add_argument(
        "--no-recurse",
        action="store_true",
        help="Only process files directly in --source, not in subfolders. "
        "--skip-path still applies to top-level names (e.g. skip a file path).",
    )
    ap.add_argument(
        "--skip-path",
        type=normalize_skip_path_arg,
        action="append",
        default=None,
        metavar="REL_PATH",
        help="Relative path under --source to skip (whole subtree). Repeatable. "
        "Use forward slashes; no '..'. Example: --skip-path raw --skip-path '.cache/thumbs'.",
    )
    ap.add_argument(
        "--include-glob",
        type=normalize_include_glob_arg,
        action="append",
        default=None,
        metavar="PATTERN",
        help="Only process media files whose path relative to --source matches this glob "
        "(pathlib-style, forward slashes; supports **). Repeatable — a file is processed if "
        "it matches any pattern. Example: --include-glob \"**/*.mp4\" --include-glob \"**/*.mov\"",
    )
    ap.add_argument(
        "--preserve-source-mtime",
        action="store_true",
        help="Keep the destination file's modified time (mtime) as the source mtime (copy2 default). "
        "By default, when a heuristic sets CreationTime on the copy, this script also sets mtime "
        "to the same chosen timestamp.",
    )
    ap.add_argument(
        "--silence-skipped",
        action="store_true",
        help="Do not print per-file 4-line summaries for files skipped as already present (identical). "
        "They are still recorded in organize_state.json and in the log file.",
    )
    ap.add_argument(
        "--sort-files",
        action="store_true",
        help="Collect all media paths, sort case-insensitively, then process. "
        "Gives deterministic order for same-name collisions (suffix allocation), but on very large folders "
        "this uses a lot of memory and delays the first file until the full list is built. "
        "Default is streaming walk order (faster startup).",
    )
    ap.add_argument(
        "--progress-every",
        type=int,
        default=100,
        metavar="N",
        help="Log INFO progress every N files (elapsed time + current path). "
        "Use 0 to disable. Default: 100.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    source: Path = args.source.resolve()
    dest: Path = args.dest.resolve() if args.dest is not None else source

    cfg = HeuristicConfig(min_year=args.min_year, max_year=args.max_year)
    now = datetime.now()

    if args.year is not None and (
        args.year < args.min_year or args.year > args.max_year
    ):
        print(
            f"ERROR: --year {args.year} is outside --min-year/--max-year bounds.",
            file=sys.stderr,
        )
        return 2

    log_path: Path = args.log
    log = setup_logging(log_path, args.verbose)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_log = log_path.open("a", encoding="utf-8")
    out_stream = sys.stdout

    if not source.is_dir():
        log.error("Source is not a directory: %s", source)
        run_log.close()
        for h in list(log.handlers):
            try:
                h.close()
            except OSError:
                pass
        log.handlers.clear()
        return 1

    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        log.info(
            "Source files are never modified; output is copied to --dest.",
        )

    if args.state_file is not None:
        state_path_resolved: Path | None = args.state_file.resolve()
    else:
        state_path_resolved = dest / "organize_state.json"

    effective_state_path: Path | None = None
    if state_path_resolved is not None and not args.dry_run:
        effective_state_path = state_path_resolved

    state_batcher = StateFlushBatcher(
        effective_state_path, int(args.state_save_every)
    )

    state: dict[str, Any]
    if state_path_resolved is not None and state_path_resolved.is_file():
        log.info("Loading state from %s ...", state_path_resolved)
        _t_state = time.perf_counter()
        loaded = load_state_file(state_path_resolved)
        state = loaded if isinstance(loaded, dict) else default_state(source, dest)
        n_slots = len(state.get("slots", {}))
        log.info(
            "State loaded in %.2fs (%s slots, %s skipped records)",
            time.perf_counter() - _t_state,
            n_slots,
            len(state.get("skipped", [])),
        )
    else:
        state = default_state(source, dest)
    state.setdefault("slots", {})
    state.setdefault("skipped", [])
    if isinstance(state.get("slots"), dict):
        state["slots"] = expand_slots_to_canonical(state["slots"])
    migrate_state_slot_keys(state)
    skipped_before_run = len(state["skipped"])
    state["source"] = str(source)
    state["dest"] = str(dest)

    processed = 0
    errors = 0
    path_review_count = 0
    path_review_samples: list[str] = []
    dest_res = dest.resolve()

    log.info(
        "Destination names: <dest>/<YEAR>/<original name>. "
        "If that path exists with different file bytes, use <name>_1, _2, ... in the same year folder. "
        "Identical bytes: skip copy (idempotent re-run).",
    )

    if args.no_recurse:
        log.info("Scanning top-level of --source only (no subfolders).")

    skip_paths = tuple(sorted(set(args.skip_path or []), key=str.lower))
    include_globs = tuple(sorted(set(args.include_glob or []), key=str.lower))
    if skip_paths:
        log.info("Skipping under --source: %s", ", ".join(skip_paths))
    if include_globs:
        log.info(
            "Including only paths (relative to --source) matching: %s",
            ", ".join(include_globs),
        )
    if args.year is not None:
        log.info(
            "Structured path slug dating enabled for slug year %s only; "
            "path calendar review hints enabled.",
            args.year,
        )

    log.info(
        "Collecting media paths under %s (full EXIF read follows for per-folder neighbor TZ inference)...",
        source,
    )
    _t_enum = time.perf_counter()
    all_media = list(
        iter_media_files(
            source,
            recurse=not args.no_recurse,
            skip_path_prefixes=skip_paths,
            include_globs=include_globs,
        )
    )
    log.info(
        "Found %s media files in %.2fs.",
        len(all_media),
        time.perf_counter() - _t_enum,
    )
    if args.sort_files:
        _t_sort = time.perf_counter()
        all_media.sort(key=lambda p: str(p).lower())
        log.info(
            "Sorted case-insensitively in %.2fs (--sort-files).",
            time.perf_counter() - _t_sort,
        )
    else:
        log.info(
            "Processing in directory-walk order "
            "(use --sort-files for deterministic order when the same year/name needs _1, _2 suffixes).",
        )
    _t_exif = time.perf_counter()
    exif_cache = {p: read_exif_capture(p) for p in all_media}
    neighbor_map = build_neighbor_tz_map(all_media, source, exif_cache, now, cfg)
    log.info(
        "EXIF preload + neighbor TZ map in %.2fs (%s inferred).",
        time.perf_counter() - _t_exif,
        len(neighbor_map),
    )

    _loop_started = time.perf_counter()
    _file_index = 0
    dry_run_claims: dict[Path, Path] = {}
    for fpath in all_media:
        _file_index += 1
        if args.progress_every > 0 and _file_index % args.progress_every == 0:
            try:
                _rel_prog = fpath.relative_to(source).as_posix()
            except ValueError:
                _rel_prog = str(fpath)
            log.info(
                "Progress: file #%s ~%.1fs elapsed — %s",
                _file_index,
                time.perf_counter() - _loop_started,
                _rel_prog,
            )
        fp_res = fpath.resolve()
        if is_prior_organizer_output(
            fp_res,
            dest_res,
            min_year=args.min_year,
            max_year=args.max_year,
        ):
            continue
        try:
            rel = fpath.relative_to(source)
        except ValueError:
            continue

        state["sequence"] = int(state.get("sequence", 0)) + 1
        seq = state["sequence"]

        # --- Stage 1: collect facts ---
        rel_parts_dating = rel_parts_for_path_dating(fpath, source)
        fname = rel.name
        if sys.platform == "win32":
            # NFC aligns decomposed vs precomposed Unicode; preserve original casing on disk.
            dest_basename = unicodedata.normalize("NFC", fname)
        else:
            dest_basename = fname
        created, modified = file_times(fpath)
        exif_cap = exif_cache[fpath]
        exif_dt = exif_cap.best_datetime
        exif_hint = exif_cap.hint_string()
        nb_tz = neighbor_map.get(rel.as_posix())
        facts = FileFacts(
            rel_parts=tuple(rel_parts_dating),
            filename=fname,
            created=created,
            modified=modified,
            exif_original=exif_dt,
            exif_capture_hint=exif_hint,
            exif_gps_timezone_name=exif_cap.gps_timezone_name,
            neighbor_inferred_tz=nb_tz,
        )

        # --- Stage 2: decide ---
        planned = decide_actions(
            list(facts.rel_parts),
            facts.filename,
            facts.created,
            facts.modified,
            facts.exif_original,
            now,
            cfg,
            exif_gps_timezone_name=facts.exif_gps_timezone_name,
            path_anchor_year=args.year,
            neighbor_inferred_tz=nb_tz,
        )

        primary = primary_time_action(planned)
        fs_materialized: datetime | None = None
        if primary is not None and primary.new_created is not None:
            fs_materialized = filesystem_instant_for_rule(
                primary.new_created,
                primary.kind,
                facts.exif_original,
                facts.exif_gps_timezone_name,
                args.fallback_timezone,
            )

        final_created = created
        if primary is not None and primary.new_created is not None:
            final_created = fs_materialized

        if args.year is not None:
            pr_note = path_calendar_divergence_hint(
                list(facts.rel_parts), final_created, now, cfg
            )
            if pr_note is not None:
                path_review_count += 1
                if len(path_review_samples) < 15:
                    path_review_samples.append(f"{rel.as_posix()}: {pr_note}")

        move_year = final_created.year
        score, rule_key = confidence_score(planned, fname)
        tz_materialized = iso_timezone_label(fs_materialized)
        if tz_materialized is None and primary is not None:
            tz_materialized = iso_timezone_label(primary.new_created)
        tz_exif = iso_timezone_label(exif_dt)
        year_dir = dest / f"{move_year}"

        canonical = year_dir / dest_basename
        target, dest_kind = resolve_organize_destination(
            fpath,
            fp_resolved=fp_res,
            canonical_dest=canonical,
            dry_run_claims=dry_run_claims if args.dry_run else None,
        )

        if dest_kind == "already_at_dest":
            processed += 1
            continue

        if dest_kind == "skip_identical":
            rec_id = record_skip_identical(
                source_relative=rel.as_posix(),
                competing_target=slot_target_display(move_year, target.name),
                incumbent_source="<dest_already_present>",
                sequence=seq,
            )
            state["skipped"].append(rec_id)
            dest_text = f"{dest.name}/{move_year}/{target.name}".replace("\\", "/")
            outcome_text = "SKIP already present (same file bytes)"
            if not args.silence_skipped:
                emit_to_both(
                    out_stream=out_stream,
                    run_log=run_log,
                    rel_posix=rel.as_posix(),
                    created=created,
                    modified=modified,
                    exif_original=exif_dt,
                    exif_capture_hint=exif_hint,
                    planned=planned,
                    materialized_created=fs_materialized,
                    dest_text=dest_text,
                    outcome_text=outcome_text,
                )
            if args.silence_skipped:
                log.debug("Skip identical dest: %s", rec_id)
            else:
                log.info("Skip copy: identical to existing dest %s", target)
            state_batcher.after_mutation(state)
            processed += 1
            continue

        if args.dry_run:
            dry_run_claims[target.resolve()] = fp_res

        if target.resolve() == fp_res:
            processed += 1
            continue

        if args.dry_run:
            try:
                show = target.relative_to(dest.parent)
            except ValueError:
                show = target
            dest_text = str(show).replace("\\", "/")
            outcome_text = "DRY-RUN would copy"
            emit_to_both(
                out_stream=out_stream,
                run_log=run_log,
                rel_posix=rel.as_posix(),
                created=created,
                modified=modified,
                exif_original=exif_dt,
                exif_capture_hint=exif_hint,
                planned=planned,
                materialized_created=fs_materialized,
                dest_text=dest_text,
                outcome_text=outcome_text,
            )
            log.info("DRY-RUN would copy %s -> %s", rel.as_posix(), target)
            slots = state["slots"]
            out_name = target.name
            sk = slot_key_for(move_year, out_name)
            wrec = record_winner(
                source_relative=rel.as_posix(),
                score=score,
                rule=rule_key,
                sequence=seq,
                original_created=dt_iso(created),
                original_modified=dt_iso(modified),
                final_created=dt_iso(final_created),
                final_modified=dt_iso(modified),
                target_relative=slot_target_display(move_year, out_name),
                exif_original=dt_iso(exif_dt),
                filename=fname,
                materialized_timezone=tz_materialized,
                exif_timezone=tz_exif,
            )
            prev = slots.get(sk, {})
            slots[sk] = {
                "canonical_slot": sk,
                "winner": wrec,
                "history": list(prev.get("history", [])),
            }
            processed += 1
            continue

        try:
            year_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, target)
            chosen_dt: datetime | None = fs_materialized
            if chosen_dt is not None:
                set_creation_time_windows(target, chosen_dt, dry_run=False)
            else:
                set_creation_time_windows(target, created, dry_run=False)
            if (not args.preserve_source_mtime) and chosen_dt is not None:
                ts = chosen_dt.timestamp()
                os.utime(target, (ts, ts))

            fc, fm = file_times(target)
            archive_year = int(target.resolve().parent.name)
            dest_text = str(target)
            outcome_text = "COPIED"
            emit_to_both(
                out_stream=out_stream,
                run_log=run_log,
                rel_posix=rel.as_posix(),
                created=created,
                modified=modified,
                exif_original=exif_dt,
                exif_capture_hint=exif_hint,
                planned=planned,
                materialized_created=fs_materialized,
                dest_text=dest_text,
                outcome_text=outcome_text,
            )
            log.info("OK copy %s -> %s", rel.as_posix(), target)

            out_name = target.name
            sk = slot_key_for(archive_year, out_name)
            wrec = record_winner(
                source_relative=rel.as_posix(),
                score=score,
                rule=rule_key,
                sequence=seq,
                original_created=dt_iso(created),
                original_modified=dt_iso(modified),
                final_created=dt_iso(fc),
                final_modified=dt_iso(fm),
                target_relative=slot_target_display(archive_year, out_name),
                exif_original=dt_iso(exif_dt),
                filename=fname,
                materialized_timezone=tz_materialized,
                exif_timezone=tz_exif,
            )
            slot_entry = state["slots"].get(sk, {"history": []})
            slot_entry["winner"] = wrec
            slot_entry["canonical_slot"] = sk
            state["slots"][sk] = slot_entry
            state_batcher.after_mutation(state)

        except Exception as e:
            errors += 1
            log.exception("Failed processing %s: %s", fpath, e)
            if not args.dry_run and target.is_file() and target.resolve() != fp_res:
                try:
                    target.unlink()
                    log.warning("Removed partial copy: %s", target)
                except OSError as ue:
                    log.error("Could not remove partial copy %s: %s", target, ue)

        processed += 1

    state_batcher.end(state)

    if path_review_count > 0:
        review_msg = (
            "Path calendar review hint: %s file(s) had folder paths implying a date period "
            "that does not contain the resolved capture time — consider manual review. "
            "Examples: %s"
            % (
                path_review_count,
                "; ".join(path_review_samples) if path_review_samples else "(none)",
            )
        )
        log.info(review_msg)
        run_log.write(review_msg + "\n")
        run_log.flush()

    log.info(
        "Done. processed=%s errors=%s dry_run=%s",
        processed,
        errors,
        args.dry_run,
    )
    if not args.dry_run and state_path_resolved is not None:
        session_dupes = state["skipped"][skipped_before_run:]
        dup_path = state_path_resolved.with_name("duplicates.json")
        report: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": str(source),
            "dest": str(dest),
            "state_file": str(state_path_resolved),
            "count": len(session_dupes),
            "duplicates": session_dupes,
        }
        try:
            save_json_atomic(dup_path, report)
            log.info(
                "Wrote duplicates.json (%s skipped-as-identical this run) -> %s",
                len(session_dupes),
                dup_path,
            )
        except OSError as e:
            log.warning("Could not write duplicate report %s: %s", dup_path, e)
    print(f"\nDone. Files processed: {processed}. Errors: {errors}. Dry run: {args.dry_run}")
    run_log.write(
        f"\nDone. Files processed: {processed}. Errors: {errors}. Dry run: {args.dry_run}\n"
    )
    run_log.flush()
    run_log.close()
    if processed == 0:
        log.warning(
            "No files processed under %s (wrong --dest vs --source, "
            "only prior-output paths, no matching extensions, or empty tree).",
            source,
        )
    return 1 if errors else 0
