#!/usr/bin/env python3
"""
Organize photos/videos by year — thin entry point at the repo root.

Delegates to ``src.cli:main``. Run from the ``photoprocessor`` directory:

  python organize_photos.py --source "D:\\inbox" --dest "D:\\archive"
"""

from __future__ import annotations

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
