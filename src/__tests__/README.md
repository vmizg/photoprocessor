# Organizer test fixtures

`run_organize_photos_tests.py` builds a **temporary** source tree (nothing under this folder is committed as sample binaries), runs the repo-root **`organize_photos.py`**, and asserts **per-file** verbose output against the hardcoded `DRY_RUN_FILE_EXPECTATIONS` table in that script (rule tags, timestamps, dry-run dest path, duplicate skip, source `created=` vs destination copy CreationTime, and a `touch_outcome` flag: touched / same-as-source / skipped duplicate).

Run (from **photoprocessor** repo root):

```bash
python src/__tests__/run_organize_photos_tests.py
python src/__tests__/run_organize_photos_tests.py --live
```

`--live` performs real copies under a temp directory and sets Windows CreationTime (needs PowerShell).
