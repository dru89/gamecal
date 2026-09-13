"""Letterboxd sources.

Officially-provided surfaces where they exist: the diary RSS feed (recent
watches, with tmdb ids included) and the CSV export (full-history backfill).
The public watchlist pages are scraped politely — one paginated sweep a
night, >=1s between requests — and film -> TMDB id lookups are cached in
the ledger forever, so each film page is fetched at most once, ever.
"""

import csv
import re
import time
from xml.etree import ElementTree

import httpx

BASE = "https://letterboxd.com"
UA = "gamecal-personal-sync/1.0 (+https://github.com/dru89/gamecal)"

LB_NS = "https://letterboxd.com"
TMDB_NS = "https://themoviedb.org"


class LetterboxdError(RuntimeError):
    pass


def diary_key(tmdb_id: int | None, title: str, watched: str) -> str:
    """Natural key for a diary entry, stable across RSS and CSV sources:
    same film watched on the same day is the same entry."""
    ident = str(tmdb_id) if tmdb_id else re.sub(r"[^a-z0-9]+", "-", (title or "").lower())
    return f"{ident}:{watched}"


class Letterboxd:
    def __init__(self, username: str):
        if not username:
            raise LetterboxdError("letterboxd.username must be set in config.toml")
        self.username = username
        self.client = httpx.Client(
            timeout=30, headers={"User-Agent": UA}, follow_redirects=True
        )
        self._last_request = 0.0

    def _get(self, path: str) -> str:
        wait = 1.0 - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()
        r = self.client.get(f"{BASE}{path}")
        r.raise_for_status()
        return r.text

    def watchlist(self) -> list[str]:
        """Film slugs on the public watchlist, all pages."""
        slugs: list[str] = []
        page = 1
        while page <= 100:  # sanity bound
            suffix = "" if page == 1 else f"page/{page}/"
            html = self._get(f"/{self.username}/watchlist/{suffix}")
            found = re.findall(r'data-item-slug="([^"]+)"', html)
            new = [s for s in found if s not in slugs]
            if not new:
                break
            slugs.extend(new)
            page += 1
        return slugs

    def film_tmdb_id(self, slug: str) -> int | None:
        html = self._get(f"/film/{slug}/")
        m = re.search(r'data-tmdb-id="(\d+)"', html)
        return int(m.group(1)) if m else None

    def diary(self) -> list[dict]:
        """Recent diary entries from the RSS feed (officially provided;
        includes tmdb ids and watched dates)."""
        xml = self._get(f"/{self.username}/rss/")
        root = ElementTree.fromstring(xml)
        out = []
        for item in root.iter("item"):
            def text(tag: str, ns: str | None = None) -> str | None:
                el = item.find(f"{{{ns}}}{tag}" if ns else tag)
                return el.text if el is not None else None

            watched = text("watchedDate", LB_NS)
            if not watched:
                continue  # list activity, not a watch
            link = text("link") or ""
            slug_m = re.search(r"/film/([^/]+)/", link)
            tmdb = text("movieId", TMDB_NS)
            title = text("filmTitle", LB_NS) or ""
            out.append(
                {
                    "external_id": diary_key(int(tmdb) if tmdb else None, title, watched),
                    "guid": text("guid"),
                    "title": title,
                    "year": text("filmYear", LB_NS),
                    "watched": watched,
                    "rewatch": text("rewatch", LB_NS) == "Yes",
                    "rating": text("memberRating", LB_NS),
                    "tmdb_id": int(tmdb) if tmdb else None,
                    "slug": slug_m.group(1) if slug_m else None,
                    "link": link,
                }
            )
        return out


def parse_diary_csv(path: str) -> list[dict]:
    """Rows from a Letterboxd diary.csv export (Settings -> Data).
    Columns: Date, Name, Year, Letterboxd URI, Rating, Rewatch, Tags,
    Watched Date. TMDB ids are resolved separately (the export has none)."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            watched = (row.get("Watched Date") or row.get("Date") or "").strip()
            title = (row.get("Name") or "").strip()
            if not watched or not title:
                continue
            rows.append(
                {
                    "title": title,
                    "year": (row.get("Year") or "").strip(),
                    "watched": watched,
                    "rewatch": (row.get("Rewatch") or "").strip().lower() == "yes",
                    "rating": (row.get("Rating") or "").strip() or None,
                    "link": (row.get("Letterboxd URI") or "").strip(),
                    "tmdb_id": None,
                    "slug": None,
                }
            )
    return rows
