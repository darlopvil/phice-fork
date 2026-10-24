# Phice (personal fork)

A lightweight privacy-friendly alternative front-end for Facebook.

This is a personal fork of [Phice](https://codeberg.org/TronNerd82/phice), originally created by c4ffe14e (caffeine) and contributors. It fixes bugs and hardens the project for self-hosting while keeping the spirit of the original: simple, lightweight and JavaScript-free.

It is maintained for a single private instance. There are no public instances, and fixes stay in this fork.

> **Modification notice:** this is a modified version of Phice, changed by darlopvil since 2026-10-21. See the commit history and the closed issues of this repository for the details of every change.

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
| `timezone` | `0` | UTC offset in hours, from `-12` to `14`. |

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

The script loads a handful of public Facebook pages and their scripts (each script only once), so run it only when needed. Provider variables get the value Facebook's own provider modules return for a logged-out visitor, and none is ever removed, since queries that keep an older id may still need them. A query whose id can't be found keeps its current one. Before writing, the new data is checked with a few real queries (a profile, a post with comments, a photo post, a search, a group, an event and a profile's photos and videos), and nothing is written if any of them fails. Facebook serves a different set of scripts on each load, so a pass can miss some definitions: `--save-dump DIR` keeps what was downloaded and `--from-dump DIR` reads it back instead of contacting Facebook.

## License

[AGPL-3.0](LICENSE), like the original project. If you run a modified version of this code as a network service, you must offer its source code to its users; the "Source code" link in the header and footer points to this repository.

Third-party assets keep their own licenses, stated in the files themselves:

- Icons from [Font Awesome Free](https://fontawesome.com/) — CC BY 4.0 (`src/static/assets/icons.svg`)
- [Catppuccin](https://github.com/catppuccin/catppuccin) themes — MIT (`src/static/themes/catppuccin_*.css`)
