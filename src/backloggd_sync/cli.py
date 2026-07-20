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


def _notify(ctx: Ctx, title: str, message: str) -> None:
    """Best-effort push via ntfy; never lets notification failure mask the
    original error."""
    if not ctx.cfg.notify.ntfy_url:
        return
    try:
        import httpx

        httpx.post(
            ctx.cfg.notify.ntfy_url,
            content=message,
            headers={"Title": title, "Priority": "high", "Tags": "warning"},
            timeout=10,
        )
    except Exception as e:
        click.echo(f"warning: ntfy push failed: {e!r}", err=True)


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
        failures = ctx.ledger.breaker_failures(name)
        tripped = failures >= BREAKER_LIMIT
        _notify(
            ctx,
            f"{name} {'DISABLED' if tripped else 'failed'}",
            f"{e!r}\n({failures}/{BREAKER_LIMIT} consecutive failures"
            + (", job disabled until breaker reset)" if tripped else ")"),
        )
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

        # Owned-but-unreleased (preorders, early access): Steam drops purchases
        # from the wishlist, so these need their own path into the pipeline.
        library = ctx.ledger.latest_observations("steam_library")
        owned_appids = [int(k) for k in library if int(k) not in set(appids)]
        owned_map = igdb.games_for_steam_appids(owned_appids) if owned_appids else {}
        unreleased = (
            igdb.unreleased_games([m["igdb_id"] for m in owned_map.values()])
            if owned_map
            else {}
        )
        owned_tracked = [
            {**m, "steam_appid": aid}
            for aid, m in owned_map.items()
            if m["igdb_id"] in unreleased
        ]
        steam_games = [{**m, "steam_appid": aid} for aid, m in mapping.items()]

        watched = _watch_slugs(ctx)
        by_slug = igdb.games_by_slugs(watched) if watched else {}
        missing = sorted(set(watched) - set(by_slug))
        if missing:
            click.echo(f"note: watched slugs not found on IGDB: {missing}", err=True)

        # Console-preference watches get a first-party store link when IGDB has one.
        def _store_source(pref: str | None) -> int | None:
            if pref and "PlayStation" in pref:
                return igdb.PSN_STORE
            if pref and "Xbox" in pref:
                return igdb.XBOX_STORE
            return None

        prefs = {slug: ctx.ledger.get(f"watch:{slug}") for slug in by_slug}
        need = [m["igdb_id"] for s, m in by_slug.items() if _store_source(prefs.get(s))]
        store = igdb.store_urls(need) if need else {}
        watch_games = [
            {
                **m,
                "store_url": store.get(m["igdb_id"], {}).get(_store_source(prefs.get(s)))
                if _store_source(prefs.get(s))
                else None,
            }
            for s, m in by_slug.items()
        ]

        games = {
            m["igdb_id"]: m
            for m in [*steam_games, *owned_tracked, *watch_games]
        }
        if not games:
            raise click.ClickException(
                "nothing to look up — run pull-steam and/or `watch add <slug>` first"
            )
        ctx.ledger.record_observations(
            run_id,
            "igdb_game",
            [
                {**m, "external_id": m["slug"], "source": src}
                for src, group in (
                    ("steam", steam_games),
                    ("owned", owned_tracked),
                    ("watch", watch_games),
                )
                for m in group
            ],
        )
        dates = igdb.release_dates(list(games))
        if ctx.cfg.sync.platforms:
            dates = [d for d in dates if d["platform"] in ctx.cfg.sync.platforms]
        ctx.ledger.record_observations(
            run_id,
            "igdb_release",
            [
                {**d, "external_id": f"{d['igdb_id']}:{d['platform']}:{d['date_unix']}"}
                for d in dates
            ],
        )

        today = datetime.now(timezone.utc).date()
        rows = []
        for d in sorted(dates, key=lambda x: x["date_unix"]):
            day = datetime.fromtimestamp(d["date_unix"], tz=timezone.utc).date()
            if not show_all and day < today:
                continue
            rows.append(f"  {day}  {d['title']}  [{d['platform']}]")
        click.echo("\n".join(rows) if rows else "  (no upcoming exact-dated releases)")
        return (
            f"{len(games)} games ({len(mapping)} wishlist, {len(owned_tracked)} owned-unreleased,"
            f" {len(by_slug)} watched), {len(dates)} dated releases"
        )

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
@click.option("--platform", default=None, help='IGDB platform name, e.g. "Nintendo Switch"')
@click.pass_obj
def watch_add(ctx: Ctx, slugs: tuple[str, ...], platform: str | None):
    """Add IGDB slugs, e.g. the tail of a Backloggd game URL: watch add silksong.

    The kv value is the preferred platform name, or "1" for no preference
    (earliest date across the config allowlist).
    """
    for slug in slugs:
        ctx.ledger.set(f"watch:{slug}", platform or "1")
    click.echo(f"watching: {', '.join(slugs)}" + (f" [{platform}]" if platform else ""))


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


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print the plan without touching the calendar")
@click.pass_obj
def calendar(ctx: Ctx, dry_run: bool):
    """Reconcile the Game Releases calendar with the ledger."""
    from . import gcal

    def run(run_id: int) -> str:
        service = gcal.get_service(ctx.cfg)
        cal_id = gcal.ensure_calendar(service, ctx.ledger)
        desired = gcal.desired_events(ctx.ledger, ctx.cfg.sync.platforms)
        today = datetime.now(timezone.utc).date()
        plan = gcal.reconcile(service, cal_id, desired, today)
        for ev, _ in plan["create"]:
            click.echo(f"  + {ev['start']['date']}  {ev['summary']}")
        for ev, cur in plan["update"]:
            click.echo(f"  ~ {cur['start'].get('date')} -> {ev['start']['date']}  {ev['summary']}")
        for _, cur in plan["delete"]:
            click.echo(f"  - {cur['start'].get('date')}  {cur.get('summary')}")
        counts = (
            f"{len(plan['create'])} create, {len(plan['update'])} update,"
            f" {len(plan['delete'])} delete ({len(desired)} events desired)"
        )
        if dry_run:
            return f"dry-run: {counts}"
        gcal.apply(service, cal_id, plan)
        ctx.ledger.record_actions(
            run_id,
            "gcal",
            [
                (ev["extendedProperties"]["private"]["bls_id"], op, ev)
                for op in ("create", "update")
                for ev, _ in plan[op]
            ]
            + [
                (cur["extendedProperties"]["private"]["bls_id"], "delete", {"id": cur["id"]})
                for _, cur in plan["delete"]
            ],
        )
        return counts

    _job(ctx, "calendar", run)


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address (ds9: the Tailscale IP)")
@click.option("--port", default=8787, type=int)
@click.pass_obj
def serve(ctx: Ctx, host: str, port: int):
    """Run the web UI: tracked games, IGDB search, run digest."""
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(ctx.cfg), host=host, port=port)


def main() -> None:
    cli()
