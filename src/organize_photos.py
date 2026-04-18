#!/usr/bin/env python3
"""
Traverse a directory under --source (read-only), copy photos and videos into <dest>/<YYYY>/,
and set CreationTime on the **copies** only using EXIF (optional), filename/path heuristics.

Package layout: ``models``, ``datetime_policy``, ``extractors``, ``rules``, ``dedupe``,
``state_repo``, ``io_ops``, ``cli``.

From repo root, prefer the root wrapper:

  python organize_photos.py --source "D:\\Photos\\inbox" --dest "D:\\Photos\\archive"

Or:

  python -m src.organize_photos --source ...
"""

from __future__ import annotations

from .cli import main
from .datetime_policy import (
    TZ_NAIVE_VS_MODIFIED_EQUIV_MAX,
    exif_ambiguous_vs_modified,
    naive_local,
)
from .dedupe import migrate_state_slot_keys, normalize_slot_key_for_dedupe, slot_key_for
from .extractors import (
    extract_filename_datetime_entries,
    match_structured_path,
    parse_path_calendar,
    path_has_calendar_hint,
    path_implies_date_period,
    read_exif_capture,
    rel_parts_for_path_dating,
    try_exif_datetime_original,
)
from .io_ops import (
    dest_path_from_slot_relative,
    file_times,
    is_prior_organizer_output,
    iter_media_files,
    normalize_skip_path_arg,
    unique_dest,
)
from .models import (
    Action,
    ExifCaptureInfo,
    HeuristicConfig,
    MEDIA_EXTENSIONS,
    MYSTERY_INCUMBENT_SCORE,
    PHOTO_EXTENSIONS,
    STATE_VERSION,
    SUPERSEDED_SUBDIR,
    VIDEO_EXTENSIONS,
)
from .rules import (
    confidence_score,
    decide_actions,
    filename_has_anchor,
    primary_time_action,
)
from .state_repo import (
    default_state,
    load_state_file,
    save_json_atomic,
    save_state_atomic,
)

_try_exif_datetime_original = try_exif_datetime_original
_exif_ambiguous_vs_modified = exif_ambiguous_vs_modified

__all__ = [
    "Action",
    "ExifCaptureInfo",
    "HeuristicConfig",
    "MEDIA_EXTENSIONS",
    "MYSTERY_INCUMBENT_SCORE",
    "PHOTO_EXTENSIONS",
    "STATE_VERSION",
    "SUPERSEDED_SUBDIR",
    "TZ_NAIVE_VS_MODIFIED_EQUIV_MAX",
    "VIDEO_EXTENSIONS",
    "_exif_ambiguous_vs_modified",
    "_try_exif_datetime_original",
    "confidence_score",
    "decide_actions",
    "default_state",
    "dest_path_from_slot_relative",
    "extract_filename_datetime_entries",
    "file_times",
    "filename_has_anchor",
    "is_prior_organizer_output",
    "iter_media_files",
    "load_state_file",
    "main",
    "match_structured_path",
    "migrate_state_slot_keys",
    "naive_local",
    "normalize_skip_path_arg",
    "normalize_slot_key_for_dedupe",
    "parse_path_calendar",
    "path_has_calendar_hint",
    "path_implies_date_period",
    "primary_time_action",
    "read_exif_capture",
    "rel_parts_for_path_dating",
    "save_json_atomic",
    "save_state_atomic",
    "slot_key_for",
    "try_exif_datetime_original",
    "unique_dest",
]


if __name__ == "__main__":
    raise SystemExit(main())
