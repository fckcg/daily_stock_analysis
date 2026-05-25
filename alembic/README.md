# Database migrations

This directory holds [Alembic](https://alembic.sqlalchemy.org/) revisions
that govern the SQLite schema used by `DatabaseManager`.

## Why

Until May 2026 the project relied on `Base.metadata.create_all()` and had
no real migration story – any column or constraint change would silently
leave existing user databases stale. The audit (Top 2) called this out;
this directory is the fix.

## How it runs

* **Embedded (default)**: `DatabaseManager.__init__` calls
  `_apply_alembic_migrations()` after `Base.metadata.create_all()`. New
  installations create the tables, get stamped to `head`, and skip
  pending migrations. Existing installations are auto-stamped to the
  baseline (`0001`) on first launch and then upgrade to `head`.
* **CLI**: `alembic upgrade head` works against the URL defined in
  `alembic.ini` (defaults to `sqlite:///stock_analysis.db`). Override
  via `alembic -x sqlalchemy.url=sqlite:///path.db upgrade head`.
* **Disabled**: set `DSA_DISABLE_DB_MIGRATIONS=1` to skip the embedded
  upgrade (use only for emergency triage).

## Authoring a new migration

```bash
# 1. Make ORM model changes in src/storage.py first.
# 2. Generate a new revision against your local DB.
alembic revision --autogenerate -m "add foo column"
# 3. Review the generated file under alembic/versions/, edit if needed
#    (rename, batch-mode SQLite ALTER, data backfill, etc.).
# 4. Run upgrade once to confirm.
alembic upgrade head
# 5. Commit both src/storage.py + alembic/versions/<id>_<slug>.py.
```

`render_as_batch=True` is configured in `env.py`, so `op.alter_column`
and similar operations transparently rebuild the SQLite table when the
underlying engine cannot perform the change in place.

## Baseline note

Revision `0001_baseline.py` is intentionally a no-op `pass`. It exists
purely as the anchor revision for `alembic stamp`. The actual table
shapes captured by the baseline are whatever `Base.metadata.create_all()`
produced before this PR – the migration system takes ownership of every
schema change *from this point forward*.
