"""Steam Web API client. Read-only, official API, needs api_key + steam_id.

Wishlist: IWishlistService/GetWishlist (works for public profiles).
Library:  IPlayerService/GetOwnedGames (playtime_forever minutes,
          rtime_last_played unix ts).
"""

import httpx

from .config import SteamConfig

API = "https://api.steampowered.com"


class SteamError(RuntimeError):
    pass


class Steam:
    def __init__(self, cfg: SteamConfig):
        if not cfg.api_key or not cfg.steam_id:
            raise SteamError(
                "steam.api_key and steam.steam_id must be set in config.toml "
                "(key: https://steamcommunity.com/dev/apikey)"
            )
        self.cfg = cfg
        self.client = httpx.Client(timeout=30)

    def _get(self, path: str, **params) -> dict:
        params.setdefault("key", self.cfg.api_key)
        r = self.client.get(f"{API}/{path}", params=params)
        r.raise_for_status()
        return r.json()

    def wishlist(self) -> list[dict]:
        """Wishlist items: [{external_id: appid, priority, date_added}]."""
        data = self._get(
            "IWishlistService/GetWishlist/v1/", steamid=self.cfg.steam_id
        )
        items = data.get("response", {}).get("items", [])
        return [
            {
                "external_id": it["appid"],
                "priority": it.get("priority"),
                "date_added": it.get("date_added"),
            }
            for it in items
        ]

    def owned_games(self) -> list[dict]:
        """Owned games with playtime and last-played, including free games."""
        data = self._get(
            "IPlayerService/GetOwnedGames/v1/",
            steamid=self.cfg.steam_id,
            include_appinfo=1,
            include_played_free_games=1,
        )
        games = data.get("response", {}).get("games", [])
        return [
            {
                "external_id": g["appid"],
                "title": g.get("name"),
                "playtime_minutes": g.get("playtime_forever", 0),
                "last_played": g.get("rtime_last_played", 0),
            }
            for g in games
        ]

    def app_names(self, appids: list[int]) -> dict[int, str]:
        """Resolve appids to names via the store API (no key needed)."""
        names: dict[int, str] = {}
        for appid in appids:
            r = self.client.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "filters": "basic"},
            )
            if r.status_code != 200:
                continue
            entry = r.json().get(str(appid), {})
            if entry.get("success"):
                names[appid] = entry["data"]["name"]
        return names
