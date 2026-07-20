"""The site: tracked-games home page, IGDB search, and the run/attention digest.

Writes go only to our own ledger (watch-list adds/removes). Search hits IGDB
live; everything else renders from the ledger, so the page works even when
IGDB is down or unconfigured.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from . import gcal as gcal_mod
from .config import Config
from .igdb import Igdb, IgdbError
from .ledger import Ledger

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True
)
TEMPLATES.filters["ts_year"] = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).year


def _tracked_games(ledger: Ledger, allowlist: list[str]) -> dict:
    """Assemble home-page data from the latest ledger observations.

    Buckets use the same platform-preference logic as the calendar
    (gcal._pick_release), so a game already out on its preferred platform
    shows as Released even if console ports are upcoming."""
    games = ledger.tracked_games()
    watched = {
        r["key"].removeprefix("watch:")
        for r in ledger.conn.execute("SELECT key FROM kv WHERE key LIKE 'watch:%'")
    }

    by_game: dict[int, list[dict]] = {}
    for rel in ledger.current_releases():
        by_game.setdefault(rel["igdb_id"], []).append(rel)

    today = datetime.now(timezone.utc).date()
    upcoming, undated, released = [], [], []
    seen_slugs = set()
    for slug, g in games.items():
        seen_slugs.add(slug)
        dates = sorted(by_game.get(g["igdb_id"], []), key=lambda r: r["date_unix"])
        entry = {**g, "watched": slug in watched, "releases": []}
        future = []
        seen = set()
        for rel in dates:
            day = datetime.fromtimestamp(rel["date_unix"], tz=timezone.utc).date()
            if (day, rel["platform"]) in seen:
                continue
            seen.add((day, rel["platform"]))
            rel = {**rel, "day": day}
            entry["releases"].append(rel)
            if day >= today:
                future.append(rel)
        pref = gcal_mod._platform_pref(ledger, slug, g)
        picked = gcal_mod._pick_release(entry["releases"], pref, allowlist, today)
        past = [r for r in entry["releases"] if r["day"] < today]
        if picked:
            rel, _ = picked
            entry["next"] = {**rel, "day": datetime.fromtimestamp(rel["date_unix"], tz=timezone.utc).date()}
            upcoming.append(entry)
        elif past:
            entry["last"] = past[-1]
            released.append(entry)
        else:
            undated.append(entry)

    # Watched slugs that haven't been through a `releases` run yet.
    pending = sorted(watched - seen_slugs)

    now_ts = datetime.now(timezone.utc).timestamp()
    playing = sorted(
        (
            g for g in ledger.latest_observations("steam_library").values()
            if g.get("last_played") and g.get("playtime_minutes", 0) >= 60
            and now_ts - g["last_played"] <= 14 * 86400
        ),
        key=lambda g: g["last_played"],
        reverse=True,
    )[:10]
    playing = [
        {**g, "last_iso": datetime.fromtimestamp(g["last_played"], tz=timezone.utc).isoformat()}
        for g in playing
    ]

    upcoming.sort(key=lambda e: e["next"]["day"])
    released.sort(key=lambda e: e["last"]["day"], reverse=True)
    undated.sort(key=lambda e: e["title"])
    return {
        "upcoming": upcoming,
        "undated": undated,
        "released": released,
        "pending": pending,
        "playing": playing,
        "today": today,
    }


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="gamecal")
    ledger = Ledger(cfg.ledger_path, check_same_thread=False)

    def render(template: str, **ctx) -> HTMLResponse:
        return HTMLResponse(TEMPLATES.get_template(template).render(**ctx))

    NUDGE_KINDS = ("playing", "gone_quiet", "released")

    @app.get("/", response_class=HTMLResponse)
    def home():
        items = []
        for a in ledger.open_attention():
            d = dict(a)
            if d.get("links"):
                d["links"] = json.loads(d["links"])
            elif d.get("url"):
                d["links"] = {"open": d["url"]}
            else:
                d["links"] = {}
            items.append(d)
        return render(
            "index.html",
            **_tracked_games(ledger, cfg.sync.platforms),
            runs=ledger.recent_runs(10),
            errors=[a for a in items if a["kind"] not in NUDGE_KINDS],
            nudges={k: [a for a in items if a["kind"] == k] for k in NUDGE_KINDS},
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(q: str = ""):
        results, error = [], None
        if q:
            try:
                igdb = Igdb(cfg.igdb)
                results = igdb.search(q)
            except IgdbError as e:
                error = str(e)
            except Exception as e:  # IGDB down shouldn't 500 the page
                error = f"IGDB search failed: {e!r}"
        tracked = set(ledger.latest_observations("igdb_game")) | {
            r["key"].removeprefix("watch:")
            for r in ledger.conn.execute("SELECT key FROM kv WHERE key LIKE 'watch:%'")
        }
        return render("search.html", q=q, results=results, error=error, tracked=tracked)

    @app.post("/watch")
    def watch(
        request: Request,
        slug: str = Form(...),
        igdb_id: int = Form(None),
        title: str = Form(None),
        platform: str = Form(""),
        back: str = Form("/"),
    ):
        ledger.set(f"watch:{slug}", platform or "1")
        # Record identity + release dates now (instead of waiting for the
        # nightly releases run) and push the calendar event immediately.
        # All best-effort: the nightly reconcile converges regardless.
        if igdb_id and title:
            run_id = ledger.start_run("web-watch")
            detail = f"watched {slug}"
            ledger.record_observations(
                run_id,
                "igdb_game_web",
                [{"external_id": slug, "igdb_id": igdb_id, "slug": slug,
                  "title": title, "source": "watch"}],
            )
            try:
                dates = Igdb(cfg.igdb).release_dates([igdb_id])
                ledger.record_observations(
                    run_id,
                    "igdb_release",
                    [
                        {**d, "external_id": f"{d['igdb_id']}:{d['platform']}:{d['date_unix']}"}
                        for d in dates
                    ],
                )
                detail += f", {len(dates)} dated releases"
                detail += ", " + gcal_mod.upsert_single(cfg, ledger, slug)
            except Exception as e:
                detail += f" (instant sync failed: {e!r})"
            ledger.finish_run(run_id, "ok", detail)
        return RedirectResponse(back, status_code=303)

    @app.post("/attention/resolve")
    def resolve_attention(item_id: int = Form(...)):
        ledger.resolve_attention(item_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/unwatch")
    def unwatch(slug: str = Form(...), back: str = Form("/")):
        g = ledger.tracked_games().get(slug)
        ledger.conn.execute("DELETE FROM kv WHERE key = ?", (f"watch:{slug}",))
        ledger.conn.commit()
        if g:
            try:
                gcal_mod.remove_single(cfg, ledger, g["igdb_id"])
            except Exception:
                pass  # nightly reconcile will delete it
        return RedirectResponse(back, status_code=303)

    return app
