# Phice (personal fork)

A lightweight privacy-friendly alternative front-end for Facebook.

This is a personal fork of [Phice](https://codeberg.org/TronNerd82/phice), originally created by c4ffe14e (caffeine) and contributors. It fixes bugs and hardens the project for self-hosting while keeping the spirit of the original: simple, lightweight and JavaScript-free.

The fork is public because the AGPL requires the source of a modified version that is served over a network to be published, and so that the code deployed is exactly the code in this repository. Issues are welcome and will be looked at in detail. There are no public instances, and changes are not sent upstream.

> **Modification notice:** this is a modified version of Phice, changed by darlopvil since 2026-10-21. [Changes from the original](#changes-from-the-original) summarises them; the commit history and the closed issues have the details of every change.

Inspired by projects like [Nitter](https://github.com/zedeus/nitter), [Invidious](https://github.com/iv-org/invidious) and others.

## Screenshot

![screenshot](screenshot.png)

## Features

- Free and open-source
- Lightweight
- No ads
- No JavaScript required
- No account required
- No trackers
- Media proxied through the instance (can be turned off per user)
- RSS feeds for profiles and groups

## What it shows

Everything Facebook shows a logged-out visitor:

- Profiles and Pages: their posts, intro (links are named after the network they lead to) and the Reels, Photos, Videos and Events tabs. Facebook leaves reels out of the timeline it shows logged-out visitors, so they have their own tab. Facebook only shows the videos of Pages.
- Posts, videos, reels and photos, with their comments and replies.
- Public groups and their posts.
- Albums and event pages.
- Search (pages, posts, recent posts and people).
- `/share/` links, resolved to the page they point to.

Phice answers the same paths as facebook.com, so replacing the host of a Facebook link is enough:

| Page | Paths |
|---|---|
| Profile | `/<profile>`, `/<profile>/timeline`, `/profile.php?id=<id>`, `/people/<name>/<id>`, `/p/<profile>` |
| Profile tabs | `/<profile>/reels`, `/<profile>/reels_tab`, `/<profile>/photos`, `/<profile>/videos`, `/<profile>/events`, `/<profile>/upcoming_hosted_events`, `/<profile>/past_hosted_events` |
| Post | `/<author>/posts/<id>`, `/<author>/videos/<id>`, `/reel/<id>`, `/photo?fbid=<id>`, `/photo.php?fbid=<id>`, `/permalink.php?story_fbid=<id>`, `/story.php?story_fbid=<id>`, `/watch?v=<id>` |
| Group | `/groups/<group>`, `/groups/<group>/posts/<id>`, `/groups/<group>/permalink/<id>` |
| Other | `/media/set?set=<album>`, `/events/<id>`, `/search`, `/share/<...>` |

What Facebook keeps for logged-in users is not available: see [Won't fix](#wont-fix).

## Changes from the original

### Fixed

- Open redirect after saving the settings ([#1](https://github.com/darlopvil/phice-fork/issues/1)).
- Invalid settings cookies breaking every page with a 500 ([#2](https://github.com/darlopvil/phice-fork/issues/2)).
- Media served as `http://` (mixed content) behind a reverse proxy that terminates TLS ([#3](https://github.com/darlopvil/phice-fork/issues/3)).
- RSS feeds crashing or linking to the wrong pages, and feed readers subscribing to the home page instead of the feed ([#6](https://github.com/darlopvil/phice-fork/issues/6)).
- GIFs frozen in browsers that block autoplay ([#7](https://github.com/darlopvil/phice-fork/issues/7)).
- Facebook failures shown as tracebacks instead of error pages with proper status codes ([#8](https://github.com/darlopvil/phice-fork/issues/8)).
- The same hardcoded identity cookies sent to Facebook by every instance ([#9](https://github.com/darlopvil/phice-fork/issues/9)).
- Outdated Facebook query ids with no way to refresh them, responses rejected when only some fields failed, and posts with a single photo failing ([#11](https://github.com/darlopvil/phice-fork/issues/11)).
- Photos uploaded without a post failing to open ([#16](https://github.com/darlopvil/phice-fork/issues/16)).
- The first page of every profile coming from a slower fallback that downloads the whole Facebook page, because the query behind it stopped answering logged-out visitors ([#22](https://github.com/darlopvil/phice-fork/issues/22)).
- Posts failing to open when a comment has a video ([#25](https://github.com/darlopvil/phice-fork/issues/25)).
- Images in comments stretched to half the page and blurry: they are shown at the size Facebook shows them ([#26](https://github.com/darlopvil/phice-fork/issues/26)).
- Dates an hour off for half of the year: the time zone is now picked by name and follows daylight saving time ([#20](https://github.com/darlopvil/phice-fork/issues/20)).

### Added

- AGPL compliance: the source link points to this fork, plus a modification notice and footer credits ([#4](https://github.com/darlopvil/phice-fork/issues/4)).
- An in-memory cache of Facebook responses shared by all visitors, so repeated views and "Next" clicks send as few requests as possible ([#5](https://github.com/darlopvil/phice-fork/issues/5)).
- Proxied media that browsers can cache instead of downloading it again ([#13](https://github.com/darlopvil/phice-fork/issues/13)).
- A reproducible, unprivileged and health-checked container ([#10](https://github.com/darlopvil/phice-fork/issues/10)).
- Previous and Start buttons, one-click comment sorting, aligned reaction badges and a note when Facebook hides replies ([#12](https://github.com/darlopvil/phice-fork/issues/12)).
- A script that refreshes the query ids and checks the new data before writing it ([#11](https://github.com/darlopvil/phice-fork/issues/11)).
- Linked accounts in profile intros named after their network ([#14](https://github.com/darlopvil/phice-fork/issues/14)).
- Event pages ([#15](https://github.com/darlopvil/phice-fork/issues/15)).
- Profile tabs: Reels ([#21](https://github.com/darlopvil/phice-fork/issues/21)), Photos ([#16](https://github.com/darlopvil/phice-fork/issues/16)), Videos ([#17](https://github.com/darlopvil/phice-fork/issues/17)) and Events ([#18](https://github.com/darlopvil/phice-fork/issues/18)).

### Won't fix

- Hashtag pages ([#19](https://github.com/darlopvil/phice-fork/issues/19)): Facebook gives logged-out visitors a single post and answers every request for more with a rate limit.
- Logging in with a Facebook account: Phice stays anonymous, so replies and anything else Facebook keeps for logged-in users can't be shown. Phice says so instead of showing an empty thread.
- Marketplace and Stories: they need an account.

## Installation

### From source

Requirements:

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/)

```sh
uv sync
cp config.example.toml config.toml   # optional, see Configuration
uv run gunicorn -b 127.0.0.1:5000 -w 1 --threads 16 --timeout 60 "app:app"
```

### Docker

The image is built locally from this repository, with dependencies pinned by `uv.lock`:

```sh
cp config.example.toml config.toml
cp docker-compose.example.yaml docker-compose.yml
docker compose up -d --build
```

The example only binds Phice to `127.0.0.1:5000`: put a reverse proxy in front of it for public access, and set `trusted_proxies`. The container runs as an unprivileged user on a read-only filesystem. `/healthz` answers `ok` without contacting Facebook, for health checks and uptime monitors.

## Configuration

Copy `config.example.toml` to `config.toml`. Every option is optional.

`[phice]`:

| Option | Default | Description |
|---|---|---|
| `enable_rss` | `true` | Serve RSS feeds (`?rss=1`) for profiles and groups. |
| `proxy` | `""` | Outgoing HTTP/SOCKS proxy used for every request to Facebook. Empty means direct. |
| `trusted_proxies` | `0` | Number of reverse proxies in front of Phice whose `X-Forwarded-For` and `X-Forwarded-Proto` headers are trusted. |
| `show_tracebacks` | `false` | Show Python tracebacks on error pages. They are always written to the log. |

`[default_user_settings]` are the defaults for visitors; each visitor can override them in Settings:

| Option | Default | Values |
|---|---|---|
| `theme` | `"default"` | `default`, `light`, `black`, `catppuccin_mocha`, `catppuccin_latte` |
| `comments_sort` | `"filtered"` | `filtered`, `newest`, `all` |
| `proxy` | `true` | Proxy images and videos through the instance. |
| `expand` | `false` | Show long posts in full instead of "Read more". |
| `timezone` | `"UTC"` | Time zone name, such as `"Europe/Madrid"`; daylight saving time is applied automatically. An old UTC offset in hours (`-12` to `14`) still works, as a fixed offset. |

Invalid defaults stop the app at startup with an explicit error.

### Caching

Data fetched from Facebook is cached in memory and shared by all visitors, so repeated views and "Next" clicks send as few requests as possible: profile/group/post routes for 24 hours, profile, group and album headers for 1 hour, and rendered pages for 10 minutes. Pages cut short by a rate limit are not cached. The cache lives in the process, so run a single worker with threads (as in the commands above); several worker processes would each keep their own copy. Every request sent to Facebook is logged as `facebook: …`.

### Behind a reverse proxy

If Phice sits behind a reverse proxy that terminates TLS, set `trusted_proxies = 1` (one proxy) so the absolute URLs in RSS feeds use the scheme the visitor used. Pages always use relative URLs for media. Only `X-Forwarded-For` and `X-Forwarded-Proto` are trusted; the `Host` header is used as-is.

## Keeping up with Facebook

Phice talks to Facebook's internal GraphQL API with persisted query ids (`src/lib/data/doc_ids.json`) and Relay provider variables (`src/lib/data/extra_variables.json`). Facebook rotates them from time to time. When that happens, pages fail with "Facebook returned an error: <QueryName>: …" or "Facebook returned an unexpected response", and the log names the query that broke.

Check whether they changed (writes nothing, exits with 1 if they did):

```sh
uv run python scripts/fetch_data.py --check
```

Update them, then restart Phice:

```sh
uv run python scripts/fetch_data.py
```

With Docker, run it in a throwaway container from the repository root, then rebuild:

```sh
docker compose run --rm --build --no-deps --user "$(id -u):$(id -g)" \
  -v ./scripts:/src/phice/scripts:ro -v ./src/lib/data:/src/phice/src/lib/data \
  phice python scripts/fetch_data.py
docker compose up -d --build
```

The script loads a handful of public Facebook pages and their scripts (each script only once), so run it only when needed. Provider variables get the value Facebook's own provider modules return for a logged-out visitor, and none is ever removed, since queries that keep an older id may still need them. A query whose id can't be found keeps its current one. Before writing, the new data is checked with a few real queries (a profile, a post with comments, a photo post, a search, a group, an event and a profile's photos, videos, events and reels), and nothing is written if any of them fails. Facebook serves a different set of scripts on each load, so a pass can miss some definitions: `--save-dump DIR` keeps what was downloaded and `--from-dump DIR` reads it back instead of contacting Facebook.

## License

[AGPL-3.0](LICENSE), like the original project. If you run a modified version of this code as a network service, you must offer its source code to its users; the "Source code" link in the header and footer points to this repository.

Third-party assets keep their own licenses, stated in the files themselves:

- Icons from [Font Awesome Free](https://fontawesome.com/) — CC BY 4.0 (`src/static/assets/icons.svg`)
- [Catppuccin](https://github.com/catppuccin/catppuccin) themes — MIT (`src/static/themes/catppuccin_*.css`)
