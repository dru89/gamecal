"""IGDB client (Twitch client-credentials auth).

Two jobs: map Steam appids to IGDB games via external_games, and pull
release_dates. IGDB models fuzzy dates explicitly via `date_format` on
release_dates — only 0 (exact YYYY-MM-DD) is calendar-worthy.
"""

import time

import httpx

from .config import IgdbConfig

AUTH_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4"

# external_games.external_game_source for Steam
STEAM_SOURCE = 1
# release_dates.date_format value for an exact YYYY-MM-DD date
EXACT_DATE = 0


class IgdbError(RuntimeError):
    pass


class Igdb:
    def __init__(self, cfg: IgdbConfig):
        if not cfg.client_id or not cfg.client_secret:
            raise IgdbError(
                "igdb.client_id and igdb.client_secret must be set in config.toml "
                "(create a Twitch dev app: https://dev.twitch.tv/console)"
            )
        self.cfg = cfg
        self.client = httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expiry = 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        r = self.client.post(
            AUTH_URL,
            params={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "grant_type": "client_credentials",
            },
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return self._token

    def _query(self, endpoint: str, body: str) -> list[dict]:
        r = self.client.post(
            f"{API}/{endpoint}",
            headers={
                "Client-ID": self.cfg.client_id,
                "Authorization": f"Bearer {self._auth()}",
            },
            content=body,
        )
        r.raise_for_status()
        return r.json()

    def games_for_steam_appids(self, appids: list[int]) -> dict[int, dict]:
        """Map steam appid -> {igdb_id, slug, name}. Batches of 100."""
        out: dict[int, dict] = {}
        for i in range(0, len(appids), 100):
            batch = appids[i : i + 100]
            uids = ",".join(f'"{a}"' for a in batch)
            rows = self._query(
                "external_games",
                f"fields uid, game.id, game.slug, game.name;"
                f" where external_game_source = {STEAM_SOURCE} & uid = ({uids});"
                f" limit 500;",
            )
            for row in rows:
                game = row.get("game")
                if not game:
                    continue
                out[int(row["uid"])] = {
                    "igdb_id": game["id"],
                    "slug": game["slug"],
                    "title": game["name"],
                }
        return out

    def games_by_slugs(self, slugs: list[str]) -> dict[str, dict]:
        """Map igdb slug -> {igdb_id, slug, title}. Slugs are the tail of an
        IGDB or Backloggd game URL, e.g. 'silksong' in backloggd.com/games/silksong/."""
        out: dict[str, dict] = {}
        for i in range(0, len(slugs), 100):
            batch = slugs[i : i + 100]
            quoted = ",".join(f'"{s}"' for s in batch)
            rows = self._query(
                "games",
                f"fields id, slug, name; where slug = ({quoted}); limit 500;",
            )
            for row in rows:
                out[row["slug"]] = {
                    "igdb_id": row["id"],
                    "slug": row["slug"],
                    "title": row["name"],
                }
        return out

    # external_game_sources ids for console storefronts (no Nintendo eShop on IGDB)
    PSN_STORE = 36
    XBOX_STORE = 31

    def store_urls(self, igdb_ids: list[int]) -> dict[int, dict[int, str]]:
        """igdb_id -> {source_id: url} for PlayStation/Xbox storefronts."""
        out: dict[int, dict[int, str]] = {}
        for i in range(0, len(igdb_ids), 100):
            batch = igdb_ids[i : i + 100]
            ids = ",".join(str(x) for x in batch)
            rows = self._query(
                "external_games",
                f"fields game, url, external_game_source;"
                f" where game = ({ids})"
                f" & external_game_source = ({self.XBOX_STORE},{self.PSN_STORE})"
                f" & url != null; limit 500;",
            )
            for r in rows:
                out.setdefault(r["game"], {})[r["external_game_source"]] = r["url"]
        return out

    def unreleased_games(self, igdb_ids: list[int]) -> dict[int, dict]:
        """Subset of the given games IGDB considers not fully released:
        alpha/beta/early access, or no past first_release_date. Used to keep
        owned-but-unreleased games (preorders, EA) in the pipeline without
        dragging in the whole library."""
        now = int(time.time())
        out: dict[int, dict] = {}
        for i in range(0, len(igdb_ids), 100):
            batch = igdb_ids[i : i + 100]
            ids = ",".join(str(x) for x in batch)
            rows = self._query(
                "games",
                f"fields id, slug, name, game_status, first_release_date;"
                f" where id = ({ids})"
                f" & (game_status = (2,3,4) | first_release_date = null"
                f" | first_release_date > {now});"
                f" limit 500;",
            )
            for row in rows:
                out[row["id"]] = {
                    "igdb_id": row["id"],
                    "slug": row["slug"],
                    "title": row["name"],
                    "game_status": row.get("game_status"),
                }
        return out

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text game search. Returns [{igdb_id, slug, title,
        first_release_date, platforms, cover_url}]."""
        q = query.replace('"', "")
        rows = self._query(
            "games",
            f'search "{q}"; fields id, slug, name, first_release_date,'
            f" platforms.name, cover.image_id; limit {limit};",
        )
        out = []
        for row in rows:
            cover = (row.get("cover") or {}).get("image_id")
            out.append(
                {
                    "igdb_id": row["id"],
                    "slug": row["slug"],
                    "title": row["name"],
                    "first_release_date": row.get("first_release_date"),
                    "platforms": [p["name"] for p in row.get("platforms", [])],
                    "cover_url": (
                        f"https://images.igdb.com/igdb/image/upload/t_cover_small/{cover}.jpg"
                        if cover
                        else None
                    ),
                }
            )
        return out

    def release_dates(self, igdb_ids: list[int]) -> list[dict]:
        """Exact-dated releases for the given games, all platforms.

        Returns [{igdb_id, title, slug, platform, date_unix, human, region}].
        Fuzzy dates (quarters, years, TBD) are excluded — they are not
        calendar events.
        """
        out: list[dict] = []
        for i in range(0, len(igdb_ids), 100):
            batch = igdb_ids[i : i + 100]
            ids = ",".join(str(x) for x in batch)
            rows: list[dict] = []
            offset = 0
            while True:
                page = self._query(
                    "release_dates",
                    f"fields game.id, game.slug, game.name, platform.name,"
                    f" date, human, date_format, release_region;"
                    f" where game = ({ids}) & date_format = {EXACT_DATE} & date != null;"
                    f" limit 500; offset {offset};",
                )
                rows.extend(page)
                if len(page) < 500:
                    break
                offset += 500
            for row in rows:
                out.append(
                    {
                        "igdb_id": row["game"]["id"],
                        "slug": row["game"]["slug"],
                        "title": row["game"]["name"],
                        "platform": (row.get("platform") or {}).get("name", "?"),
                        "date_unix": row["date"],
                        "human": row.get("human"),
                        "region": row.get("release_region"),
                    }
                )
        return out
