#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path


def iter_targets(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # ensure we traverse dot dirs too
        dirnames[:] = dirnames

        for name in filenames:
            p = Path(dirpath) / name

            if name.lower() == ".ds_store":
                yield p
                continue

            try:
                # delete dotfiles that are exactly 4096 bytes (anywhere under root)
                if name.startswith(".") and p.stat().st_size == 4096:
                    yield p
            except (FileNotFoundError, PermissionError, OSError):
                continue


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete files that are (size==4096 and name starts with '.') or named .DS_Store."
    )
    ap.add_argument("root", type=Path, help="Root directory to scan")
    ap.add_argument("--delete", action="store_true", help="Actually delete (otherwise preview)")
    args = ap.parse_args()

    root = args.root

    targets = list(iter_targets(root))

    for p in targets:
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        print(f"{p} ({size} bytes)")

    if not args.delete:
        print(f"\nPreview only. {len(targets)} files would be deleted.")
        print("Re-run with --delete to actually delete them.")
        return 0

    deleted = 0
    failed = 0
    for p in targets:
        try:
            p.unlink()
            deleted += 1
        except (FileNotFoundError, PermissionError, OSError) as e:
            failed += 1
            print(f"FAILED: {p} ({e})")

    print(f"\nDeleted: {deleted}, Failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())