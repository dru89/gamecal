"""CLI entry point. Cron on ds9 calls the same subcommands you run by hand.

Every job runs through _job(): ledger run row, circuit breaker check on the
way in, breaker bookkeeping on the way out. Breaker trips after 3 consecutive
failures and the job refuses to run until `breaker reset`.
"""

import sys
from datetime import datetime, timezone

import click

from . import config as config_mod
from .igdb import Igdb
from .ledger import Ledger
from .steam import Steam

BREAKER_LIMIT = 3


class Ctx:
    def __init__(self, cfg_path: str | None):
        self.cfg = config_mod.load(cfg_path)
        self.ledger = Ledger(self.cfg.ledger_path)


@click.group()
@click.option("--config", "cfg_path", type=click.Path(), default=None, help="Path to config.toml")
@click.pass_context
def cli(ctx: click.Context, cfg_path: str | None):
    ctx.obj = Ctx(cfg_path)


def _job(ctx: Ctx, name: str, fn) -> None:
    if ctx.ledger.breaker_tripped(name, BREAKER_LIMIT):
        click.echo(
            f"{name}: circuit breaker tripped "
            f"({ctx.ledger.breaker_failures(name)} consecutive failures). "
            f"Run `backloggd-sync breaker reset {name}` after investigating.",
            err=True,
        )
        sys.exit(2)
    run_id = ctx.ledger.start_run(name)
    try:
        detail = fn(run_id) or ""
    except Exception as e:
        ctx.ledger.finish_run(run_id, "failed", repr(e))
        ctx.ledger.breaker_record(name, ok=False)
        ctx.ledger.add_attention("sync_failure", f"{name} failed: {e!r}")
        raise
    ctx.ledger.finish_run(run_id, "ok", detail)
    ctx.ledger.breaker_record(name, ok=True)
    click.echo(f"{name}: ok. {detail}")


@cli.command("pull-steam")
@click.pass_obj
def pull_steam(ctx: Ctx):
    """Pull wishlist + owned games from the Steam Web API into the ledger."""

    def run(run_id: int) -> str:
        steam = Steam(ctx.cfg.steam)
        wishlist = steam.wishlist()
        owned = steam.owned_games()
        ctx.ledger.record_observations(run_id, "steam_wishlist", wishlist)
        ctx.ledger.record_observations(run_id, "steam_library", owned)
        return f"{len(wishlist)} wishlist items, {len(owned)} owned games"

    _job(ctx, "pull-steam", run)


@cli.command("releases")
@click.option("--all", "show_all", is_flag=True, help="Include past releases")
@click.pass_obj
def releases(ctx: Ctx, show_all: bool):
    """Join wishlist against IGDB release dates; print upcoming releases."""

    def run(run_id: int) -> str:
        igdb = Igdb(ctx.cfg.igdb)
        wishlist = ctx.ledger.latest_observations("steam_wishlist")
        appids = [int(k) for k in wishlist]
        mapping = igdb.games_for_steam_appids(appids) if appids else {}
        unmatched = sorted(set(appids) - set(mapping))
        if unmatched:
            click.echo(f"note: {len(unmatched)} appids had no IGDB match: {unmatched}", err=True)

        watched = _watch_slugs(ctx)
        by_slug = igdb.games_by_slugs(watched) if watched else {}
        missing = sorted(set(watched) - set(by_slug))
        if missing:
            click.echo(f"note: watched slugs not found on IGDB: {missing}", err=True)

        games = {m["igdb_id"]: m for m in [*mapping.values(), *by_slug.values()]}
        if not games:
            raise click.ClickException(
                "nothing to look up — run pull-steam and/or `watch add <slug>` first"
            )
        dates = igdb.release_dates(list(games))
        if ctx.cfg.sync.platforms:
            dates = [d for d in dates if d["platform"] in ctx.cfg.sync.platforms]
        ctx.ledger.record_observations(
            run_id,
            "igdb_release",
            [{**d, "external_id": f"{d['igdb_id']}:{d['platform']}"} for d in dates],
        )

        today = datetime.now(timezone.utc).date()
        rows = []
        for d in sorted(dates, key=lambda x: x["date_unix"]):
            day = datetime.fromtimestamp(d["date_unix"], tz=timezone.utc).date()
            if not show_all and day < today:
                continue
            rows.append(f"  {day}  {d['title']}  [{d['platform']}]")
        click.echo("\n".join(rows) if rows else "  (no upcoming exact-dated releases)")
        return f"{len(games)} games ({len(mapping)} steam, {len(by_slug)} watched), {len(dates)} dated releases"

    _job(ctx, "releases", run)


def _watch_slugs(ctx: Ctx) -> list[str]:
    rows = ctx.ledger.conn.execute(
        "SELECT key FROM kv WHERE key LIKE 'watch:%'"
    ).fetchall()
    return [r["key"].removeprefix("watch:") for r in rows]


@cli.group()
def watch():
    """Manually watched games (non-Steam titles), by IGDB slug."""


@watch.command("add")
@click.argument("slugs", nargs=-1, required=True)
@click.pass_obj
def watch_add(ctx: Ctx, slugs: tuple[str, ...]):
    """Add IGDB slugs, e.g. the tail of a Backloggd game URL: watch add silksong."""
    for slug in slugs:
        ctx.ledger.set(f"watch:{slug}", "1")
    click.echo(f"watching: {', '.join(slugs)}")


@watch.command("remove")
@click.argument("slugs", nargs=-1, required=True)
@click.pass_obj
def watch_remove(ctx: Ctx, slugs: tuple[str, ...]):
    for slug in slugs:
        ctx.ledger.conn.execute("DELETE FROM kv WHERE key = ?", (f"watch:{slug}",))
    ctx.ledger.conn.commit()
    click.echo(f"unwatched: {', '.join(slugs)}")


@watch.command("list")
@click.pass_obj
def watch_list(ctx: Ctx):
    for slug in _watch_slugs(ctx):
        click.echo(f"  {slug}")


@cli.command("report")
@click.pass_obj
def report(ctx: Ctx):
    """Recent runs and open attention items. Grows into the ds9 digest site."""
    click.echo("Recent runs:")
    for r in ctx.ledger.recent_runs():
        click.echo(f"  #{r['id']} {r['started_at']} {r['job']:<14} {r['status']:<7} {r['detail'] or ''}")
    attention = ctx.ledger.open_attention()
    click.echo(f"\nAttention ({len(attention)} open):")
    for a in attention:
        click.echo(f"  [{a['kind']}] {a['created_at']} {a['message']}")


@cli.group()
def breaker():
    """Inspect or reset circuit breakers."""


@breaker.command("status")
@click.pass_obj
def breaker_status(ctx: Ctx):
    rows = ctx.ledger.conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE 'breaker:%'"
    ).fetchall()
    if not rows:
        click.echo("no breaker state yet")
    for r in rows:
        job = r["key"].removeprefix("breaker:")
        n = int(r["value"])
        state = "TRIPPED" if n >= BREAKER_LIMIT else "ok"
        click.echo(f"  {job}: {n} consecutive failures ({state})")


@breaker.command("reset")
@click.argument("job")
@click.pass_obj
def breaker_reset(ctx: Ctx, job: str):
    ctx.ledger.breaker_reset(job)
    click.echo(f"breaker reset for {job}")


def main() -> None:
    cli()
