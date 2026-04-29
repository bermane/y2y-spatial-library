"""Reconciliation: detect drift between library/ and inventory.xlsx.

Reconciliation produces a timestamped markdown report in ``reports/``.
By default it is **almost-read-only**: the only mutation it performs is
auto-applying drift findings whose files still pass canonical
validators. Other findings (orphans, ghosts, schema violations,
renames) remain steward-actioned via the report or via
``--fix-renames``.

Categories (DESIGN.md §2):

    orphans            Files in library/ with no matching inventory row.
    ghosts             Inventory rows whose file is missing from disk.
    drift              Path matches, content changed, AND file is no
                       longer canonical. Surfaces alongside schema_violations.
    schema_violations  File no longer satisfies format/CRS/naming, or
                       the inventory's status disagrees with disk
                       (e.g. row tombstoned but file still present).
    auto_resolved      Path matches, content changed, but file is still
                       canonical → snapshot auto-refreshed via
                       ``lifecycle.refresh``. Informational only; no
                       further steward action needed.
    renames            (deep only) ghost+orphan pairs where checksums
                       match — i.e. the file was moved within library/.

Two modes (DESIGN.md §3):

    fast    Compares size + mtime only. Default. Cheap.
    deep    Recomputes SHA-256 for every file. Authoritative.

Renames are detected as (same checksum, different path) and require
deep mode because fast mode doesn't recompute checksums.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from . import inventory_manager, utils
from .validators import validate_all

_SPATIAL_SUFFIXES: tuple[str, ...] = (".gpkg", ".tif", ".tiff")


class Finding(NamedTuple):
    dataset_id: str | None
    path: str  # library-relative
    reason: str


class ReconcileResult(NamedTuple):
    report_path: Path
    library_files: int
    inventory_rows: int
    orphans: list[Finding]
    ghosts: list[Finding]
    drift: list[Finding]
    schema_violations: list[Finding]
    renames: list[Finding]
    auto_resolved: list[Finding]
    deep: bool

    @property
    def total_findings(self) -> int:
        # auto_resolved is informational, not an action item.
        return (
            len(self.orphans)
            + len(self.ghosts)
            + len(self.drift)
            + len(self.schema_violations)
            + len(self.renames)
        )


def reconcile(
    library_root: Path,
    inventory_path: Path,
    reports_dir: Path,
    *,
    actor: str,
    changelog_path: Path | None = None,
    deep: bool = False,
    apply_drift: bool = True,
) -> ReconcileResult:
    """Detect drift, auto-resolve canonical-passing drift, write a report.

    For each row whose file is on disk:

    1. Run canonical validators on the file.
    2. Check for drift (size/mtime always; checksum if ``deep``).
    3. If drift detected:
       - **and** validators passed **and** ``apply_drift`` is True:
         call ``lifecycle.refresh`` to update the inventory's snapshot.
         Listed in ``auto_resolved`` (informational).
       - **otherwise**: listed in ``drift`` (action item; the file is
         non-canonical or apply was disabled).

    See DESIGN.md §2 for the broader read-mostly policy and the
    explicit auto-fix exceptions.
    """
    if changelog_path is None:
        changelog_path = inventory_path.parent / "changelog.md"

    # Fail fast if Excel has the inventory open and we'd be auto-applying
    # drift through it. Otherwise a long deep reconcile would do hours of
    # work and crash on the first refresh attempt.
    if apply_drift and inventory_path.exists():
        inventory_manager.assert_not_locked(inventory_path)

    library_root.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    library_files = _walk_library(library_root)
    inventory_rows = inventory_manager.load_inventory(inventory_path)

    by_path: dict[str, dict[str, Any]] = {
        str(row["file_path"]): row for row in inventory_rows if row.get("file_path")
    }

    orphans: list[Finding] = []
    ghosts: list[Finding] = []
    drift: list[Finding] = []
    schema_violations: list[Finding] = []
    auto_resolved: list[Finding] = []

    matched_paths: set[str] = set()

    for row in inventory_rows:
        fp = str(row.get("file_path") or "")
        did = str(row.get("dataset_id") or "") or None
        status = row.get("status")

        if not fp:
            schema_violations.append(Finding(did, "—", "inventory row has empty file_path"))
            continue

        full_path = library_root / fp
        on_disk = full_path.exists()

        if status == "tombstoned":
            matched_paths.add(fp)
            if on_disk:
                schema_violations.append(
                    Finding(did, fp, "row is tombstoned but file is still present in library/")
                )
            continue

        if not on_disk:
            ghosts.append(Finding(did, fp, "inventory row has no matching file on disk"))
            matched_paths.add(fp)
            continue

        # Path matches. Run schema + drift checks.
        matched_paths.add(fp)

        schema_failures = validate_all(full_path)
        for check, reason in schema_failures:
            schema_violations.append(Finding(did, fp, f"{check}: {reason}"))

        drift_reason = _check_drift(full_path, row, deep=deep)
        if drift_reason:
            if not schema_failures and apply_drift and did:
                # Canonical drift → safe to auto-resolve via refresh.
                from . import lifecycle

                try:
                    lifecycle.refresh(
                        inventory_path,
                        changelog_path,
                        library_root,
                        dataset_id=did,
                        actor=actor,
                    )
                    auto_resolved.append(
                        Finding(did, fp, f"snapshot refreshed: {drift_reason}")
                    )
                except (lifecycle.LifecycleError, inventory_manager.InventoryLockedError) as exc:
                    drift.append(
                        Finding(did, fp, f"{drift_reason} (auto-refresh failed: {exc})")
                    )
            else:
                drift.append(Finding(did, fp, drift_reason))

    # Anything in library/ that no inventory row claimed → orphan.
    for rel_path in sorted(library_files):
        if rel_path not in matched_paths:
            orphans.append(Finding(None, rel_path, "file in library/ has no inventory row"))

    # Rename detection (deep only).
    renames: list[Finding] = []
    if deep and ghosts and orphans:
        renames, ghosts, orphans = _detect_renames(library_root, ghosts, orphans, by_path)

    report_path = _write_report(
        reports_dir=reports_dir,
        library_root=library_root,
        inventory_path=inventory_path,
        library_files=library_files,
        inventory_rows=inventory_rows,
        orphans=orphans,
        ghosts=ghosts,
        drift=drift,
        schema_violations=schema_violations,
        renames=renames,
        auto_resolved=auto_resolved,
        deep=deep,
    )

    return ReconcileResult(
        report_path=report_path,
        library_files=len(library_files),
        inventory_rows=len(inventory_rows),
        orphans=orphans,
        ghosts=ghosts,
        drift=drift,
        schema_violations=schema_violations,
        renames=renames,
        auto_resolved=auto_resolved,
        deep=deep,
    )


# --- internal helpers ---------------------------------------------------

def _walk_library(library_root: Path) -> set[str]:
    """Library-relative paths of every spatial file under ``library_root``."""
    files: set[str] = set()
    for path in library_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SPATIAL_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        rel = path.relative_to(library_root)
        files.add(str(rel).replace("\\", "/"))
    return files


def _check_drift(full_path: Path, row: dict[str, Any], *, deep: bool) -> str | None:
    """Return a drift reason or ``None``."""
    fresh_size, fresh_mtime = utils.stat_signature(full_path)

    if int(row.get("size_bytes") or 0) != fresh_size:
        return f"size_bytes drift: inventory={row.get('size_bytes')} disk={fresh_size}"
    if str(row.get("mtime") or "") != fresh_mtime:
        return f"mtime drift: inventory={row.get('mtime')} disk={fresh_mtime}"

    if deep:
        fresh_checksum = utils.sha256_file(full_path)
        if str(row.get("checksum_sha256") or "") != fresh_checksum:
            return "checksum_sha256 drift: file content has changed since ingestion"

    return None


def _detect_renames(
    library_root: Path,
    ghosts: list[Finding],
    orphans: list[Finding],
    by_path: dict[str, dict[str, Any]],
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Match ghosts to orphans by SHA-256.

    Returns ``(renames, remaining_ghosts, remaining_orphans)``.
    """
    orphan_checksums: dict[str, str] = {
        o.path: utils.sha256_file(library_root / o.path) for o in orphans
    }

    renames: list[Finding] = []
    matched_orphan_paths: set[str] = set()
    matched_ghost_dids: set[str] = set()

    for ghost in ghosts:
        row = by_path.get(ghost.path)
        if not row or not ghost.dataset_id:
            continue
        expected = str(row.get("checksum_sha256") or "")
        if not expected:
            continue
        for orphan_path, orphan_checksum in orphan_checksums.items():
            if orphan_path in matched_orphan_paths:
                continue
            if orphan_checksum == expected:
                renames.append(
                    Finding(
                        dataset_id=ghost.dataset_id,
                        path=f"{ghost.path} → {orphan_path}",
                        reason="checksum matches: file appears to have been moved/renamed within library/",
                    )
                )
                matched_orphan_paths.add(orphan_path)
                matched_ghost_dids.add(ghost.dataset_id)
                break

    remaining_ghosts = [g for g in ghosts if g.dataset_id not in matched_ghost_dids]
    remaining_orphans = [o for o in orphans if o.path not in matched_orphan_paths]
    return renames, remaining_ghosts, remaining_orphans


