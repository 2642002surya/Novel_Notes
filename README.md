# Novel Checking Bot

Indexes novel links shared in your server, flags duplicates, and lets members
**track** novels and get **new-chapter alerts** — all through rich embeds and
interactive Discord components.

Default prefix: `i novel ` (or @mention the bot).

## Commands

| Command | What it does |
|---|---|
| `track <title or link>` | Track a novel. One match → instant card; several → multi-select menu. |
| `untrack <title>` | Stop tracking (select menu if several match). |
| `library` / `library server` | Paginated list of what *you* track / everything indexed here. Pick a novel to open its card. |
| `novelinfo <title or link>` | Novel card: cover, author, status, latest chapter, release time, tracking state. |
| `novelsearch <query>` | Paginated results with a select menu to open the exact match. |
| `duplicates` | Paginated list of repeated links. |
| `checkreleases` | (Manage Server) check tracked novels for new chapters now. |
| `novelscan`, `help`, `botinfo`, … | Unchanged, now using the shared colour theme. |

## Colour scheme

Green `#2ECC71` new release / now tracking · Blue `#3498DB` info & search ·
Gold `#F1C40F` library lists · Red `#E74C3C` errors & untracking ·
Orange `#E67E22` duplicate warnings.

## New-chapter alerts

Every `RELEASE_CHECK_MINUTES` (default 60) the bot re-reads the page of each
tracked novel. When the latest chapter changes it posts a green embed in the
alert channel (`ALERT_CHANNEL_ID`) — or in the channel where the novel was first
shared — mentioning everyone who hasn't muted it. The alert has persistent
buttons that keep working after restarts: **Unsubscribe**, **Mute Pings**
(toggle, per user), plus **View Chapter**, **Source Page**, **Novel Updates**.

The first time a novel is read it is stored as a silent baseline, so tracking
never triggers a false "new release".

## How novel details are found

Sites have no common format, so details are **best-effort**: JSON-LD and
Open Graph tags (title, cover, author, site name, icon), "Author:" / "Status:"
labels, and the highest-numbered "Chapter N" link on the page. Anything not
found shows as *Unknown*. Pages behind bot protection (e.g. Cloudflare
challenges) can't be read and fall back to a title derived from the URL.

Fetching is hardened against SSRF: only public http(s) hosts, validated at
connect time and on every redirect, with size and time limits.

## Upgrading

Drop these files over your existing folder. Your `novel_index.db` migrates
automatically (two new tables: `novel_meta`, `subscriptions`). No new
dependencies. New optional `.env` settings: `RELEASE_CHECK_MINUTES`,
`METADATA_CACHE_HOURS`. The bot needs **Send Messages** and **Embed Links**.

## Layout

- `novelbot/` — shared code: `embeds.py` (theme + builders), `views.py`
  (paginator, selects, buttons), `parser.py` / `fetcher.py` / `service.py`
  (metadata), kept out of `cogs/` so they aren't loaded as extensions.
- `cogs/library.py` — track / untrack / library / novelinfo + release checker.
