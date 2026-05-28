"""CLI entry point for the ``y2y`` command.

Subcommands:
    y2y ingest                        Phase 1 scan of queue/incoming/ → pending.xlsx
    y2y ingest --approve              Phase 2 approval — promote ready rows
    y2y update <id> --set k=v ...     Edit non-locked fields on an inventory row
    y2y rename <id> <new-path>        Move a file within library/ + update inventory
    y2y refresh <id>                  Re-snapshot a library file after in-place edit
    y2y tombstone <id> [--reason …]   Mark removed (soft delete) and erase the file
    y2y reconcile [--deep]            Drift report
    y2y reconcile --fix-renames       Interactively confirm rename pairs
    y2y export-xlsx [--out PATH]      Render inventory.db → inventory.xlsx (read-only)

Post-migration to SQLite (2026-04-29): the catalogue lives in
``inventory/inventory.db``. Lifecycle and reconcile commands take the
db path; ``export-xlsx`` is the only command that still produces an
xlsx, and it does so as a generated artifact.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from rich.console import Console

console = Console()


def _default_actor() -> str:
    """Best-effort actor name for changelog entries."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _parse_set_pairs(set_pairs: tuple[str, ...]) -> dict[str, object]:
    """Parse ``--set k=v`` repeated options into a dict."""
    out: dict[str, object] = {}
    for raw in set_pairs:
        if "=" not in raw:
            raise click.BadParameter(f"--set must be 'key=value', got: {raw!r}")
        key, _, value = raw.partition("=")
        out[key.strip()] = value
    return out


def _resolve_paths(root: Path) -> tuple[Path, Path, Path]:
    """Return (db_path, library_root, xlsx_path).

    Post-migration 002 (library restructure): the spatial-data root is
    ``<root>/library/spatial`` rather than ``<root>/library``. file_path
    values in the catalogue are relative to this typed root, matching
    the schema's design intent that future dataset_types
    (``tabular``, ``imagery``, …) get sibling subtrees under ``library/``.
    """
    return (
        root / "inventory" / "inventory.db",
        root / "library" / "spatial",
        root / "inventory" / "inventory.xlsx",
    )


