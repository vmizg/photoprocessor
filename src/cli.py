"""Argument parsing and main orchestration (collect → decide → dedupe → execute)."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .dedupe import migrate_state_slot_keys, slot_key_for
from .extractors import read_exif_capture, rel_parts_for_path_dating
from .io_ops import (
    DEFAULT_LOG_ARG_SENTINEL,
    append_backup_manifest,
    backup_path_for,
    dest_path_from_slot_relative,
    emit_to_both,
    file_times,
    is_prior_organizer_output,
    iter_media_files,
    normalize_skip_path_arg,
    record_skip_duplicate,
    record_skip_identical,
    record_winner,
    resolve_log_path_arg,
    same_size_created_mtime,
    set_creation_time_windows,
    setup_logging,
    unique_dest,
    unique_superseded_name,
)
from .models import (
    FileFacts,
    HeuristicConfig,
    MYSTERY_INCUMBENT_SCORE,
    SUPERSEDED_SUBDIR,
)
from .rules import confidence_score, decide_actions
from .state_repo import (
    default_state,
    dt_iso,
    load_state_file,
    save_json_atomic,
    save_state_atomic,
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
            "If folders under --source lack a calendar year, dating may use the path "
            "from --source's parent (so a dated leaf as --source still counts)."
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
        "--backup-dir",
        type=Path,
        default=None,
        help="Optional extra copy of each source file here (source is never modified). "
        "Ignored with --dry-run.",
    )
    ap.add_argument(
        "--flat-backup-names",
        action="store_true",
        help="Store backups as <hash>.<ext> under --backup-dir (shorter paths on Windows).",
    )
    ap.add_argument(
        "--remove-backup-on-success",
        action="store_true",
        help="Delete per-file backup copy after successful copy to --dest (saves disk).",
    )
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
        "--state-file",
        type=Path,
        default=None,
        help="JSON state (slots, scores, skipped). Default: <dest>/organize_state.json when "
        "duplicate scoring is on; omit with --no-dedupe-scoring unless set explicitly. "
        "When duplicate scoring runs (not --dry-run), duplicates.json is written beside this file.",
    )
    ap.add_argument(
        "--no-dedupe-scoring",
        action="store_true",
        help="Legacy mode: use _1, _2 suffixes on name collisions instead of score-based "
        "winner/skip. Equal scores: first processed wins.",
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
        "--preserve-source-mtime",
        action="store_true",
        help="Keep the destination file's modified time (mtime) as the source mtime (copy2 default). "
        "By default, when a heuristic sets CreationTime on the copy, this script also sets mtime "
        "to the same chosen timestamp.",
    )
    ap.add_argument(
        "--silence-skipped",
        action="store_true",
        help="Do not print per-file 4-line summaries for files skipped as duplicate losers. "
        "They are still recorded in organize_state.json and in the log file.",
    )
    ap.add_argument(
        "--sort-files",
        action="store_true",
        help="Collect all media paths, sort case-insensitively, then process. "
        "Gives deterministic tie-break order for duplicate slots, but on very large folders "
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
        if args.backup_dir is not None:
            Path(args.backup_dir).resolve().mkdir(parents=True, exist_ok=True)

    if not args.dry_run and args.backup_dir is None:
        log.info(
            "Source files are never modified; output is copied to --dest. "
            "Optional --backup-dir keeps an extra copy of each source file elsewhere."
        )

    dedupe = not args.no_dedupe_scoring
    if args.state_file is not None:
        state_path_resolved: Path | None = args.state_file.resolve()
    elif dedupe:
        state_path_resolved = dest / "organize_state.json"
    else:
        state_path_resolved = None

    effective_state_path: Path | None = None
    if state_path_resolved is not None and not args.dry_run:
        effective_state_path = state_path_resolved

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
    migrate_state_slot_keys(state)
    skipped_before_run = len(state["skipped"])
    state["source"] = str(source)
    state["dest"] = str(dest)

    processed = 0
    errors = 0
    dest_res = dest.resolve()
    backup_root_res = Path(args.backup_dir).resolve() if args.backup_dir else None

    if dedupe:
        log.info(
            "Duplicate handling: canonical dest/<YEAR>/<name>; higher score wins. "
            "Tie -> first processed wins. Losers stay in source. Superseded: %s",
            dest.joinpath(SUPERSEDED_SUBDIR),
        )
    else:
        log.info(
            "Legacy duplicate handling: numeric suffixes (_1, _2). "
            "Tie-break for scoring N/A.",
        )

    if args.no_recurse:
        log.info("Scanning top-level of --source only (no subfolders).")

    skip_paths = tuple(sorted(set(args.skip_path or []), key=str.lower))
    if skip_paths:
        log.info("Skipping under --source: %s", ", ".join(skip_paths))

    legacy_dry_reserved: set[Path] = set()

    if args.sort_files:
        log.info(
            "Collecting media paths under %s for sorted processing (large trees: high memory, delay before first file)...",
            source,
        )
        _t_enum = time.perf_counter()
        _all_media = list(
            iter_media_files(
                source,
                recurse=not args.no_recurse,
                skip_path_prefixes=skip_paths,
            )
        )
        log.info(
            "Found %s media files in %.2fs; sorting...",
            len(_all_media),
            time.perf_counter() - _t_enum,
        )
        _t_sort = time.perf_counter()
        media_iter = iter(sorted(_all_media, key=lambda p: str(p).lower()))
        log.info("Sort finished in %.2fs; starting main loop.", time.perf_counter() - _t_sort)
    else:
        log.info(
            "Streaming media files in walk order (no full sort). "
            "Use --sort-files if you need deterministic duplicate tie-break order."
        )
        media_iter = iter_media_files(
            source,
            recurse=not args.no_recurse,
            skip_path_prefixes=skip_paths,
        )

    _loop_started = time.perf_counter()
    _file_index = 0
    for fpath in media_iter:
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
        if backup_root_res is not None and backup_root_res in fp_res.parents:
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
        created, modified = file_times(fpath)
        exif_cap = read_exif_capture(fpath)
        exif_dt = exif_cap.best_datetime
        exif_hint = exif_cap.hint_string()
        facts = FileFacts(
            rel_parts=tuple(rel_parts_dating),
            filename=fname,
            created=created,
            modified=modified,
            exif_original=exif_dt,
            exif_capture_hint=exif_hint,
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
        )

        final_created = created
        for a in planned:
            if a.new_created is not None:
                final_created = a.new_created

        move_year = final_created.year
        score, rule_key = confidence_score(planned, fname)
        year_dir = dest / f"{move_year}"

        # --- Stage 3–4: dedupe arbitration + execute ---
        if dedupe:
            target = year_dir / fname
            sk = slot_key_for(move_year, fname)
            slots: dict[str, Any] = state["slots"]
            if target.is_file() and sk not in slots:
                pc, pm = file_times(target)
                slots[sk] = {
                    "winner": {
                        "source_relative": "<dest_pre_existing>",
                        "score": MYSTERY_INCUMBENT_SCORE,
                        "rule": "pre_existing_on_dest",
                        "sequence": 0,
                        "original_created": dt_iso(pc),
                        "original_modified": dt_iso(pm),
                        "final_created": dt_iso(pc),
                        "final_modified": dt_iso(pm),
                        "target_relative": sk,
                        "exif_original": None,
                        "filename": fname,
                    },
                    "history": [],
                }
                if effective_state_path:
                    save_state_atomic(effective_state_path, state)

            incumbent = slots.get(sk)
            if incumbent is not None:
                w = incumbent["winner"]
                inc_score = int(w["score"])
                inc_seq = int(w["sequence"])
                lose = score < inc_score or (
                    score == inc_score and seq >= inc_seq
                )
                if lose:
                    rec = record_skip_duplicate(
                        source_relative=rel.as_posix(),
                        competing_target=sk,
                        incumbent_score=inc_score,
                        candidate_score=score,
                        candidate_rule=rule_key,
                        incumbent_source=w.get("source_relative"),
                        sequence=seq,
                    )
                    state["skipped"].append(rec)
                    dest_text = f"{dest.name}/{move_year}/{fname}".replace("\\", "/")
                    outcome_text = (
                        f"SKIP duplicate: score {score} loses to incumbent {inc_score} (slot {sk})"
                    )
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
                            dest_text=dest_text,
                            outcome_text=outcome_text,
                        )
                    if args.silence_skipped:
                        log.debug("Skipped duplicate: %s", rec)
                    else:
                        log.info("Skipped duplicate: %s", rec)
                    if effective_state_path:
                        save_state_atomic(effective_state_path, state)
                    processed += 1
                    continue

                tr = str(w.get("target_relative", sk))
                inc_path = dest_path_from_slot_relative(dest, tr)
                if inc_path.is_file() and fp_res != inc_path.resolve():
                    if same_size_created_mtime(fpath, inc_path):
                        rec_id = record_skip_identical(
                            source_relative=rel.as_posix(),
                            competing_target=sk,
                            incumbent_source=w.get("source_relative"),
                            sequence=seq,
                        )
                        state["skipped"].append(rec_id)
                        dest_text = f"{dest.name}/{move_year}/{fname}".replace("\\", "/")
                        outcome_text = (
                            "SKIP identical dest (same size, created, modified)"
                        )
                        emit_to_both(
                            out_stream=out_stream,
                            run_log=run_log,
                            rel_posix=rel.as_posix(),
                            created=created,
                            modified=modified,
                            exif_original=exif_dt,
                            exif_capture_hint=exif_hint,
                            planned=planned,
                            dest_text=dest_text,
                            outcome_text=outcome_text,
                        )
                        log.info("Skip supersede: identical to dest %s", inc_path)
                        if effective_state_path:
                            save_state_atomic(effective_state_path, state)
                        processed += 1
                        continue
                    if not args.dry_run:
                        (dest / SUPERSEDED_SUBDIR).mkdir(parents=True, exist_ok=True)
                        sup = unique_superseded_name(
                            dest, inc_path.stem, inc_path.suffix
                        )
                        shutil.move(str(inc_path), str(sup))
                        hist = {
                            "action": "superseded",
                            "source_relative": w.get("source_relative"),
                            "score": inc_score,
                            "rule": w.get("rule"),
                            "moved_to": sup.relative_to(dest).as_posix(),
                            "replaced_by_sequence": seq,
                        }
                        incumbent.setdefault("history", []).append(hist)
                        log.info("Superseded incumbent -> %s", sup)
                        if effective_state_path:
                            save_state_atomic(effective_state_path, state)
                    else:
                        hist = {
                            "action": "superseded_dry_run",
                            "source_relative": w.get("source_relative"),
                            "score": inc_score,
                            "rule": w.get("rule"),
                            "replaced_by_sequence": seq,
                        }
                        incumbent.setdefault("history", []).append(hist)
        else:
            target = unique_dest(
                year_dir / fname,
                reserved=legacy_dry_reserved if args.dry_run and not dedupe else None,
            )

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
                dest_text=dest_text,
                outcome_text=outcome_text,
            )
            log.info("DRY-RUN would copy %s -> %s", rel.as_posix(), target)
            if not dedupe:
                try:
                    legacy_dry_reserved.add(target.resolve())
                except OSError:
                    legacy_dry_reserved.add(target)
            if dedupe:
                slots = state["slots"]
                sk = slot_key_for(move_year, fname)
                wrec = record_winner(
                    source_relative=rel.as_posix(),
                    score=score,
                    rule=rule_key,
                    sequence=seq,
                    original_created=dt_iso(created),
                    original_modified=dt_iso(modified),
                    final_created=dt_iso(final_created),
                    final_modified=dt_iso(modified),
                    target_relative=sk,
                    exif_original=dt_iso(exif_dt),
                    filename=fname,
                )
                prev = slots.get(sk, {})
                slots[sk] = {
                    "winner": wrec,
                    "history": list(prev.get("history", [])),
                }
            processed += 1
            continue

        backup_abs: Path | None = None
        backup_root_path: Path | None = None
        if args.backup_dir is not None:
            backup_root_path = Path(args.backup_dir).resolve()
            backup_abs = backup_path_for(
                backup_root_path, rel, args.flat_backup_names
            )
            try:
                backup_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fpath, backup_abs)
                append_backup_manifest(
                    backup_root_path, rel, backup_abs, args.flat_backup_names
                )
                log.debug("Backup: %s -> %s", fpath, backup_abs)
            except OSError as e:
                errors += 1
                log.exception("Backup failed for %s: %s", fpath, e)
                processed += 1
                continue

        try:
            year_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, target)
            chosen_dt: datetime | None = None
            for a in planned:
                if a.new_created is not None:
                    chosen_dt = a.new_created
                    set_creation_time_windows(target, a.new_created, dry_run=False)
            if chosen_dt is None:
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
                dest_text=dest_text,
                outcome_text=outcome_text,
            )
            log.info("OK copy %s -> %s", rel.as_posix(), target)

            if dedupe:
                sk = slot_key_for(archive_year, fname)
                wrec = record_winner(
                    source_relative=rel.as_posix(),
                    score=score,
                    rule=rule_key,
                    sequence=seq,
                    original_created=dt_iso(created),
                    original_modified=dt_iso(modified),
                    final_created=dt_iso(fc),
                    final_modified=dt_iso(fm),
                    target_relative=sk,
                    exif_original=dt_iso(exif_dt),
                    filename=fname,
                )
                slot_entry = state["slots"].get(sk, {"history": []})
                slot_entry["winner"] = wrec
                state["slots"][sk] = slot_entry
                if effective_state_path:
                    save_state_atomic(effective_state_path, state)

            if (
                args.remove_backup_on_success
                and backup_abs is not None
                and backup_abs.is_file()
            ):
                try:
                    backup_abs.unlink()
                except OSError as e:
                    log.warning("Could not remove backup %s: %s", backup_abs, e)
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

    log.info(
        "Done. processed=%s errors=%s dry_run=%s",
        processed,
        errors,
        args.dry_run,
    )
    if dedupe and not args.dry_run and state_path_resolved is not None:
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
                "Wrote duplicate report (%s skipped this run) -> %s",
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
