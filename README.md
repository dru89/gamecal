# gamecal

A personal game-release calendar. Pulls your Steam wishlist and library,
joins them against [IGDB](https://www.igdb.com/) for release dates, and
maintains a **Game Releases** calendar in Google Calendar — one all-day
event per game, on the date that matters for the platform you'll play it on.
A small web UI shows everything tracked and lets you follow non-Steam games
(Switch, PlayStation, Xbox) via IGDB search.

Built to run on a home server via cron; every job is a CLI subcommand with a
shared SQLite ledger, so manual runs and scheduled runs are the same thing.

## What it does

- **Steam wishlist → calendar.** Wishlisted games get an event on their PC
  release date, with Steam/IGDB/Backloggd links in the description.
- **Owned-but-unreleased too.** Steam removes purchases from your wishlist,
  so preorders and early-access games are detected from your library
  (via IGDB release status) and kept on the calendar for their 1.0 date.
- **Non-Steam games.** Search IGDB from the web UI and pick a platform;
  the event uses that platform's date and first-party store link
  (PlayStation Store / Xbox Marketplace; IGDB has no Nintendo eShop links).
- **Sane date picking.** One event per game: earliest upcoming date on your
  preferred platform. Fuzzy dates (Q4 2026, TBD) are excluded until they
  firm up. A game already released on your platform gets no event for
  later console ports.
- **Reconciliation, not append.** Events are keyed by IGDB id in private
  extended properties. Dates that slip get patched; untracked games get
  their future events deleted; past events stay as history.
- **Backlog signals.** A nightly `signals` job turns Steam playtime into
  curation nudges on the site: recently played -> "mark it Playing,"
  30+ days idle -> "completed or shelved?", tracked release passed ->
  "it's out — move it along." Each links to the game's page; dismiss with
  one click. Nothing writes to Backloggd — the automation is the noticing.
- **Instant adds.** Tracking a game from the web UI fetches its dates and
  pushes the calendar event immediately; the nightly reconcile remains the
  authority.
- **Circuit breakers + ntfy.** Three consecutive failures disable a job
  until you reset it, with push notifications via [ntfy](https://ntfy.sh)
  if configured.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and three sets of credentials:

1. **Steam Web API key** — https://steamcommunity.com/dev/apikey — plus
   your steamID64. Your profile's game details must be public.
2. **IGDB via Twitch** — create an app at https://dev.twitch.tv/console
   (any OAuth redirect URL, e.g. `http://localhost`; confidential client),
   note the client ID and secret.
3. **Google Calendar** — a Google Cloud project with the Calendar API
   enabled and an OAuth **Desktop app** client. Download the client JSON to
   `data/google_client.json`. First `gamecal calendar` run opens a browser
   for consent; the token is stored in `data/google_token.json`.
   (Workspace users: mark the app *Internal*. Otherwise publish the consent
   screen to production so refresh tokens don't expire after 7 days.)

```sh
cp config.example.toml config.toml   # fill in credentials + platforms
uv run gamecal pull-steam            # wishlist + library into the ledger
uv run gamecal releases              # join against IGDB release dates
uv run gamecal calendar --dry-run    # show the event plan
uv run gamecal calendar              # apply it
uv run gamecal serve                 # web UI on 127.0.0.1:8787
```

Other commands: `watch add <igdb-slug> [--platform "Nintendo Switch"]`,
`report`, `breaker status|reset <job>`.

## Deploying

`Dockerfile` and `compose.yaml` are included. The `web` service runs the UI
(bind it to a private interface via `BIND_IP`, e.g. a Tailscale address);
`scripts/nightly.sh` runs the three sync jobs via `docker compose run` and
is meant to be called from cron:

```
15 4 * * * /path/to/gamecal/scripts/nightly.sh >> /path/to/gamecal/data/nightly.log 2>&1
```

Copy `config.toml` and the `data/` directory (ledger, Google client +
token) when moving hosts — the calendar's ID lives in the ledger, and a
fresh one would create a duplicate calendar.

## Notes

- IGDB is accessed via Twitch client-credentials; no user auth needed.
- The Steam API is read-only and official. Nothing here writes to Steam,
  Backloggd, or IGDB.
- Release rows are snapshotted per run into the ledger (`observations`),
  so the web UI works even when IGDB is down.
