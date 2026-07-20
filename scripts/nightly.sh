#!/usr/bin/env bash
# Nightly sync: pull Steam, refresh IGDB dates, reconcile the calendar.
# Each step is its own container run; the circuit breaker in the ledger
# stops repeated failures, and failures push via ntfy if configured.
set -uo pipefail
cd "$(dirname "$0")/.."
docker compose run --rm --no-deps job pull-steam
docker compose run --rm --no-deps job releases
docker compose run --rm --no-deps job calendar
