# backloggd-sync

Personal media-backlog automation: pulls the Steam wishlist/library, joins
against IGDB for release dates, and (eventually) maintains a Google Calendar
of upcoming releases. Runs locally or on a home server via cron; every job is
a CLI subcommand with a shared SQLite ledger and per-job circuit breakers.

## Setup

    cp config.example.toml config.toml   # fill in Steam + IGDB credentials
    uv run backloggd-sync pull-steam
    uv run backloggd-sync releases
    uv run backloggd-sync report

## Status

- [x] Ledger (runs, observations, actions, attention queue, breakers)
- [x] Steam pull (wishlist + owned via official Web API)
- [x] IGDB join (appid → game via external_games; exact-dated releases only)
- [ ] Google Calendar reconciler
- [ ] Digest site on ds9
- Backloggd read/write: **on hold** — site is behind BotStopper/Anubis with a
  hard deny for automated browsers. Do not scrape without an explicit decision.