def _write_report(
    *,
    reports_dir: Path,
    library_root: Path,
    inventory_path: Path,
    library_files: set[str],
    inventory_rows: list[dict[str, Any]],
    orphans: list[Finding],
    ghosts: list[Finding],
    drift: list[Finding],
    schema_violations: list[Finding],
    renames: list[Finding],
    auto_resolved: list[Finding],
    deep: bool,
) -> Path:
    mode = "deep" if deep else "fast"
    timestamp_safe = utils.utc_now_compact()
    timestamp_iso = utils.utc_now_iso()
    report_path = reports_dir / f"reconcile_{timestamp_safe}_{mode}.md"

    lines: list[str] = [
        f"# Reconcile report — {timestamp_iso} (mode: {mode})",
        "",
        f"- Library root: `{library_root}`",
        f"- Inventory:    `{inventory_path}`",
        "",
        "## Summary",
        "",
        f"- Files in library/: {len(library_files)}",
        f"- Rows in inventory: {len(inventory_rows)}",
        f"- Orphans: {len(orphans)}",
        f"- Ghosts: {len(ghosts)}",
        f"- Drift (action items): {len(drift)}",
        f"- Schema violations: {len(schema_violations)}",
        f"- Renames detected: {len(renames)}",
        f"- Auto-resolved drift (informational): {len(auto_resolved)}",
        "",
    ]

    def section(title: str, findings: list[Finding]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not findings:
            lines.append("_(none)_")
        else:
            for f in findings:
                did = f.dataset_id or "—"
                lines.append(f"- `{did}` `{f.path}` — {f.reason}")
        lines.append("")

    section("Orphans (in library/ but not in inventory)", orphans)
    section("Ghosts (in inventory but missing from library/)", ghosts)
    section("Drift (file changed AND not canonical — action required)", drift)
    section("Schema violations (file no longer matches its admission criteria)", schema_violations)
    section("Renames (deep mode — file moved within library/)", renames)
    section(
        "Auto-resolved drift (file changed but still canonical — snapshot refreshed)",
        auto_resolved,
    )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
