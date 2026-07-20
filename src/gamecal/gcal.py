"""Google Calendar reconciler.

Maintains a dedicated "Game Releases" calendar: one all-day event per game,
keyed by IGDB id via extendedProperties (never matched by title). Each run
recomputes the desired event per game and creates/patches/deletes to match.

Platform choice per game:
  - steam/owned source -> PC date
  - watch source        -> the stored per-game platform preference, if any
  - fallback            -> earliest upcoming date across the config allowlist
If the preferred platform has no dated release but another platform does,
the earliest available date is used and the description says so.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config
from .ledger import Ledger

SCOPES = ["https://www.googleapis.com/auth/calendar"]
MARKER = "bls_managed"

PC = "PC (Microsoft Windows)"


# -- auth / calendar bootstrap ------------------------------------------------


def get_service(cfg: Config):
    token_path = cfg.data_dir / "google_token.json"
    client_path = cfg.data_dir / "google_client.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds or not creds.valid:
        if not client_path.exists():
            raise RuntimeError(f"missing {client_path} (Google OAuth desktop client)")
        flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def ensure_calendar(service, ledger: Ledger) -> str:
    cal_id = ledger.get("gcal:calendar_id")
    if cal_id:
        try:
            service.calendars().get(calendarId=cal_id).execute()
            return cal_id
        except Exception:
            pass  # deleted out from under us; recreate
    created = service.calendars().insert(body={"summary": "Game Releases"}).execute()
    ledger.set("gcal:calendar_id", created["id"])
    return created["id"]


# -- desired-state computation ------------------------------------------------


def _platform_pref(ledger: Ledger, slug: str, g: dict) -> str | None:
    if g.get("source") in ("steam", "owned"):
        return PC
    stored = ledger.get(f"watch:{slug}")
    return stored if stored and stored != "1" else None


def _pick_release(releases: list[dict], platform_pref: str | None,
                  allowlist: list[str], today: date) -> tuple[dict, bool] | None:
    """Choose the release row that defines this game's event date.

    Returns (release, used_fallback) or None if no event is warranted.
    A game already released on its preferred platform gets no event —
    upcoming ports to other platforms are noise, not a release date.
    Falling back to other platforms happens only when the preferred
    platform has no dates at all yet.
    """
    def day(r):
        return datetime.fromtimestamp(r["date_unix"], tz=timezone.utc).date()

    upcoming = sorted(
        (r for r in releases if day(r) >= today), key=lambda r: r["date_unix"]
    )
    if platform_pref:
        on_pref = [r for r in releases if r["platform"] == platform_pref]
        upcoming_pref = sorted(
            (r for r in on_pref if day(r) >= today), key=lambda r: r["date_unix"]
        )
        if upcoming_pref:
            return upcoming_pref[0], False
        if on_pref:
            return None  # released on the platform you play on
    if not upcoming:
        return None
    allowed = [r for r in upcoming if not allowlist or r["platform"] in allowlist]
    pool = allowed or upcoming
    return pool[0], platform_pref is not None


def desired_events(ledger: Ledger, allowlist: list[str]) -> list[dict]:
    games = ledger.tracked_games()
    by_game: dict[int, list[dict]] = {}
    for rel in ledger.run_observations("igdb_release"):
        by_game.setdefault(rel["igdb_id"], []).append(rel)

    today = datetime.now(timezone.utc).date()
    out = []
    for slug, g in games.items():
        pref = _platform_pref(ledger, slug, g)
        picked = _pick_release(by_game.get(g["igdb_id"], []), pref, allowlist, today)
        if not picked:
            continue
        rel, fallback = picked
        day = datetime.fromtimestamp(rel["date_unix"], tz=timezone.utc).date()

        lines = [f"https://backloggd.com/games/{slug}/"]
        if g.get("steam_appid"):
            lines.append(f"https://store.steampowered.com/app/{g['steam_appid']}/")
        elif g.get("store_url"):
            lines.append(g["store_url"])
        lines.append(f"https://www.igdb.com/games/{slug}")
        lines.append("")
        if fallback:
            lines.append(f"No {pref} date yet — earliest is {rel['platform']}.")
        seen = set()
        for r in sorted(by_game.get(g["igdb_id"], []), key=lambda r: r["date_unix"]):
            d = datetime.fromtimestamp(r["date_unix"], tz=timezone.utc).date()
            if (d, r["platform"]) in seen:
                continue
            seen.add((d, r["platform"]))
            lines.append(f"{d.isoformat()}  {r['platform']}")

        out.append(
            {
                "summary": f"🎮 {g['title']}",
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
                "description": "\n".join(lines),
                "transparency": "transparent",
                "extendedProperties": {
                    "private": {MARKER: "1", "bls_id": str(g["igdb_id"])}
                },
            }
        )
    return out


# -- reconcile ----------------------------------------------------------------

def _differs(existing: dict, desired: dict) -> bool:
    return (
        existing.get("summary") != desired["summary"]
        or existing.get("start", {}).get("date") != desired["start"]["date"]
        or (existing.get("description") or "") != desired["description"]
    )


def reconcile(service, cal_id: str, desired: list[dict], today: date) -> dict:
    """Returns {'create': [...], 'update': [...], 'delete': [...]} of planned
    ops; caller decides whether to apply (dry-run) — each item is
    (event_body, existing_event_or_None)."""
    existing: dict[str, dict] = {}
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=cal_id,
                privateExtendedProperty=f"{MARKER}=1",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            existing[ev["extendedProperties"]["private"]["bls_id"]] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    desired_by_id = {e["extendedProperties"]["private"]["bls_id"]: e for e in desired}
    plan = {"create": [], "update": [], "delete": []}
    for bls_id, ev in desired_by_id.items():
        cur = existing.get(bls_id)
        if cur is None:
            plan["create"].append((ev, None))
        elif _differs(cur, ev):
            plan["update"].append((ev, cur))
    for bls_id, cur in existing.items():
        if bls_id in desired_by_id:
            continue
        # Untracked or nothing upcoming anymore: only remove future events;
        # past events stay as history.
        start = cur.get("start", {}).get("date")
        if start and date.fromisoformat(start) >= today:
            plan["delete"].append((None, cur))
    return plan


def apply(service, cal_id: str, plan: dict) -> None:
    for ev, _ in plan["create"]:
        service.events().insert(calendarId=cal_id, body=ev).execute()
    for ev, cur in plan["update"]:
        service.events().patch(calendarId=cal_id, eventId=cur["id"], body=ev).execute()
    for _, cur in plan["delete"]:
        service.events().delete(calendarId=cal_id, eventId=cur["id"]).execute()
