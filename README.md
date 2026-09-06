# SN Radar

One page that pulls ServiceNow ecosystem content (YouTube, Community articles, creator blogs, podcasts, press) into a single feed, with a shortlist you can turn into a content calendar.

## What it does

- Fetches every feed in `sources.json`, normalizes the entries, and merges them with the previous run so history accumulates past what each feed exposes (90 days by default).
- Renders `docs/index.html`, a self-contained dashboard with filters by window, type, origin, and source, full-text search, a topic strip built from the last 14 days of headlines, and a plan rail for shortlisting.
- Writes `docs/items.json` (the same normalized data, for agents or scripts) and `docs/status.json` (per-source fetch health).
- Plan, seen marks, and filter state live in the browser's local storage. Use Backup and Restore in the plan rail to move them between browsers.

## Run it locally

    pip install -r requirements.txt
    python build.py
    open docs/index.html

`python build.py --dry-run` fetches and reports without writing anything.

## Hosted, hands-off

1. Push this folder to a GitHub repository.
2. Settings, Pages: deploy from branch `main`, folder `/docs`.
3. The workflow in `.github/workflows/build.yml` rebuilds every six hours and commits the refreshed dashboard. Run it once manually from the Actions tab to publish immediately.

## Add or change sources

Edit `sources.json`. Each entry needs `id`, `name`, `type` (video, community, blog, podcast, news), `group` (official, creator, press), and `url`. Set `"enabled": false` to keep a source in the list without fetching it.

- YouTube channel feed: `https://www.youtube.com/feeds/videos.xml?channel_id=<channel id>`. The channel id appears in the page source of any channel page as `channel_id=` or `externalId`.
- ServiceNow Community boards: `https://www.servicenow.com/community/s/{community_key}/rss/board?board.id=<board id>`. Leave `{community_key}` as is; the build resolves the current key from the Community homepage on each run.
- Podcasts: the RSS feed from the show's host. `https://itunes.apple.com/search?term=<show>&media=podcast` returns `feedUrl` for any show in Apple Podcasts.

`exclude_title_patterns` in `settings` drops headlines that match, which keeps stock-ticker chatter out of the news feed.

## Files

| Path | Purpose |
|---|---|
| `build.py` | Fetch, normalize, merge, render |
| `sources.json` | Sources and settings |
| `template.html` | Dashboard markup, styles, and behavior; `build.py` injects the data blob |
| `docs/` | Published output (index.html, items.json, status.json) |
| `.github/workflows/build.yml` | Six-hourly rebuild on GitHub Actions |
