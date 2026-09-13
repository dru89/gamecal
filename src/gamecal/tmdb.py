"""TMDB client. The IGDB of movies: free API key, release dates by
country and type (theatrical, digital, physical)."""

import time

import httpx

from .config import TmdbConfig

API = "https://api.themoviedb.org/3"

# release_dates[].type
PREMIERE, LIMITED, THEATRICAL, DIGITAL, PHYSICAL, TV = 1, 2, 3, 4, 5, 6
TYPE_SHORT = {
    PREMIERE: "premiere",
    LIMITED: "limited",
    THEATRICAL: "theatrical",
    DIGITAL: "streaming",
    PHYSICAL: "physical",
    TV: "tv",
}


class TmdbError(RuntimeError):
    pass


class Tmdb:
    def __init__(self, cfg: TmdbConfig):
        if not cfg.api_key:
            raise TmdbError(
                "tmdb.api_key must be set in config.toml"
                " (free: themoviedb.org -> Settings -> API)"
            )
        self.cfg = cfg
        self.client = httpx.Client(timeout=30)
        self._last_request = 0.0

    def _get(self, path: str, **params) -> dict:
        for attempt in range(4):
            wait = 0.1 - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            r = self.client.get(
                f"{API}{path}", params={"api_key": self.cfg.api_key, **params}
            )
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1.5 * (attempt + 1))))
                continue
            r.raise_for_status()
            return r.json()
        raise TmdbError(f"TMDB rate limit on {path}: retries exhausted")

    def movie_with_releases(self, tmdb_id: int) -> dict:
        """Title, status, and this region's dated releases in one request.

        Returns {tmdb_id, title, year, status, releases: [{type, type_name,
        date}]} with dates as YYYY-MM-DD strings.
        """
        data = self._get(f"/movie/{tmdb_id}", append_to_response="release_dates")
        releases = []
        for entry in data.get("release_dates", {}).get("results", []):
            if entry.get("iso_3166_1") != self.cfg.region:
                continue
            for rel in entry.get("release_dates", []):
                date = (rel.get("release_date") or "")[:10]
                if not date:
                    continue
                releases.append(
                    {
                        "type": rel["type"],
                        "type_name": TYPE_SHORT.get(rel["type"], str(rel["type"])),
                        "date": date,
                    }
                )
        return {
            "tmdb_id": tmdb_id,
            "title": data.get("title") or f"tmdb {tmdb_id}",
            "year": (data.get("release_date") or "")[:4],
            "status": data.get("status"),
            "releases": sorted(releases, key=lambda r: r["date"]),
        }

    def search_movie(self, title: str, year: str | None = None) -> int | None:
        """Best-effort title(+year) -> tmdb_id, for diary CSV backfill."""
        params = {"query": title}
        if year:
            params["year"] = year
        results = self._get("/search/movie", **params).get("results", [])
        return results[0]["id"] if results else None
