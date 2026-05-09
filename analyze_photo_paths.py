#!/usr/bin/env python3
"""
Scan a photo library tree and summarize distinct patterns.

Includes:
- Folder layout date hints (year/month/day)
- Filename date patterns
- Filesystem timestamps (ctime/mtime)
- EXIF DateTimeOriginal when Pillow is installed
- Which src.organize_photos rule would be chosen (decide_actions)

This intentionally prints *patterns* and a few samples, not a full file listing.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

re_year = re.compile(r"^(\d{4})$")
re_iso = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
re_eu = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")
re_ym_num = re.compile(r"^(\d{4})-(\d{1,2})$")
re_ym_mon = re.compile(r"^(\d{4})-([A-Za-z]{3,12})$")
re_dd_mmm_yyyy = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
re_slug = re.compile(r"^(\d{8})-(\d{6})$")

MONTHS = {
    m.lower()
    for m in [
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "sept",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    ]
}


def classify_segment(seg: str) -> str | None:
    s = seg.strip()
    if re_year.match(s):
        return "YYYY"
    if re_ym_num.match(s):
        return "YYYY-MM"
    m = re_ym_mon.match(s)
    if m and m.group(2).lower() in MONTHS:
        return "YYYY-Mmm"
    if re_iso.match(s):
        return "YYYY-MM-DD"
    if re_eu.match(s):
        return "D-M-YYYY_or_DD.MM.YYYY"
    if re_dd_mmm_yyyy.match(s):
        return "DD-Mmm-YYYY"
    if re_slug.match(s):
        return "yyyymmdd-HHMMss"
    if s.isdigit() and len(s) in (1, 2):
        return "numeric_1or2"
    return None


def structured_path_kind(parts: list[str]) -> str | None:
    # Apple/structured: YYYY/MM/DD/yyyymmdd-HHMMss
    if len(parts) >= 4:
        if (
            re_year.match(parts[0])
            and parts[1].isdigit()
            and len(parts[1]) == 2
            and parts[2].isdigit()
            and len(parts[2]) == 2
            and re_slug.match(parts[3])
        ):
            return "Apple_structured_YYYY/MM/DD/slug"
    return None


def add(
    patterns: dict[str, dict[str, object]],
    key: str,
    sample: str,
    *,
    sample_limit: int,
) -> None:
    e = patterns.setdefault(key, {"count": 0, "samples": []})
    e["count"] = int(e["count"]) + 1
    samples = e["samples"]
    assert isinstance(samples, list)
    if len(samples) < sample_limit:
        samples.append(sample)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--top", type=int, default=120, help="Max pattern keys to print")
    ap.add_argument("--samples", type=int, default=3, help="Samples per pattern key")
    ap.add_argument(
        "--files",
        action="store_true",
        help="Also scan media files and compute decide_actions patterns.",
    )
    args = ap.parse_args()

    root: Path = args.root
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}")
        return 2

    patterns: dict[str, dict[str, object]] = {}
    root_r = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root_r):
        p = Path(dirpath)
        try:
            rel = p.relative_to(root_r)
        except Exception:
            continue
        parts = list(PurePosixPath(rel.as_posix()).parts)
        if not parts:
            continue

        sk = structured_path_kind(parts)
        if sk:
            add(patterns, sk, str(rel).replace("\\", "/"), sample_limit=args.samples)

        for seg in parts:
            c = classify_segment(seg)
            if c:
                add(patterns, f"seg:{c}", seg, sample_limit=args.samples)

        lead = parts[:4]
        lead_classes = [classify_segment(x) or "other" for x in lead]
        add(
            patterns,
            "lead:" + "/".join(lead_classes),
            "/".join(lead),
            sample_limit=args.samples,
        )

    if args.files:
        repo_root = Path(__file__).resolve().parent
        sd = str(repo_root)
        if sd not in sys.path:
            sys.path.insert(0, sd)
        try:
            import src.organize_photos as op
        except Exception as e:
            print(f"ERROR: could not import src.organize_photos: {e}")
            return 3

        now = datetime.now()
        cfg = op.HeuristicConfig()

        file_patterns: dict[str, dict[str, object]] = {}

        def add_file(key: str, sample: str) -> None:
            add(file_patterns, key, sample, sample_limit=args.samples)

        def safe_rel(p: Path) -> str:
            try:
                return str(p.relative_to(root_r)).replace("\\", "/")
            except Exception:
                return str(p).replace("\\", "/")

        def fmt_dt(d: datetime | None) -> str:
            if d is None:
                return "-"
            return d.isoformat(sep=" ", timespec="seconds")

        for fpath in op.iter_media_files(root_r, recurse=True):
            try:
                rel = fpath.relative_to(root_r)
            except Exception:
                continue
            rel_posix = rel.as_posix()
            rel_parts = list(rel.parts[:-1])
            fname = rel.name

            created, modified = op.file_times(fpath)
            exif_cap = op.read_exif_capture(fpath)
            exif_dt = exif_cap.best_datetime
            earliest_dt = created if created <= modified else modified
            earliest_label = "created" if earliest_dt == created else "modified"

            rel_parts_dating = op.rel_parts_for_path_dating(fpath, root_r)
            planned = op.decide_actions(
                rel_parts_dating,
                fname,
                created,
                modified,
                exif_dt,
                now,
                cfg,
                exif_gps_timezone_name=exif_cap.gps_timezone_name,
            )
            primary = op.primary_time_action(planned)
            primary_kind = primary.kind if primary is not None else "move_only_no_time_change"

            fn_entries = op.extract_filename_datetime_entries(fname, now, cfg)
            fn_has_clock = any(inc for _d, inc in fn_entries)
            fn_has_date = bool(fn_entries)

            folder_hint = op.path_has_calendar_hint(rel_parts_dating)
            folder_period = op.path_implies_date_period(rel_parts_dating)
            folder_gran = "none"
            if folder_period is not None:
                y, mo, da = op.parse_path_calendar(rel_parts_dating)
                if y is not None and mo is None:
                    folder_gran = "year"
                elif y is not None and mo is not None and da is None:
                    folder_gran = "month"
                elif y is not None and mo is not None and da is not None:
                    folder_gran = "day"

            exif_present = exif_dt is not None
            exif_ambig = (
                exif_dt is not None and op._exif_ambiguous_vs_modified(exif_dt, modified)
            )

            # Key is intentionally coarse (distinct patterns).
            key = "|".join(
                [
                    f"rule:{primary_kind}",
                    f"folder:{'Y' if folder_hint else 'N'}",
                    f"folder_gran:{folder_gran}",
                    f"fn_date:{'Y' if fn_has_date else 'N'}",
                    f"fn_clock:{'Y' if fn_has_clock else 'N'}",
                    f"exif:{'Y' if exif_present else 'N'}",
                    f"exif_ambig:{'Y' if exif_ambig else 'N'}",
                    f"ext:{fpath.suffix.lower()}",
                ]
            )
            sample = (
                f"{rel_posix} "
                f"[EXIF]{fmt_dt(exif_dt)} "
                f"[{earliest_label}]{fmt_dt(earliest_dt)}"
            )
            add_file(key, sample)

        # Merge file patterns into patterns with a prefix so sorting includes both.
        for k, v in file_patterns.items():
            patterns["file:" + k] = v

    items = sorted(patterns.items(), key=lambda kv: (-int(kv[1]["count"]), kv[0]))
    print(f"Root: {root_r}")
    print(f"Unique pattern keys: {len(items)}")
    print()
    for k, v in items[: args.top]:
        print(f"{int(v['count']):7d}  {k}")
        for s in v["samples"]:
            print(f"         sample: {s}")
    if len(items) > args.top:
        print(f"... ({len(items) - args.top} more patterns omitted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

