"""Migration 009 — adopt existing AGOL items into sync management.

Why
---
Some catalogue rows carry an ``agol_item_id`` because the steward
published them to AGOL manually (web UI) before this integration
existed. Their ``sync_status`` is ``'unpublished'`` because the
catalogue's sync state machine has never reconciled them against
AGOL. This migration brings each such row under sync management by
diffing catalogue ↔ AGOL field-for-field and marking the row
either ``'clean'`` (matched) or ``'conflict'`` (drifted; Phase D
pull will resolve).

What this migration does
------------------------
1. Connects to AGOL via the OAuth profile (steward must have run
   ``y2y agol-sync login`` at least once).
2. Selects every row matching ``status='active' AND
   agol_item_id IS NOT NULL AND sync_status='unpublished'``.
3. For each row, calls :func:`agol_sync.adopt_row` which:
   * Fetches the AGOL item.
   * Diffs catalogue ↔ AGOL fields (title, snippet, description,
     tags, accessInformation, licenseInfo, categories).
   * No diff → marks ``sync_status='clean'``, populates
     ``last_synced_at`` and (if absent) ``agol_published_at``.
   * Diff → marks ``sync_status='conflict'``, writes a structured
     changelog entry containing the per-field diff.
   * AGOL item missing → marks ``sync_status='error'`` with a
     remediation hint in ``internal_notes``.
4. Records itself in ``schema_migrations``.

Adoption never mutates AGOL. It's catalogue-only state.

Behaviour
---------
* **Idempotent at the migration level.** Refuses to run if
  version ``'009'`` is already in ``schema_migrations``.
* **Idempotent at the per-row level.** Only adopts rows still in
  ``'unpublished'``; once a row hits ``'clean'`` /
  ``'conflict'`` / ``'error'`` it's outside the adoption candidate
  set on any subsequent re-run.
* **Pre-flight.** Migrations 001–008 applied; AGOL connection
  works; the steward authenticates before this migration runs.

Usage
-----
::

    python pipeline/migrations/009_agol_adopt_existing.py
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

from pipeline import agol_config, agol_sync, inventory_manager
from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "009"
MIGRATION_DESCRIPTION = (
    "Adopt existing AGOL items into sync management "
    "(diff catalogue ↔ AGOL, mark clean/conflict)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"


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
    return [
        v for v in ("001", "002", "003", "004", "005", "006", "007", "008")
        if v not in have
    ]


def _find_adoption_candidates(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT dataset_id, title, agol_item_id, sync_status, agol_format "
        "FROM datasets "
        "WHERE status='active' "
        "  AND agol_item_id IS NOT NULL "
        "  AND sync_status='unpublished' "
        "ORDER BY title"
    )
    return [dict(r) for r in cur.fetchall()]


def run(*, db_path: Path, actor: str = "migration_009") -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    # --- pre-flight ----------------------------------------------------
    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn):
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied.",
                file=sys.stderr,
            )
            return 1

        missing = _check_prereq_migrations(conn)
        if missing:
            print(
                f"ERROR: prerequisite migrations not applied: {missing}.",
                file=sys.stderr,
            )
            return 1

        candidates = _find_adoption_candidates(conn)

    print(f"=== Migration {MIGRATION_VERSION}: adoption ===")
    if not candidates:
        print()
        print(
            "No adoption candidates found "
            "(no active rows with agol_item_id set + sync_status='unpublished')."
        )
        print("Recording migration as applied anyway — schema_migrations row "
              "is the audit record.")
    else:
        print()
        print(f"Adoption candidates ({len(candidates)}):")
        for c in candidates:
            print(f"  {c['dataset_id']}  "
                  f"agol_format={c['agol_format']!r:24}  "
                  f"{c['title']!r}")
        print()
        print("Connecting to AGOL…")

    # --- AGOL connection (only needed if we have candidates) ----------
    if candidates:
        try:
            config = agol_config.load_config()
            gis = agol_sync.get_gis(config)
        except agol_sync.AgolError as exc:
            print(
                f"ERROR: AGOL connection failed: {exc}. "
                f"Run `y2y agol-sync login` and retry.",
                file=sys.stderr,
            )
            return 1
        print(f"Connected: {gis}")
        print()

        # --- per-row adoption -----------------------------------------
        outcomes: dict[str, int] = {"clean": 0, "conflict": 0, "error": 0}
        for c in candidates:
            print(f"Adopting {c['dataset_id']}  ({c['title']!r})…")
            try:
                result = agol_sync.adopt_row(
                    db_path, c["dataset_id"], gis, config, actor=actor,
                )
                outcomes[result.sync_status_after] = (
                    outcomes.get(result.sync_status_after, 0) + 1
                )
                print(f"  → sync_status: {result.sync_status_after}  "
                      f"({result.note})")
            except agol_sync.AgolError as exc:
                print(f"  → ERROR: {exc}", file=sys.stderr)
                outcomes["error"] = outcomes.get("error", 0) + 1

        print()
        print("Adoption summary:")
        for status, n in sorted(outcomes.items()):
            print(f"  {n:2d}  {status}")

    # --- record migration ---------------------------------------------
    with closing(_db.get_connection(db_path)) as conn:
        conn.execute(
            "INSERT INTO schema_migrations "
            "(version, description, applied_at) "
            "VALUES (?, ?, ?)",
            (MIGRATION_VERSION, MIGRATION_DESCRIPTION, utc_now_iso()),
        )
        conn.commit()

    print()
    print(f"OK: migration {MIGRATION_VERSION} applied.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to inventory.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--actor", default="migration_009",
        help="Name to record as the changelog actor (default: migration_009)",
    )
    args = parser.parse_args()
    return run(db_path=args.db, actor=args.actor)


if __name__ == "__main__":
    sys.exit(main())
