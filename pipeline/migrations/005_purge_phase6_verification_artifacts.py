"""Migration 005 — purge the two Phase 6 verification artifacts.

Why
---
Phase 6 (post-SQLite-migration verification) ran two end-to-end
ingest tests that were tombstoned immediately after promotion:

* ``ds_e699f546087a`` — Phase 6 first ingest test, allocated by the
  pre-fix ``utils.new_dataset_id()`` (legacy ``ds_<12hex>`` format).
* ``ds_01KQE2RJH52KQA3HNKBN2MXHRB`` — Phase 6 second ingest test
  (post-ULID-fix).

Both rows are tombstoned in the catalogue and have surviving source
bundles in ``queue/archived/``. They were verification scaffolding,
not real datasets — and the lingering hex-format archive directory
clutters an otherwise ULID-only ``queue/archived/`` listing.

This migration hard-deletes both rows, their changelog history, and
their archive directories. Like migration 004, it's a deliberate
exception to the "tombstone, never delete" policy in DESIGN.md
§5/§10 — the catalogue is pre-production and the verification audit
trail isn't worth keeping.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'005'`` is already
  recorded in ``schema_migrations``.
* **Safety.** Refuses to run if either target is not currently
  ``status='tombstoned'`` (so we never accidentally delete an active
  dataset by editing the target list).
* **Audit.** Records itself in ``schema_migrations``. Per-dataset
  changelog rows aren't written — there's no surviving dataset row
  to FK to.

Usage
-----
::

    python pipeline/migrations/005_purge_phase6_verification_artifacts.py
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "005"
MIGRATION_DESCRIPTION = (
    "Hard-delete Phase 6 verification artifacts (2 tombstoned rows + "
    "their archive bundles); FK constraint bypassed"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_ARCHIVED = PROJECT_ROOT / "queue" / "archived"

# The exact dataset_ids to purge. Hard-coded so the migration can't
# accidentally widen its scope by re-querying.
_TARGETS = (
    "ds_e699f546087a",
    "ds_01KQE2RJH52KQA3HNKBN2MXHRB",
)


def _migration_already_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    )
    return cur.fetchone() is not None


def _check_prereq_migrations(conn) -> list[str]:
    have = {
        r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    missing = [v for v in ("001", "002", "003", "004") if v not in have]
    return missing


def run(*, db_path: Path, archived_dir: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn):
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied. "
                f"This migration is one-shot and intentionally has no --force.",
                file=sys.stderr,
            )
            return 1

        missing = _check_prereq_migrations(conn)
        if missing:
            print(
                f"ERROR: prerequisite migrations not applied: {missing}. "
                f"Run them first.",
                file=sys.stderr,
            )
            return 1

        # Safety check: every target must currently be tombstoned. Refuse
        # to run if any target is active or missing.
        placeholders = ",".join(["?"] * len(_TARGETS))
        rows = conn.execute(
            f"SELECT dataset_id, status, title FROM datasets "
            f"WHERE dataset_id IN ({placeholders})",
            _TARGETS,
        ).fetchall()

        rows_by_id = {r["dataset_id"]: dict(r) for r in rows}
        problems: list[str] = []
        for t in _TARGETS:
            if t not in rows_by_id:
                problems.append(f"  {t}: not present in catalogue")
            elif rows_by_id[t]["status"] != "tombstoned":
                problems.append(
                    f"  {t}: status is {rows_by_id[t]['status']!r}, "
                    f"expected 'tombstoned'"
                )
        if problems:
            print(
                "ERROR: targets are not in the expected state:",
                file=sys.stderr,
            )
            for p in problems:
                print(p, file=sys.stderr)
            return 1

        for t in _TARGETS:
            row = rows_by_id[t]
            print(f"  target: {t}  status={row['status']}  title={row['title']!r}")

    # --- DELETE rows + changelog (FK off) -----------------------------
    print()
    print("Disabling FK enforcement; deleting rows + changelog history…")
    placeholders = ",".join(["?"] * len(_TARGETS))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            cur = conn.execute(
                f"DELETE FROM changelog WHERE dataset_id IN ({placeholders})",
                _TARGETS,
            )
            cl_deleted = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM datasets WHERE dataset_id IN ({placeholders})",
                _TARGETS,
            )
            ds_deleted = cur.rowcount
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (MIGRATION_VERSION, utc_now_iso(), MIGRATION_DESCRIPTION),
            )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            print(
                f"ERROR: foreign_key_check reports {len(violations)} "
                f"violation(s) after delete. Aborting before re-enabling FKs.",
                file=sys.stderr,
            )
            return 2
        conn.execute("PRAGMA foreign_keys = ON")
    print(f"  deleted {ds_deleted} dataset rows, {cl_deleted} changelog rows")

    # --- remove archive directories -----------------------------------
    print()
    print("Removing archive bundles…")
    archives_removed = 0
    for t in _TARGETS:
        d = archived_dir / t
        if d.is_dir():
            shutil.rmtree(d)
            print(f"  rmtree: {d}")
            archives_removed += 1
        else:
            print(f"  skip (already gone): {d}")

    print()
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  rows deleted:        {ds_deleted}")
    print(f"  changelog deleted:   {cl_deleted}")
    print(f"  archives removed:    {archives_removed}")
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-005",
        description=(__doc__ or "").splitlines()[0],
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--archived", type=Path, default=DEFAULT_ARCHIVED)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return run(db_path=args.db, archived_dir=args.archived)


if __name__ == "__main__":
    raise SystemExit(main())
