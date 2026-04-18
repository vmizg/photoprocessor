# photoprocessor

Organize photos and videos by year (Windows-oriented; copies only; optional dedupe state).

**Run the organizer** from this directory:

```bash
python organize_photos.py --source "D:\inbox" --dest "D:\archive"
```

Or:

```bash
python -m src.organize_photos --source ...
```

**Tests:**

```bash
python src/__tests__/run_organize_photos_tests.py
```

See `AGENT_CONTEXT.md` for design notes.
