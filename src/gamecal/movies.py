"""Movie pipeline: Letterboxd watchlist/diary + TMDB dates -> two Google
calendars, reusing the game reconciler.

Movie Releases: one event per watchlisted movie on its theatrical date;
digital-only movies (no theatrical date on TMDB) use the streaming date
and say so. Streaming/physical dates ride along in the description.

Movies Watched: one all-day event per diary entry, full history. All
events are past-dated, and the shared reconciler never deletes past
events, so this calendar is effectively append-and-patch.
"""

from datetime import date, datetime, timedelta, timezone

from .gcal import MARKER
from .ledger import Ledger
from .tmdb import DIGITAL, LIMITED, PHYSICAL, THEATRICAL

RELEASES_CAL_KEY = "gcal:movie_calendar_id"
RELEASES_CAL_NAME = "Movie Releases"
WATCHED_CAL_KEY = "gcal:watched_calendar_id"
WATCHED_CAL_NAME = "Movies Watched"


def _links(slug: str | None, tmdb_id: int | None) -> dict:
    links = {}
    if slug:
        links["letterboxd"] = f"https://letterboxd.com/film/{slug}/"
    if tmdb_id:
        links["tmdb"] = f"https://www.themoviedb.org/movie/{tmdb_id}"
    return links


def releases_by_movie(ledger: Ledger) -> dict[int, list[dict]]:
    by_movie: dict[int, list[dict]] = {}
    for rel in ledger.current_releases(source="tmdb_release", id_field="tmdb_id"):
        by_movie.setdefault(rel["tmdb_id"], []).append(rel)
    return by_movie


def pick_release(releases: list[dict]) -> tuple[dict, bool] | None:
    """(release, is_streaming_only). Theatrical (incl. limited) wins; a
    movie with only a digital date is a streaming release."""
    theatrical = sorted(
        (r for r in releases if r["type"] in (THEATRICAL, LIMITED)),
        key=lambda r: r["date"],
    )
    if theatrical:
        return theatrical[0], False
    digital = sorted(
        (r for r in releases if r["type"] == DIGITAL), key=lambda r: r["date"]
    )
    if digital:
        return digital[0], True
    return None


def desired_release_events(ledger: Ledger) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    by_movie = releases_by_movie(ledger)
    out = []
    for m in ledger.run_observations("lbx_watchlist"):
        tmdb_id = m.get("tmdb_id")
        if not tmdb_id:
            continue
        rels = by_movie.get(tmdb_id, [])
        picked = pick_release(rels)
        if not picked or picked[0]["date"] < today:
            continue  # undated, or already out (past events stay as history)
        rel, streaming_only = picked
        title = rel.get("title") or m.get("title") or m["slug"]
        day = date.fromisoformat(rel["date"])

        lines = list(_links(m.get("slug"), tmdb_id).values())
        lines.append("")
        seen = set()
        for r in sorted(rels, key=lambda r: r["date"]):
            if r["type"] in seen or r["type"] not in (THEATRICAL, LIMITED, DIGITAL, PHYSICAL):
                continue
            seen.add(r["type"])
            lines.append(f"{r['date']}  {r['type_name']}")

        out.append(
            {
                "summary": f"🎬 {title}" + (" (streaming)" if streaming_only else ""),
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
                "description": "\n".join(lines),
                "transparency": "transparent",
                "extendedProperties": {
                    "private": {MARKER: "1", "bls_id": f"mv:{tmdb_id}"}
                },
            }
        )
    return out


def desired_watched_events(ledger: Ledger) -> list[dict]:
    out = []
    for e in ledger.latest_observations("lbx_diary").values():
        day = date.fromisoformat(e["watched"])
        lines = list(_links(e.get("slug"), e.get("tmdb_id")).values())
        notes = []
        if e.get("rating"):
            notes.append(f"Rated {e['rating']}★")
        if e.get("rewatch"):
            notes.append("Rewatch")
        if notes:
            lines += ["", " · ".join(notes)]
        out.append(
            {
                "summary": f"🍿 {e['title']}",
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
                "description": "\n".join(lines),
                "transparency": "transparent",
                "extendedProperties": {
                    "private": {MARKER: "1", "bls_id": f"dw:{e['external_id']}"}
                },
            }
        )
    return out
