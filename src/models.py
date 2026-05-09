"""Dataclasses and shared constants for the organize pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# JSON state / duplicate scoring
STATE_VERSION = 1
MYSTERY_INCUMBENT_SCORE = 30
SUPERSEDED_SUBDIR = "_superseded"

PHOTO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".webp",
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".orf",
    ".rw2",
}

VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".3gp",
        ".mts",
        ".m2ts",
        ".flv",
        ".ogv",
    }
)

MEDIA_EXTENSIONS = frozenset(PHOTO_EXTENSIONS) | VIDEO_EXTENSIONS


@dataclass(frozen=True)
class HeuristicConfig:
    min_year: int = 1990
    max_year: int = 2100
    future_slack: timedelta = field(default_factory=lambda: timedelta(days=1))


@dataclass
class Action:
    kind: str
    detail: str
    new_created: datetime | None = None
    filename_includes_time_in_name: bool | None = None


@dataclass(frozen=True)
class ExifCaptureInfo:
    """EXIF read result: best datetime plus optional OffsetTime / GPS hints."""

    best_datetime: datetime | None
    offset_time_original: str | None = None
    offset_time_digitized: str | None = None
    has_gps: bool = False
    #: IANA zone inferred from GPS when DateTime* had no OffsetTime tags (optional).
    gps_timezone_name: str | None = None
    #: When GPS was present but IANA/zone data could not be applied (verbose hint only).
    gps_tz_skip: str | None = None
    #: Where embedded capture time came from for video (e.g. ``ffprobe:creation_time``).
    video_metadata_source: str | None = None

    def hint_string(self) -> str | None:
        parts: list[str] = []
        if self.video_metadata_source:
            parts.append(self.video_metadata_source)
        if self.offset_time_original:
            parts.append(f"OffsetTimeOriginal={self.offset_time_original}")
        if self.offset_time_digitized and not self.offset_time_original:
            parts.append(f"OffsetTimeDigitized={self.offset_time_digitized}")
        if self.gps_timezone_name:
            parts.append(f"tz~GPS={self.gps_timezone_name}")
        elif self.gps_tz_skip:
            parts.append(f"GPS~no_tz={self.gps_tz_skip}")
        elif self.has_gps:
            parts.append("GPS=yes")
        return "; ".join(parts) if parts else None


@dataclass(frozen=True)
class FileFacts:
    """Per-file inputs for the decision engine (collect stage)."""

    rel_parts: tuple[str, ...]
    filename: str
    created: datetime
    modified: datetime
    exif_original: datetime | None
    exif_capture_hint: str | None = None
    #: IANA zone from GPS when EXIF had no offset (see :class:`ExifCaptureInfo`).
    exif_gps_timezone_name: str | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of :func:`src.rules.decide` (rules stage)."""

    actions: tuple[Action, ...]

    @classmethod
    def from_actions(cls, actions: list[Action]) -> Decision:
        return cls(actions=tuple(actions))

    def planned_list(self) -> list[Action]:
        return list(self.actions)

    def resolved_created(self, fallback: datetime) -> datetime:
        out = fallback
        for a in self.actions:
            if a.new_created is not None:
                out = a.new_created
        return out


@dataclass(frozen=True)
class RunConfig:
    """Resolved CLI options needed outside argparse (orchestration)."""

    source: Path
    dest: Path
    dry_run: bool
    dedupe_scoring: bool
    no_recurse: bool
    skip_path_prefixes: tuple[str, ...]
    backup_dir: Path | None
    flat_backup_names: bool
    remove_backup_on_success: bool
    preserve_source_mtime: bool
    silence_skipped: bool
    sort_files: bool
    progress_every: int
    state_file: Path | None
    verbose: bool
    min_year: int
    max_year: int


@dataclass(frozen=True)
class Candidate:
    """Competing file for a dedupe slot (score arbitration)."""

    source_relative: str
    score: int
    rule_key: str
    sequence: int


@dataclass(frozen=True)
class SlotState:
    """In-memory view of one JSON slot entry (winner + optional history)."""

    winner: dict[str, Any]
    history: tuple[dict[str, Any], ...] = ()