@click.group()
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project root. Defaults to current working directory.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path | None) -> None:
    """Y2Y Spatial Library pipeline CLI."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = (root or Path.cwd()).resolve()


def _auto_export_xlsx(ctx: click.Context) -> None:
    """Regenerate inventory/inventory.xlsx from the current catalogue.

    Called at the success path of every command that mutates
    inventory.db so the rendered xlsx view stays current
    automatically. The xlsx is NOT a source of truth — it's a
    snapshot that stewards open in Excel to inspect catalogue
    state. Without auto-export, the file drifts behind the DB and
    stewards working from it see stale data.

    Silent on failure: we don't want a stuck Excel lock or a
    transient I/O hiccup to break the primary command's
    success-reporting flow. If the export fails, the steward can
    still manually run ``y2y export-xlsx`` later.

    Skipped when the catalogue file doesn't exist (e.g., a CLI
    invocation before the first migration).
    """
    from . import export_xlsx

    try:
        db_path, _, default_xlsx = _resolve_paths(ctx.obj["root"])
        if not db_path.exists():
            return
        export_xlsx.export(db_path, default_xlsx)
    except Exception:
        # Auto-export is best-effort. Don't surface the failure;
        # the primary command's success message already printed.
        pass


# --- ingest ------------------------------------------------------------

@cli.command()
@click.option(
    "--approve",
    "approve_flag",
    is_flag=True,
    help="Phase 2: validate pending.xlsx and promote ready rows to library/.",
)
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor at promotion. Defaults to $USER.",
)
@click.pass_context
def ingest(ctx: click.Context, approve_flag: bool, actor: str | None) -> None:
    """Phase 1 (default): scan queue/incoming/ → queue/processing/pending.xlsx.

    Phase 2 (--approve): validate ready rows in pending.xlsx and promote
    into library/ + inventory.db.
    """
    from . import ingest as ingest_mod
    from . import inventory_manager

    root: Path = ctx.obj["root"]
    incoming = root / "queue" / "incoming"
    processing = root / "queue" / "processing"
    rejected = root / "queue" / "rejected"
    db_path, library, _ = _resolve_paths(root)

    if approve_flag:
        try:
            result = ingest_mod.approve(
                processing, library, db_path,
                actor=actor or _default_actor(),
                auto_push=True,
            )
        except inventory_manager.InventoryLockedError as exc:
            raise click.ClickException(str(exc))
        headline = "yellow" if result.failed else "green"
        console.print(
            f"[{headline}]approve complete[/{headline}] — "
            f"promoted: [bold]{result.promoted}[/bold], "
            f"failed: [bold]{result.failed}[/bold], "
            f"skipped: [bold]{result.skipped}[/bold] (ready=FALSE)"
        )
        if result.promoted:
            console.print(f"  catalogue:    [cyan]{db_path}[/cyan]")
        if result.pending_deleted:
            console.print("  pending sheet drained and deleted")
        elif result.pending_path.exists():
            console.print(f"  remaining in: [cyan]{result.pending_path}[/cyan]")
        # When rows failed validation, point the steward at the
        # _validation_error column + remind them that ready was
        # flipped back to FALSE (so the row won't auto-retry on the
        # next approve until they re-check the box).
        if result.failed:
            console.print(
                f"  [yellow]→ See the [bold]_validation_error[/bold] "
                f"column in pending.xlsx for the failure reason. Fix "
                f"the underlying issue, set [bold]ready[/bold] back to "
                f"[bold]TRUE[/bold], and run `y2y ingest --approve` "
                f"to try again.[/yellow]"
            )
        # Rev 3: surface VTPK reminders for newly-approved VTL rows
        # that don't yet have a VTPK on disk. Non-blocking; reconcile
        # will keep flagging them until the steward acts.
        if result.vtpk_reminders:
            console.print(
                f"[yellow]VTPK needed[/yellow] for "
                f"[bold]{len(result.vtpk_reminders)}[/bold] approved "
                f"row(s) targeted as vector-tile-layer:"
            )
            for rem in result.vtpk_reminders:
                console.print(
                    f"  · {rem.dataset_id}  (from {rem.gpkg_relative_path})"
                )
                console.print(
                    f"    Build VTPK in ArcGIS Pro from this GPKG, save as "
                    f"`{rem.expected_vtpk_path.name}`, drop in "
                    f"`queue/incoming/`, then run `y2y ingest`."
                )
        # Auto-export the rendered xlsx so it stays current.
        if result.promoted:
            _auto_export_xlsx(ctx)
        return

    scan_result = ingest_mod.scan(
        incoming, processing, rejected,
        library_root=library, db_path=db_path,
        actor=actor or _default_actor(),
    )

    console.print(
        f"[green]scan complete[/green] — "
        f"accepted: [bold]{scan_result.accepted}[/bold], "
        f"rejected: [bold]{scan_result.rejected}[/bold]"
    )
    if scan_result.accepted:
        console.print(f"  review sheet: [cyan]{scan_result.pending_path}[/cyan]")
    if scan_result.rejected:
        console.print(f"  rejections:   [cyan]{rejected}[/cyan] (see .rejected.yaml sidecars)")

    # --- VTPK ingest results (rev 3) ---
    moved: list = []
    if scan_result.vtpk_results:
        moved = [r for r in scan_result.vtpk_results if r.status == "moved"]
        problems = [r for r in scan_result.vtpk_results if r.status != "moved"]
        if moved:
            console.print(
                f"[green]VTPKs ingested[/green]: [bold]{len(moved)}[/bold]"
            )
            for r in moved:
                console.print(f"  · {r.message}")
        if problems:
            console.print(
                f"[yellow]VTPKs needing attention[/yellow]: "
                f"[bold]{len(problems)}[/bold]"
            )
            for r in problems:
                console.print(f"  · [{r.status}] {r.vtpk_path.name}: {r.message}")

    # Auto-export xlsx if any VTPK ingest moved a file (changelog
    # was appended). Phase 1 scan itself only writes to
    # pending.xlsx, not the catalogue, so we skip auto-export when
    # only source datasets were staged.
    if moved:
        _auto_export_xlsx(ctx)


# --- lifecycle: update / rename / refresh / tombstone -----------------

@cli.command()
@click.argument("dataset_id")
@click.option(
    "--set", "set_pairs",
    multiple=True,
    help="Field to update, in 'key=value' form. May be repeated.",
)
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def update(ctx: click.Context, dataset_id: str, set_pairs: tuple[str, ...], actor: str | None) -> None:
    """Update non-locked fields on an inventory row."""
    from . import lifecycle

    if not set_pairs:
        raise click.UsageError("Provide at least one --set key=value")

    fields = _parse_set_pairs(set_pairs)
    db_path, library, _ = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.update(
            db_path,
            dataset_id=dataset_id, fields=fields,
            actor=actor or _default_actor(),
            library_root=library, auto_push=True,
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]update complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold] fields=[bold]{list(fields)}[/bold]"
    )
    console.print(f"  date_modified: [cyan]{row.get('date_modified')}[/cyan]")
    _auto_export_xlsx(ctx)


@cli.command()
@click.argument("dataset_id")
@click.argument("new_path")
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def rename(ctx: click.Context, dataset_id: str, new_path: str, actor: str | None) -> None:
    """Rename/move a file within library/ and update its inventory row."""
    from . import lifecycle

    db_path, library, _ = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.rename(
            db_path, library,
            dataset_id=dataset_id, new_path=new_path,
            actor=actor or _default_actor(),
            auto_push=True,
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]rename complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold] file_path=[cyan]{row['file_path']}[/cyan]"
    )
    _auto_export_xlsx(ctx)


@cli.command()
@click.argument("dataset_id")
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def refresh(ctx: click.Context, dataset_id: str, actor: str | None) -> None:
    """Re-stat a library file after an in-place edit and update the snapshot.

    The file must still pass canonical validators — refresh refuses to
    record bad state in the inventory.
    """
    from . import lifecycle

    db_path, library, _ = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.refresh(
            db_path, library,
            dataset_id=dataset_id, actor=actor or _default_actor(),
            auto_push=True,
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]refresh complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold]"
    )
    console.print(f"  date_modified: [cyan]{row.get('date_modified')}[/cyan]")
    _auto_export_xlsx(ctx)


@cli.command()
@click.argument("dataset_id")
@click.option("--reason", default=None, help="Optional reason recorded in the changelog.")
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.confirmation_option(prompt="Tombstone is irreversible — proceed?")
@click.pass_context
def tombstone(
    ctx: click.Context,
    dataset_id: str,
    reason: str | None,
    actor: str | None,
) -> None:
    """Soft-delete a row (status=tombstoned) and erase its file from library/."""
    from . import lifecycle

    db_path, library, _ = _resolve_paths(ctx.obj["root"])

    try:
        lifecycle.tombstone(
            db_path, library,
            dataset_id=dataset_id, actor=actor or _default_actor(), reason=reason,
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[yellow]tombstoned[/yellow] dataset_id=[bold]{dataset_id}[/bold] "
        "(file deleted, inventory row preserved as audit record)"
    )
    _auto_export_xlsx(ctx)


# --- reconcile ---------------------------------------------------------

@cli.command()
@click.option("--deep", is_flag=True, help="Recompute SHA-256 checksums instead of stat-only.")
@click.option(
    "--fix-renames",
    is_flag=True,
    help="After reconcile, prompt to confirm and apply each detected rename. Implies --deep.",
)
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor when applying fixes. Defaults to $USER.",
)
@click.pass_context
def reconcile(
    ctx: click.Context,
    deep: bool,
    fix_renames: bool,
    actor: str | None,
) -> None:
    """Reconcile library/ against inventory.db and write a timestamped report."""
    from . import lifecycle, reconcile as reconcile_mod

    root: Path = ctx.obj["root"]
    db_path, library, _ = _resolve_paths(root)
    reports = root / "reports"

    if fix_renames:
        deep = True  # rename detection requires deep mode

    actor_name = actor or _default_actor()
    result = reconcile_mod.reconcile(
        library, db_path, reports,
        actor=actor_name, deep=deep,
    )

    mode = "deep" if deep else "fast"
    headline = "green" if result.total_findings == 0 else "yellow"
    console.print(
        f"[{headline}]reconcile complete[/{headline}] (mode: {mode}) — "
        f"library files: [bold]{result.library_files}[/bold], "
        f"inventory rows: [bold]{result.inventory_rows}[/bold]"
    )
    console.print(
        f"  orphans: [bold]{len(result.orphans)}[/bold], "
        f"ghosts: [bold]{len(result.ghosts)}[/bold], "
        f"drift: [bold]{len(result.drift)}[/bold], "
        f"schema violations: [bold]{len(result.schema_violations)}[/bold], "
        f"renames: [bold]{len(result.renames)}[/bold]"
    )
    # Rev 3: VTPK invariants for vector-tile-layer rows.
    if result.vtpk_missing or result.vtpk_stale or result.vtpk_orphan:
        console.print(
            f"  VTPK missing: [bold]{len(result.vtpk_missing)}[/bold], "
            f"VTPK stale: [bold]{len(result.vtpk_stale)}[/bold], "
            f"VTPK orphan: [bold]{len(result.vtpk_orphan)}[/bold]"
        )
    if result.auto_resolved:
        console.print(
            f"  [dim]auto-resolved drift: {len(result.auto_resolved)} "
            f"(snapshot refreshed; see changelog)[/dim]"
        )
    console.print(f"  report: [cyan]{result.report_path}[/cyan]")

    # Reconcile mutates the catalogue when it auto-resolves drift
    # via lifecycle.refresh. Auto-export so the rendered xlsx
    # picks up the refreshed snapshots.
    if result.auto_resolved:
        _auto_export_xlsx(ctx)

    if not fix_renames:
        return

    if not result.renames:
        console.print("[green]no rename candidates to confirm[/green]")
        return

    console.print()
    console.print(f"[bold]Confirm {len(result.renames)} rename candidate(s):[/bold]")
    actor_name = actor or _default_actor()
    applied = 0
    skipped = 0

    for finding in result.renames:
        if "→" not in finding.path:
            continue
        old_str, new_str = (s.strip() for s in finding.path.split("→", 1))

        console.print(f"  [cyan]{finding.dataset_id}[/cyan]: {old_str} → {new_str}")
        if not click.confirm("  apply this rename?", default=False):
            skipped += 1
            continue
        try:
            lifecycle.rename(
                db_path, library,
                dataset_id=finding.dataset_id or "",
                new_path=new_str, actor=actor_name,
            )
            applied += 1
        except lifecycle.LifecycleError as exc:
            console.print(f"  [red]failed:[/red] {exc}")
            skipped += 1

    console.print(
        f"[green]fix-renames done[/green] — applied: [bold]{applied}[/bold], "
        f"skipped: [bold]{skipped}[/bold]"
    )
    if applied:
        _auto_export_xlsx(ctx)


# --- export-xlsx -------------------------------------------------------

@cli.command("export-xlsx")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output xlsx path. Defaults to inventory/inventory.xlsx under --root.",
)
@click.pass_context
def export_xlsx_cmd(ctx: click.Context, out_path: Path | None) -> None:
    """Render the SQLite catalogue as a read-only inventory.xlsx.

    The xlsx is a steward-friendly view; it is **not** a source of
    truth. Editing it changes nothing in the catalogue. Re-export
    overwrites it.
    """
    from . import export_xlsx, inventory_manager

    db_path, _, default_xlsx = _resolve_paths(ctx.obj["root"])
    target = out_path or default_xlsx

    if not db_path.exists():
        raise click.ClickException(
            f"catalogue not found at {db_path}. "
            f"Run pipeline/migrations/001_xlsx_to_sqlite.py first."
        )

    try:
        n_rows, n_log = export_xlsx.export(db_path, target)
    except inventory_manager.InventoryLockedError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]export-xlsx complete[/green] — "
        f"datasets: [bold]{n_rows}[/bold], "
        f"changelog: [bold]{n_log}[/bold]"
    )
    console.print(f"  wrote: [cyan]{target}[/cyan]")
    console.print(
        "  [dim]This file is regenerated; it is not the source of truth.[/dim]"
    )


# --- agol-sync ---------------------------------------------------------
#
# AGOL integration sub-group. See DESIGN.md §15 for the design.
#
# Phase A ships login + init-categories + status (read-only). Phase B
# adds push. Phase C adds adopt + reconcile + auto-sync triggers.
# Phase D adds pull + conflict. Phase E adds unpublish.

@cli.group("agol-sync")
def agol_sync_group() -> None:
    """Catalogue ↔ ArcGIS Online integration commands."""


@agol_sync_group.command("login")
def agol_sync_login() -> None:
    """Interactive OAuth login; saves profile for subsequent agol-sync runs.

    Requires the OAuth client_id of your Y2Y AGOL app to be set in
    the environment variable ``Y2Y_AGOL_CLIENT_ID`` (or in
    ``~/.y2y/agol_config.yaml``). The browser will open for the
    consent flow once; thereafter credentials are cached at
    ``~/.arcgis/profile_y2y``.
    """
    from . import agol_config, agol_sync

    cfg = agol_config.load_config()
    try:
        gis = agol_sync.login_interactive(cfg)
    except agol_sync.AgolAuthError as exc:
        raise click.ClickException(str(exc))

    user = getattr(gis, "users", None)
    me = user.me if user else None
    console.print(
        f"[green]agol-sync login complete[/green] — "
        f"profile [bold]{cfg.profile_name}[/bold] cached"
    )
    if me is not None:
        console.print(f"  authenticated as: [cyan]{me.username}[/cyan] ({me.fullName})")
        console.print(f"  org: [cyan]{getattr(me, 'orgId', 'unknown')}[/cyan]")


@agol_sync_group.command("init-categories")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would change but don't write to AGOL.",
)
@click.option(
    "--yes", "skip_confirm",
    is_flag=True,
    help="Skip the confirmation prompt (use for scripting).",
)
def agol_sync_init_categories(dry_run: bool, skip_confirm: bool) -> None:
    """Rewrite the AGOL org's category schema to match the catalogue typology.

    Builds the canonical schema from pipeline/taxonomy.py (10 top-level
    categories + 7 Species subcategories) and writes it to the org.
    Any pre-existing categories not in the canonical typology are
    orphaned — items tagged with them lose those tags.

    Requires org-admin privileges. Confirms before writing unless
    ``--yes`` is passed. Use ``--dry-run`` to preview the diff without
    making changes.
    """
    from . import agol_config, agol_sync

    cfg = agol_config.load_config()
    try:
        gis = agol_sync.get_gis(cfg)
        # First do a dry-run probe so we can show the diff.
        diff = agol_sync.ensure_org_categories(gis, cfg, apply=False)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    if not diff.will_add and not diff.will_orphan:
        console.print(
            "[green]AGOL category schema already matches the catalogue typology[/green] "
            "— no changes needed"
        )
        return

    # Show the proposed diff.
    console.print("[bold]Proposed AGOL category schema changes:[/bold]")
    if diff.will_add:
        console.print(f"  [green]+ {len(diff.will_add)} to add:[/green]")
        for c in diff.will_add:
            console.print(f"      + {c}")
    if diff.will_orphan:
        console.print(
            f"  [red]- {len(diff.will_orphan)} to orphan "
            f"(items tagged with these will lose those tags):[/red]"
        )
        for c in diff.will_orphan:
            console.print(f"      - {c}")
    if diff.unchanged:
        console.print(
            f"  [dim]= {len(diff.unchanged)} unchanged: "
            f"{', '.join(diff.unchanged)}[/dim]"
        )

    if dry_run:
        console.print()
        console.print("[yellow]--dry-run: not writing[/yellow]")
        return

    console.print()
    if not skip_confirm and not click.confirm(
        "Rewrite the AGOL category schema with these changes?",
        default=False,
    ):
        console.print("[yellow]aborted[/yellow]")
        return

    try:
        applied = agol_sync.ensure_org_categories(gis, cfg, apply=True)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]wrote AGOL category schema[/green] — "
        f"added {len(applied.will_add)}, orphaned {len(applied.will_orphan)}"
    )


@agol_sync_group.command("status")
@click.option(
    "--deep",
    is_flag=True,
    help=(
        "Also query AGOL for each linked item's modified timestamp "
        "and flag rows where AGOL has drifted past the catalogue's "
        "last_synced_at."
    ),
)
@click.pass_context
def agol_sync_status(ctx: click.Context, deep: bool) -> None:
    """Show catalogue ↔ AGOL sync state for every active dataset."""
    from . import agol_config, agol_sync, inventory_manager

    db_path, _, _ = _resolve_paths(ctx.obj["root"])
    cfg = agol_config.load_config()

    rows = inventory_manager.load_inventory(db_path)
    active = [r for r in rows if r.get("status") == "active"]

    from collections import Counter
    counts = Counter(r.get("sync_status") for r in active)

    console.print(
        f"[bold]Catalogue ↔ AGOL status[/bold] — "
        f"{len(active)} active dataset(s)"
    )
    for status, n in sorted(counts.items(), key=lambda x: (x[0] or "")):
        console.print(f"  {n:3d}  {status}")
    console.print()
    # Show per-target breakdown so the steward can see how many of
    # each kind will be published when sync fires.
    target_counts = Counter(r.get("agol_format") for r in active)
    console.print("[bold]By agol_format:[/bold]")
    for target, n in sorted(target_counts.items(), key=lambda x: (x[0] or "")):
        console.print(f"  {n:3d}  {target}")

    if not deep:
        return

    # Deep mode: for every row that already has an agol_item_id,
    # fetch the AGOL item's modified timestamp and compare against
    # last_synced_at.
    linked = [r for r in active if r.get("agol_item_id")]
    if not linked:
        console.print()
        console.print(
            "[dim]--deep: no rows with agol_item_id yet; nothing to query[/dim]"
        )
        return

    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    console.print()
    console.print(
        f"[bold]--deep: AGOL modified-timestamp check on "
        f"{len(linked)} linked row(s):[/bold]"
    )
    pull_candidates: list[agol_sync.PullCandidate] = []
    for row in linked:
        item_id = row["agol_item_id"]
        last_synced = row.get("last_synced_at")
        try:
            item = gis.content.get(item_id)
        except Exception as exc:  # pragma: no cover — defensive
            console.print(
                f"  [red]ERROR[/red] {row['dataset_id']} item={item_id}: {exc}"
            )
            continue
        if item is None:
            console.print(
                f"  [yellow]MISSING ON AGOL[/yellow] {row['dataset_id']} "
                f"item={item_id} (item was deleted or revoked)"
            )
            continue
        # AGOL's `modified` is a Unix millisecond timestamp.
        agol_modified = _format_agol_timestamp(getattr(item, "modified", None))
        marker = ""
        if last_synced and agol_modified and agol_modified > last_synced:
            marker = "  [bold yellow]DRIFTED ON AGOL[/bold yellow]"
            pull_candidates.append(agol_sync.PullCandidate(
                dataset_id=row["dataset_id"],
                agol_item_id=item_id,
                title=row.get("title") or "",
                last_synced_at=last_synced,
                agol_modified_at=agol_modified,
            ))
        console.print(
            f"  {row['dataset_id']}  last_synced={last_synced or '—'}  "
            f"agol_modified={agol_modified or '—'}{marker}"
        )

    if pull_candidates:
        console.print()
        console.print(
            f"[yellow]{len(pull_candidates)} row(s) need a pull "
            f"(Phase D — `y2y agol-sync pull <id>`)[/yellow]"
        )


def _format_agol_timestamp(ms: int | None) -> str | None:
    """AGOL's item.modified is Unix ms; render as ISO-8601 Z to match catalogue."""
    if ms is None:
        return None
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# --- agol-sync push ----------------------------------------------------

