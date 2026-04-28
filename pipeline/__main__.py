"""CLI entry point for the ``y2y`` command.

Subcommands:
    y2y ingest                        Phase 1 scan of queue/incoming/ → pending.xlsx
    y2y ingest --approve              Phase 2 approval — promote ready rows
    y2y update <id> --set k=v ...     Edit non-locked fields on an inventory row
    y2y rename <id> <new-path>        Move a file within library/ + update inventory
    y2y tombstone <id> [--reason …]   Mark removed (soft delete) and erase the file
    y2y reconcile [--deep]            Drift report
    y2y reconcile --fix-renames       Interactively confirm rename pairs
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
    into library/ + inventory.xlsx.
    """
    from . import ingest as ingest_mod

    root: Path = ctx.obj["root"]
    incoming = root / "queue" / "incoming"
    processing = root / "queue" / "processing"
    rejected = root / "queue" / "rejected"
    library = root / "library"
    inventory = root / "inventory" / "inventory.xlsx"
    changelog = root / "inventory" / "changelog.md"

    if approve_flag:
        result = ingest_mod.approve(
            processing, library, inventory, changelog,
            actor=actor or _default_actor(),
        )
        console.print(
            f"[green]approve complete[/green] — "
            f"promoted: [bold]{result.promoted}[/bold], "
            f"failed: [bold]{result.failed}[/bold], "
            f"skipped: [bold]{result.skipped}[/bold] (ready=FALSE)"
        )
        if result.promoted:
            console.print(f"  inventory:    [cyan]{inventory}[/cyan]")
            console.print(f"  changelog:    [cyan]{changelog}[/cyan]")
        if result.pending_deleted:
            console.print("  pending sheet drained and deleted")
        elif result.pending_path.exists():
            console.print(f"  remaining in: [cyan]{result.pending_path}[/cyan]")
        return

    scan_result = ingest_mod.scan(incoming, processing, rejected)

    console.print(
        f"[green]scan complete[/green] — "
        f"accepted: [bold]{scan_result.accepted}[/bold], "
        f"rejected: [bold]{scan_result.rejected}[/bold]"
    )
    if scan_result.accepted:
        console.print(f"  review sheet: [cyan]{scan_result.pending_path}[/cyan]")
    if scan_result.rejected:
        console.print(f"  rejections:   [cyan]{rejected}[/cyan] (see .rejected.yaml sidecars)")


# --- lifecycle: update / rename / tombstone ----------------------------

def _resolve_paths(root: Path) -> tuple[Path, Path, Path]:
    """Return (inventory_path, changelog_path, library_root)."""
    return (
        root / "inventory" / "inventory.xlsx",
        root / "inventory" / "changelog.md",
        root / "library",
    )


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
    inventory, changelog, _ = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.update(
            inventory, changelog,
            dataset_id=dataset_id, fields=fields,
            actor=actor or _default_actor(),
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]update complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold] fields=[bold]{list(fields)}[/bold]"
    )
    console.print(f"  date_modified: [cyan]{row.get('date_modified')}[/cyan]")


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

    inventory, changelog, library = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.rename(
            inventory, changelog, library,
            dataset_id=dataset_id, new_path=new_path,
            actor=actor or _default_actor(),
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]rename complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold] file_path=[cyan]{row['file_path']}[/cyan]"
    )


@cli.command()
@click.argument("dataset_id")
@click.option(
    "--actor",
    default=None,
    help="Name to record as the changelog actor. Defaults to $USER.",
)
@click.pass_context
def refresh(ctx: click.Context, dataset_id: str, actor: str | None) -> None:
    """Re-stat a library file after an in-place edit and update the inventory snapshot.

    Use after editing a library file (e.g. adding a vector field). The
    file must still pass canonical validators — refresh refuses to
    record bad state in the inventory.
    """
    from . import lifecycle

    inventory, changelog, library = _resolve_paths(ctx.obj["root"])

    try:
        row = lifecycle.refresh(
            inventory, changelog, library,
            dataset_id=dataset_id, actor=actor or _default_actor(),
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[green]refresh complete[/green] — "
        f"dataset_id=[bold]{dataset_id}[/bold]"
    )
    console.print(f"  date_modified: [cyan]{row.get('date_modified')}[/cyan]")


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

    inventory, changelog, library = _resolve_paths(ctx.obj["root"])

    try:
        lifecycle.tombstone(
            inventory, changelog, library,
            dataset_id=dataset_id, actor=actor or _default_actor(), reason=reason,
        )
    except lifecycle.LifecycleError as exc:
        raise click.ClickException(str(exc))

    console.print(
        f"[yellow]tombstoned[/yellow] dataset_id=[bold]{dataset_id}[/bold] "
        "(file deleted, inventory row preserved as audit record)"
    )


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
    """Reconcile library/ against inventory.xlsx and write a timestamped report."""
    from . import lifecycle, reconcile as reconcile_mod

    root: Path = ctx.obj["root"]
    library = root / "library"
    inventory = root / "inventory" / "inventory.xlsx"
    changelog = root / "inventory" / "changelog.md"
    reports = root / "reports"

    if fix_renames:
        deep = True  # rename detection requires deep mode

    actor_name = actor or _default_actor()
    result = reconcile_mod.reconcile(
        library, inventory, reports,
        actor=actor_name, changelog_path=changelog, deep=deep,
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
    if result.auto_resolved:
        console.print(
            f"  [dim]auto-resolved drift: {len(result.auto_resolved)} "
            f"(snapshot refreshed; see changelog)[/dim]"
        )
    console.print(f"  report: [cyan]{result.report_path}[/cyan]")

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
        # finding.path is "old → new"; finding.dataset_id is the row id
        if "→" not in finding.path:
            continue
        old_str, new_str = (s.strip() for s in finding.path.split("→", 1))

        console.print(f"  [cyan]{finding.dataset_id}[/cyan]: {old_str} → {new_str}")
        if not click.confirm("  apply this rename?", default=False):
            skipped += 1
            continue
        try:
            lifecycle.rename(
                inventory, changelog, library,
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


if __name__ == "__main__":
    cli()
