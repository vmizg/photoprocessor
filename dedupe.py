#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileKey:
    size: int
    mtime_ns: int
    ctime_ns: int


def iter_files(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield entry
                    except OSError:
                        continue
        except OSError:
            continue


def key_for_entry(entry: os.DirEntry) -> FileKey | None:
    try:
        st = entry.stat(follow_symlinks=False)
        return FileKey(size=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)
    except OSError:
        return None


def build_index(root: Path) -> dict[FileKey, list[Path]]:
    index: dict[FileKey, list[Path]] = {}
    for e in iter_files(root):
        k = key_for_entry(e)
        if k is None:
            continue
        index.setdefault(k, []).append(Path(e.path))
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Deduplicate files between two folders by size+mtime+ctime.")
    ap.add_argument("a", type=Path, help="Folder A (kept by default)")
    ap.add_argument("b", type=Path, help="Folder B (duplicates deleted by default)")
    ap.add_argument("--delete-from", choices=["a", "b"], default="b", help="Which side to delete duplicates from")
    ap.add_argument("--do-it", action="store_true", help="Actually delete (otherwise dry-run)")
    args = ap.parse_args()

    a = args.a.resolve()
    b = args.b.resolve()

    index_a = build_index(a)
    index_b = build_index(b)

    keep_root = a if args.delete_from == "b" else b
    del_root = b if args.delete_from == "b" else a
    index_keep = index_a if args.delete_from == "b" else index_b
    index_del = index_b if args.delete_from == "b" else index_a

    to_delete: list[Path] = []
    for k, del_paths in index_del.items():
        if k in index_keep:
            to_delete.extend(del_paths)

    for p in to_delete:
        print(f"DELETE {p}")

    if not args.do_it:
        print(f"\nDry-run. {len(to_delete)} files would be deleted from {del_root}")
        print("Re-run with --do-it to actually delete.")
        return 0

    deleted = 0
    failed = 0
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            failed += 1
            print(f"FAILED {p}: {e}")

    print(f"\nDeleted: {deleted}, Failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())