_VALID_TARGETS = ("feature-layer", "vector-tile-layer", "imagery-layer")
_VALID_SHARING = ("private", "org", "public")


@agol_sync_group.command("push")
@click.argument("dataset_id", required=False)
@click.option(
    "--all-dirty",
    is_flag=True,
    help="Push every row whose sync_status='pending_push' (no <dataset_id> argument).",
)
@click.option(
    "--all-unpublished",
    "all_unpublished",
    is_flag=True,
    help="Push every row whose sync_status='unpublished' — the initial "
         "bulk publish of the backlog (no <dataset_id> argument).",
)
@click.option(
    "--target",
    type=click.Choice(_VALID_TARGETS),
    default=None,
    help=(
        "Override the row's persisted agol_format for this one invocation. "
        "Use for ad-hoc testing; for durable per-dataset changes use "
        "`y2y update <id> --set agol_format=...`. Not allowed with batch flags."
    ),
)
@click.option(
    "--sharing",
    type=click.Choice(_VALID_SHARING),
    default=None,
    help=(
        "Override the default sharing (org + Conservation Atlas group). "
        "private = owner only; org = org-visible, no group; public = "
        "world-visible, no group."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be pushed without contacting AGOL.",
)
@click.option(
    "--actor",
    default=None,
    help="Name recorded in the changelog. Defaults to $USER.",
)
@click.pass_context
def agol_sync_push(
    ctx: click.Context,
    dataset_id: str | None,
    all_dirty: bool,
    all_unpublished: bool,
    target: str | None,
    sharing: str | None,
    dry_run: bool,
    actor: str | None,
) -> None:
    """Push a single dataset (or a batch) to AGOL.

    \b
    USAGE:
      y2y agol-sync push <dataset_id>             — push one row
      y2y agol-sync push --all-dirty              — push every pending_push row
      y2y agol-sync push --all-unpublished        — initial bulk publish of the backlog
      y2y agol-sync push <id> --dry-run           — preview without contacting AGOL
      y2y agol-sync push <id> --target vector-tile-layer
                                                  — ad-hoc target override
      y2y agol-sync push --all-unpublished --dry-run

    The publish target for each row comes from the catalogue's
    `agol_format` column unless overridden via --target. Sharing
    defaults to org + Y2Y Conservation Atlas group; --sharing
    overrides per invocation.
    """
    from . import agol_config, agol_sync

    if all_dirty and all_unpublished:
        raise click.UsageError(
            "Pass only one of --all-dirty / --all-unpublished."
        )
    batch = all_dirty or all_unpublished
    if batch and dataset_id is not None:
        raise click.UsageError(
            "Cannot combine <dataset_id> argument with a batch flag "
            "(--all-dirty / --all-unpublished)."
        )
    if batch and target is not None:
        raise click.UsageError(
            "--target is per-row; not allowed with a batch flag. "
            "Use `y2y update <id> --set agol_format=...` to make a "
            "persistent change to a row's target."
        )
    if not batch and dataset_id is None:
        raise click.UsageError(
            "Either give a <dataset_id> or pass --all-dirty / "
            "--all-unpublished."
        )

    db_path, library, _ = _resolve_paths(ctx.obj["root"])
    cfg = agol_config.load_config()
    actor_name = actor or _default_actor()
    cache_dir = ctx.obj["root"] / ".y2y"

    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    if batch:
        batch_fn = (
            agol_sync.push_all_dirty if all_dirty
            else agol_sync.push_all_unpublished
        )
        results = batch_fn(
            db_path, gis, cfg,
            library_root=library, actor=actor_name,
            sharing_override=sharing, dry_run=dry_run,
        )
        _print_push_results(results, dry_run=dry_run)
        # Auto-export if any row actually mutated the catalogue.
        if not dry_run and any(r.error is None for r in results):
            _auto_export_xlsx(ctx)
        return

    # Single-row push.
    try:
        result = agol_sync.push(
            db_path, dataset_id, gis, cfg,
            library_root=library, actor=actor_name,
            target_override=target, sharing_override=sharing,
            dry_run=dry_run, cache_dir=cache_dir,
        )
    except agol_sync.AgolToolingError as exc:
        # arcpy missing; surface as a click error with the SDK's
        # clear instructions.
        raise click.ClickException(str(exc))
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    _print_push_results([result], dry_run=dry_run)
    # push mutates the catalogue's agol_item_id / sync_status /
    # last_synced_at; auto-export so the xlsx view reflects it.
    # Dry-runs don't mutate, skip.
    if not dry_run:
        _auto_export_xlsx(ctx)


def _print_push_results(results, *, dry_run: bool) -> None:
    """Render a per-result line for `y2y agol-sync push` output."""
    if not results:
        console.print("[yellow]no rows to push[/yellow]")
        return

    n_ok = sum(1 for r in results if r.error is None)
    n_err = len(results) - n_ok

    if dry_run:
        console.print(
            f"[bold]Dry-run: would push {len(results)} row(s)[/bold]"
        )
    else:
        headline = "green" if n_err == 0 else "yellow"
        console.print(
            f"[{headline}]push complete[/{headline}] — "
            f"ok: [bold]{n_ok}[/bold], failed: [bold]{n_err}[/bold]"
        )

    for r in results:
        if r.error:
            console.print(
                f"  [red]✗[/red] {r.dataset_id}: {r.error}"
            )
        else:
            marker = "[dim](dry-run)[/dim]" if dry_run else ""
            console.print(
                f"  [green]✓[/green] {r.dataset_id} → "
                f"item={r.agol_item_id or '—'}  status={r.sync_status_after}  {marker}"
            )
            if dry_run and r.note:
                # Indent the multi-line dry-run plan.
                for line in r.note.splitlines():
                    console.print(f"      {line}")


@agol_sync_group.command("adopt")
@click.argument("dataset_id")
@click.option(
    "--actor", default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def agol_sync_adopt(
    ctx: click.Context, dataset_id: str, actor: str | None,
) -> None:
    """Bring one pre-existing AGOL item under catalogue sync management.

    For a row whose ``agol_item_id`` is set but ``sync_status`` is
    still ``'unpublished'`` (typically published to AGOL manually
    before this integration existed), fetch the AGOL item and diff
    it against the catalogue field-by-field. Mark the row
    ``'clean'`` (no drift) or ``'conflict'`` (drift; resolve via
    Phase D pull). Adoption never mutates AGOL.

    Migration 009 runs this same logic across every adoption
    candidate at once. Use this ad-hoc command for one-off rows
    that get an ``agol_item_id`` later (e.g. a steward manually
    publishes another item and wires it into the catalogue).
    """
    from . import agol_config, agol_sync

    db_path, _, _ = _resolve_paths(ctx.obj["root"])
    cfg = agol_config.load_config()

    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    try:
        result = agol_sync.adopt_row(
            db_path, dataset_id, gis, cfg,
            actor=actor or _default_actor(),
        )
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    colour = "green" if result.sync_status_after == "clean" else "yellow"
    console.print(
        f"[{colour}]adopt complete[/{colour}] — "
        f"{result.dataset_id} → sync_status={result.sync_status_after}"
    )
    console.print(f"  {result.note}")
    _auto_export_xlsx(ctx)


@agol_sync_group.command("reconcile")
@click.option(
    "--dry-run", is_flag=True,
    help="Compute outcomes + write report without mutating the catalogue "
         "or attempting AGOL pushes.",
)
@click.option(
    "--actor", default=None,
    help="Name to record as the changelog actor. Defaults to $USER "
         "(use 'reconcile-cron' from scheduled runs).",
)
@click.pass_context
def agol_sync_reconcile(
    ctx: click.Context, dry_run: bool, actor: str | None,
) -> None:
    """Bidirectional catalogue ↔ AGOL reconcile.

    For every active row this command:

    * Pushes ``pending_push`` rows (failures mark ``error``).
    * Checks ``clean`` rows for AGOL-side drift via the item's
      ``modified`` timestamp; flags drifted rows ``pending_pull``
      for Phase-D resolution.
    * Retries ``error`` rows once.
    * Skips ``unpublished`` / ``pending_pull`` / ``conflict``.

    Writes a markdown report to ``reports/agol_reconcile_<ts>.md``.
    Intended for a weekly cron / launchd schedule (see DESIGN.md
    §15 for sample configs).
    """
    from . import agol_config, agol_sync

    db_path, library, _ = _resolve_paths(ctx.obj["root"])
    reports_dir = ctx.obj["root"] / "reports"

    cfg = agol_config.load_config()
    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    report = agol_sync.reconcile_bidirectional(
        db_path, gis, cfg,
        library_root=library,
        actor=actor or _default_actor(),
        reports_dir=reports_dir,
        dry_run=dry_run,
    )

    mode = "dry-run" if dry_run else "applied"
    console.print(
        f"[green]agol-sync reconcile complete ({mode})[/green] — "
        f"[bold]{len(report.outcomes)}[/bold] rows processed"
    )
    for bucket in (
        "pushed", "pulled_flag", "error_retry_ok", "clean_confirmed",
        "push_failed", "error_retry_failed", "skipped",
    ):
        n = report.counts_by_bucket.get(bucket, 0)
        if n:
            colour = "red" if "failed" in bucket else (
                "yellow" if bucket == "pulled_flag" else "cyan"
            )
            console.print(f"  [{colour}]{n:3d}[/{colour}]  {bucket}")
    console.print(f"  report: [cyan]{report.report_path}[/cyan]")
    if not dry_run:
        _auto_export_xlsx(ctx)


@agol_sync_group.command("pull")
@click.argument("dataset_id", required=False)
@click.option(
    "--all-pending", "all_pending", is_flag=True,
    help="Surface diffs for every row with sync_status='pending_pull'. "
         "Mutually exclusive with a dataset_id argument and with "
         "--accept / --reject (batch mode never auto-resolves).",
)
@click.option(
    "--accept", "accept", is_flag=True,
    help="Absorb AGOL's drifted text fields into the catalogue "
         "(title / summary / description / tags / acknowledgements / "
         "terms_of_use). The `categories` diff is filesystem-bound "
         "and skipped with an internal_notes annotation; change the "
         "category with `y2y rename` if needed. Marks sync_status='clean'.",
)
@click.option(
    "--reject", "reject", is_flag=True,
    help="Re-push catalogue values to AGOL, overwriting whatever "
         "drifted there. Flips sync_status to pending_push first and "
         "delegates to push().",
)
@click.option(
    "--actor", default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def agol_sync_pull(
    ctx: click.Context,
    dataset_id: str | None,
    all_pending: bool,
    accept: bool,
    reject: bool,
    actor: str | None,
) -> None:
    """Pull AGOL state back into the catalogue.

    Three single-row modes (gated by --accept / --reject):

    \b
    * No flag (default):     fetch + diff + mark sync_status='conflict',
                             log structured diff for steward review.
    * --accept:              catalogue absorbs AGOL's text-field values.
    * --reject:              re-push catalogue values to AGOL.

    Batch mode (--all-pending): iterates every pending_pull row and
    surfaces its diff (no auto-resolution). The steward then runs
    `pull <id> --accept` or `--reject` per row.
    """
    from . import agol_config, agol_sync

    if accept and reject:
        raise click.UsageError("--accept and --reject are mutually exclusive")
    if all_pending and (accept or reject):
        raise click.UsageError(
            "--all-pending does not accept --accept / --reject; "
            "batch mode never auto-resolves. Resolve each row "
            "individually with `pull <id> --accept|--reject`."
        )
    if all_pending and dataset_id:
        raise click.UsageError(
            "--all-pending and a dataset_id argument are mutually exclusive"
        )
    if not all_pending and not dataset_id:
        raise click.UsageError(
            "either pass a dataset_id or use --all-pending"
        )

    db_path, library, _ = _resolve_paths(ctx.obj["root"])
    cfg = agol_config.load_config()
    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    resolution: str | None = None
    if accept:
        resolution = "accept_agol"
    elif reject:
        resolution = "reject_agol"

    actor_name = actor or _default_actor()

    if all_pending:
        results = agol_sync.pull_all_pending(
            db_path, gis, cfg,
            library_root=library, actor=actor_name,
        )
        if not results:
            console.print(
                "[green]pull --all-pending complete[/green] — "
                "no rows with sync_status='pending_pull'."
            )
            return
        console.print(
            f"[green]pull --all-pending complete[/green] — "
            f"[bold]{len(results)}[/bold] row(s) surfaced:"
        )
        for r in results:
            colour = "red" if r.error else (
                "yellow" if r.sync_status_after == "conflict" else "cyan"
            )
            console.print(
                f"  [{colour}]{r.dataset_id}[/{colour}] → "
                f"{r.sync_status_after}: {r.note}"
            )
        _auto_export_xlsx(ctx)
        return

    # Single-row mode.
    try:
        result = agol_sync.pull(
            db_path, dataset_id, gis, cfg,
            library_root=library, actor=actor_name,
            resolution=resolution,
        )
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    colour = (
        "red" if result.error else
        "green" if result.sync_status_after == "clean" else
        "yellow"
    )
    console.print(
        f"[{colour}]pull complete[/{colour}] — "
        f"{result.dataset_id} → sync_status={result.sync_status_after}"
    )
    console.print(f"  {result.note}")
    _auto_export_xlsx(ctx)


@agol_sync_group.command("unpublish")
@click.argument("dataset_id")
@click.option(
    "--actor", default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.confirmation_option(
    prompt="Permanently delete this dataset's AGOL item(s)? The "
           "catalogue row stays active and can be re-pushed.",
)
@click.pass_context
def agol_sync_unpublish(
    ctx: click.Context, dataset_id: str, actor: str | None,
) -> None:
    """Delete a dataset's AGOL item(s) and clear the catalogue link.

    Permanently removes the AGOL service + any linked source item,
    then clears ``agol_item_id`` / ``agol_published_at`` /
    ``last_synced_at`` and sets ``sync_status='unpublished'``. The
    catalogue row itself stays active — only the AGOL representation
    is torn down. Re-publish any time with `y2y agol-sync push`.
    """
    from . import agol_config, agol_sync

    db_path, _, _ = _resolve_paths(ctx.obj["root"])
    cfg = agol_config.load_config()
    try:
        gis = agol_sync.get_gis(cfg)
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    try:
        result = agol_sync.unpublish(
            db_path, dataset_id, gis, cfg,
            actor=actor or _default_actor(),
        )
    except agol_sync.AgolError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]unpublish complete[/green] — "
        f"{result.dataset_id} → sync_status={result.sync_status_after}"
    )
    console.print(f"  {result.note}")
    _auto_export_xlsx(ctx)


if __name__ == "__main__":
    cli()
