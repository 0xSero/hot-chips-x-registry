# Hot Chips X signal monitor

A read-only X API monitor for Hot Chips 38. It polls recent search, stores normalized posts in SQLite, exports JSON/JSONL, and regenerates a local signal-ranked HTML index.

## Safety boundary

- X reads only: `GET /2/tweets/search/recent`.
- No post, reply, follow, like, DM, browser scraping, or OAuth grant.
- No token copies. By default it reads the current access token from the dedicated local.ai bot's owner-only SQLite file and never refreshes it.
- Per-lane `since_id` cursors avoid re-fetching old results.
- `daily_resource_ceiling` stops collection after 500 returned Post resources per UTC day. X's exact rates live in the Developer Console; the public pricing docs do not currently print a per-Post search rate.
- If X returns its monthly spend-cap error, the collector records a paused state through the next monthly boundary, exits cleanly, and keeps publishing the existing registry instead of retrying every five minutes.

## Run

```sh
./run-monitor.sh
./serve-index.sh
open http://127.0.0.1:8787/
```

Artifacts:

- `docs/index.html` — searchable registry and GitHub Pages source
- `docs/posts.json` — normalized array
- `docs/posts.jsonl` — one normalized Post per line
- `state/hot-chips.sqlite3` — durable source index, cursors, run ledger, resource ledger
- `state/monitor.log` — poll log

## Automation

```sh
./install-launchd.sh
launchctl print gui/$(id -u)/com.sero.hot-chips-x-monitor
```

The LaunchAgent polls every five minutes and publishes changed `docs/` artifacts to `origin/main`. The HTML page refreshes every two minutes. Change the queries or daily ceiling in `config.json`; tokens do not belong there.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile hot_chips_monitor.py
```
