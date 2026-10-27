"""Refresh the persisted GraphQL query ids (doc_ids) and Relay provider variables Phice needs.

Facebook rotates them from time to time. Run from the repository root:

    python scripts/fetch_data.py --check   # report changes, write nothing
    python scripts/fetch_data.py           # update src/lib/data/*.json

Facebook serves a different set of scripts on each load, so a pass can miss some definitions.
--save-dump DIR keeps the downloaded pages and scripts, and --from-dump DIR reads them back
instead of contacting Facebook, so a complete pass can be reused.

Before writing, the new data is checked with a few real queries (a profile with its next page, a
post with comments, a photo post with its full-size photo, a search, a group, an event and a profile's
photos, videos and events); nothing is written unless they all work.

Exit codes: 0 up to date or updated, 1 changes found (--check only), 2 some query ids not found
(their current ids are kept), 3 the new data failed the check (nothing written).
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, cast, override

import httpx
import orjson

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.lib import api, extractor
from src.lib.datatypes import EventPage, Scroll
from src.lib.wrappers import http_client

QUERY_NAMES: list[str] = [
    "CometAlbumPhotoCollagePaginationQuery",
    "CometGroupDiscussionRootSuccessQuery",
    "CometGroupRootQuery",
    "CometPhotoAlbumQuery",
    "CometPhotoRootContentQuery",
    "CometSinglePostDialogContentQuery",
    "CommentListComponentsRootQuery",
    "CommentsListComponentsPaginationQuery",
    "Depth1CommentsListPaginationQuery",
    "FBReelsRootWithEntrypointQuery",
    "GroupsCometDiscussionLayoutRootQuery",
    "GroupsCometFeedRegularStoriesPaginationQuery",
    "ProfileCometHeaderQuery",
    "ProfileCometTimelineFeedRefetchQuery",
    "ProfilePlusCometLoggedOutRootQuery",
    "SearchCometResultsPaginatedResultsQuery",
    "CometFeedStoryMenuQuery",
    "EventCometPermalinkHeaderQuery",
    "PublicEventCometAboutRootQuery",
    "ProfileCometTopAppSectionQuery",
    "ProfileCometAppCollectionPhotosRendererPaginationQuery",
    "PagesCometChannelTabAllVideosCardImplPaginationQuery",
    "ProfileCometAppCollectionEventsRendererPaginationQuery",
]
# Pages whose scripts define the queries above.
URLS: list[str] = [
    "https://www.facebook.com/facebook",
    "https://www.facebook.com/facebook/posts/1193563419482343",
    "https://www.facebook.com/reel/3894716004079577",
    "https://www.facebook.com/groups/python",
    "https://www.facebook.com/media/set/?set=a.10152716010956729",
    "https://www.facebook.com/photo/?fbid=10152716011096729&set=a.10152716010956729",
    "https://www.facebook.com/watch/search/?q=python",
    "https://www.facebook.com/events/2088317415270457",
    "https://www.facebook.com/facebook/photos",
    "https://www.facebook.com/facebook/videos",
    "https://www.facebook.com/public.gr/events",
]
# Preloaded scripts, plus the ones the page loads directly.
SCRIPT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r'<link rel="preload" href="([^"]+)" as="script"'),
    re.compile(r'<script\b[^>]*\bsrc="(https://[^"]+)"'),
)
PROVIDER_PREFIX: str = "__relay_internal__pv__"
PROVIDER_RE: re.Pattern[str] = re.compile(r"__relay_internal__pv__(\w+)relayprovider\b")
PROVIDER_MODULE_RE: re.Pattern[str] = re.compile(
    # The body is read with a lookahead so it doesn't swallow the next module: providers are tiny and
    # Facebook often bundles them back to back.
    r'__d\("([\w$]+)\.relayprovider",\[[^\]]*\],\(?function\([^)]*\)\{(?=(.{0,300}))',
    re.DOTALL,
)
GATEKEEPER_RE: re.Pattern[str] = re.compile(r'"(\d+)":\{"result":(true|false)')
CONFIG_RE: re.Pattern[str] = re.compile(r'\["(\w+Config)",\[\],(\{[^{}]*\}),\d+\]')
GK_CALL_RE: re.Pattern[str] = re.compile(r'r\("gkx"\)\("(\d+)"\)')
DATA_PATH: Path = Path(__file__).resolve().parent.parent / "src" / "lib" / "data"
PAUSE: float = 1.0  # between page loads, to stay gentle with Facebook

type Value = bool | int | str


def provider_value(body: str, gatekeepers: dict[str, bool], configs: dict[str, dict[str, object]]) -> Value:
    """Value a Relay provider module returns for a logged-out visitor, read from its source.

    Providers are tiny modules: a constant, a gatekeeper check (whose value Facebook embeds in
    the page), a ternary picking a string or a number, or a config flag. Anything else is false.
    """
    expr: str = (body.split("return", 1)[1] if "return" in body else body).split("}", 1)[0].strip().rstrip(";")
    gk_call: re.Match[str] | None = GK_CALL_RE.search(expr)
    gk: bool = bool(gk_call and gatekeepers.get(gk_call.group(1)))
    if "," in expr and expr.endswith("!0"):
        return True
    if choice := re.search(r'\?"(\w+)":"(\w+)"', expr):
        return choice.group(1) if gk else choice.group(2)
    if choice := re.search(r"\?(-?\d+):(-?\d+)", expr):
        return int(choice.group(1) if gk else choice.group(2))
    if expr in ("!0", "!1"):
        return expr == "!0"
    if expr.startswith('!r("gkx")'):
        return not gk
    if gk_call:
        return gk
    if config := re.search(r'r\("(\w+)"\)\.(\w+)', expr):
        return bool(configs.get(config.group(1), {}).get(config.group(2), False))
    return False


def download(client: httpx.Client, save_to: Path | None) -> Iterator[tuple[str, str]]:
    """Yield ("page", html) and ("script", js) from Facebook, each script once."""
    seen_scripts: set[str] = set()
    for i, url in enumerate(URLS):
        page: httpx.Response = client.get(url)
        if save_to:
            (save_to / f"page{i}.html").write_text(page.text)
        found: list[str] = [s for pattern in SCRIPT_RES for s in pattern.findall(page.text)]
        scripts: list[str] = list(dict.fromkeys(s for s in found if s not in seen_scripts))
        print(f"  {url} -> HTTP {page.status_code}, {len(scripts)} new scripts", file=sys.stderr)
        yield "page", page.text
        for script in scripts:
            seen_scripts.add(script)
            text: str = client.get(script).text
            if save_to:
                (save_to / f"js_{hashlib.sha1(script.encode(), usedforsecurity=False).hexdigest()[:12]}.js").write_text(text)
            yield "script", text
        time.sleep(PAUSE)


def read_dump(path: Path) -> Iterator[tuple[str, str]]:
    """Yield the pages and scripts saved by --save-dump."""
    pages: list[Path] = sorted(path.glob("page*.html"))
    scripts: list[Path] = sorted(path.glob("js_*.js"))
    print(f"  {path}: {len(pages)} pages, {len(scripts)} scripts", file=sys.stderr)
    for p in pages:
        yield "page", p.read_text(errors="replace")
    for p in scripts:
        yield "script", p.read_text(errors="replace")


def collect(sources: Iterator[tuple[str, str]]) -> tuple[dict[str, int], dict[str, Value], set[str]]:
    doc_ids: dict[str, int] = {}
    used: set[str] = set()
    modules: dict[str, str] = {}
    gatekeepers: dict[str, bool] = {}
    configs: dict[str, dict[str, object]] = {}
    id_patterns: dict[str, re.Pattern[str]] = {
        name: re.compile(rf'__d\("{name}_facebookRelayOperation",[^\n]*?exports="([0-9]+)"') for name in QUERY_NAMES
    }

    for kind, text in sources:
        if kind == "page":
            gatekeepers.update((k, v == "true") for k, v in GATEKEEPER_RE.findall(text))
            for name, obj in CONFIG_RE.findall(text):
                with suppress(ValueError):
                    configs[name] = json.loads(obj)
            continue
        for name, pattern in id_patterns.items():
            if name not in doc_ids and (match := pattern.search(text)):
                doc_ids[name] = int(match.group(1))
        used.update(PROVIDER_RE.findall(text))
        modules.update(PROVIDER_MODULE_RE.findall(text))

    # Facebook doesn't serve the same scripts on every load, so a provider can be used by a query
    # whose definition wasn't downloaded this time: it is still returned, without a value.
    providers: dict[str, Value] = {
        f"{PROVIDER_PREFIX}{name}relayprovider": provider_value(modules[name], gatekeepers, configs)
        for name in sorted(used)
        if name in modules
    }
    unknown: set[str] = {f"{PROVIDER_PREFIX}{name}relayprovider" for name in used if name not in modules}
    print(f"  {len(used)} provider variables seen, {len(providers)} with a known value", file=sys.stderr)
    return doc_ids, providers, unknown


def describe(label: str, result: object) -> tuple[str, bool]:
    """One-line summary of a check's result, and whether it passed."""
    if isinstance(result, EventPage):
        return (f"{result.name!r}, {result.time!r}", True) if result.name and result.time else ("no name or date", False)
    items, scroll = cast("tuple[Any, Scroll]", result)
    if label == "photo post":
        # The post renders even if the full-size lookup fails, so check it explicitly.
        full_size: bool = "dst-jpg_" not in getattr(items.attachment, "url", "")
        return ("full-size photo", True) if full_size else ("full-size photo lookup failed", False)
    count: int = len(
        getattr(items, "posts", None)
        or getattr(items, "media", None)
        or getattr(items, "events", None)
        or getattr(items, "comments", None)
        or items
    )
    return f"{count} items{' (rate limited)' if scroll.rate_limited else ''}", bool(count)


class HtmlFallbacks(logging.Handler):
    """Records the page HTML fallbacks a check needed: the queries behind them aren't working."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.routes: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if (message := record.getMessage()).startswith("facebook: html /"):
            self.routes.append(message.removeprefix("facebook: html ").removesuffix(" (rate-limit fallback)"))


def verify(doc_ids: dict[str, int], providers: dict[str, Value]) -> list[str]:
    """Run a few real queries with the candidate data; return what failed."""
    saved: tuple[dict[str, int], dict[str, Value]] = (dict(api.DOC_IDS), dict(api.EXTRA_VARIABLES))
    api.DOC_IDS.clear()
    api.DOC_IDS.update(doc_ids)
    api.EXTRA_VARIABLES.clear()
    api.EXTRA_VARIABLES.update(providers)
    failures: list[str] = []
    fallbacks: HtmlFallbacks = HtmlFallbacks()
    logging.getLogger("phice").addHandler(fallbacks)
    try:
        for label, fn, args in (
            ("profile", extractor.get_profile, ("facebook",)),
            ("post comments", extractor.get_post, ("1193563419482343", None, None, "newest")),
            ("photo post", extractor.get_post, ("10152716011096729", None, None, "filtered")),
            ("search", extractor.get_search, ("python", "pages")),
            ("group", extractor.get_group, ("python",)),
            ("event", extractor.get_event, ("2088317415270457",)),
            ("profile photos", extractor.get_profile_photos, ("facebook",)),
            ("profile videos", extractor.get_profile_videos, ("facebook",)),
            ("profile events", extractor.get_profile_events, ("public.gr",)),
        ):
            for cache in (extractor.HEADERS, extractor.PAGES):
                cache.clear()
            fallbacks.routes.clear()
            try:
                summary, ok = describe(label, fn(*args))
            except Exception as e:
                summary, ok = f"FAILED ({e})", False
            if ok and fallbacks.routes:
                summary, ok = f"{summary}, but only through the page HTML ({', '.join(fallbacks.routes)})", False
            print(f"  check {label}: {summary}", file=sys.stderr)
            if not ok:
                failures.append(f"{label}: {summary}")
            time.sleep(PAUSE)
    finally:
        logging.getLogger("phice").removeHandler(fallbacks)
        api.DOC_IDS.clear()
        api.DOC_IDS.update(saved[0])
        api.EXTRA_VARIABLES.clear()
        api.EXTRA_VARIABLES.update(saved[1])
    return failures


def merge[T](old: dict[str, T], new: dict[str, T], names: list[str] | None = None) -> dict[str, T]:
    """New values over old ones, keeping the old file's order and every old entry."""
    order: list[str] = [k for k in old if names is None or k in names] + [k for k in (names or new) if k not in old]
    return {k: new.get(k, old.get(k)) for k in order if k in new or k in old}  # pyright: ignore[reportReturnType]


def report(label: str, old: dict[str, Value], new: dict[str, Value]) -> bool:
    changed: list[str] = [k for k in new if k in old and old[k] != new[k]]
    added: list[str] = [k for k in new if k not in old]
    print(f"{label}: {len(changed)} changed, {len(added)} new")
    for k in changed:
        print(f"  ~ {k.removeprefix(PROVIDER_PREFIX)}: {old[k]!r} -> {new[k]!r}")
    for k in added:
        print(f"  + {k.removeprefix(PROVIDER_PREFIX)}: {new[k]!r}")
    return bool(changed or added)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report changes without writing anything")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--save-dump", type=Path, metavar="DIR", help="also save the downloaded pages and scripts")
    source.add_argument("--from-dump", type=Path, metavar="DIR", help="read pages and scripts saved by --save-dump")
    args = parser.parse_args()

    if args.from_dump:
        doc_ids, providers, unknown = collect(read_dump(args.from_dump))
    else:
        if args.save_dump:
            args.save_dump.mkdir(parents=True, exist_ok=True)
        with http_client(follow_redirects=True) as client:
            doc_ids, providers, unknown = collect(download(client, args.save_dump))

    old_ids: dict[str, int] = orjson.loads((DATA_PATH / "doc_ids.json").read_bytes())
    old_providers: dict[str, Value] = orjson.loads((DATA_PATH / "extra_variables.json").read_bytes())

    # A query whose module isn't loaded by these pages keeps its current id. Provider variables are
    # never dropped either: queries that still use an older id may still require them.
    missing: list[str] = [name for name in QUERY_NAMES if name not in doc_ids]
    if missing:
        print("Not found in Facebook's scripts, current ids kept:", *missing, file=sys.stderr)
    new_ids: dict[str, int] = merge(old_ids, doc_ids, QUERY_NAMES)
    # Providers whose definition wasn't downloaded keep their current value, or default to false.
    new_providers: dict[str, Value] = merge(
        old_providers, providers | {k: old_providers.get(k, False) for k in unknown - providers.keys()}
    )
    if guessed := sorted(k for k in unknown if k not in old_providers and k not in providers):
        print(
            "Provider definitions not found, defaulting to false:",
            *(k.removeprefix(PROVIDER_PREFIX) for k in guessed),
            file=sys.stderr,
        )

    ids_differ: bool = report("doc_ids", dict(old_ids), dict(new_ids))
    providers_differ: bool = report("extra_variables", old_providers, new_providers)

    if not args.check:
        if not ids_differ and not providers_differ:
            print("Already up to date.")
        elif failures := verify(new_ids, new_providers):
            print("The new data failed the check, nothing was written:", *failures, sep="\n  ")
            return 3
        else:
            (DATA_PATH / "doc_ids.json").write_bytes(orjson.dumps(new_ids, option=orjson.OPT_INDENT_2))
            (DATA_PATH / "extra_variables.json").write_bytes(orjson.dumps(new_providers, option=orjson.OPT_INDENT_2))
            print("Data files updated.")

    if missing:
        return 2
    return 1 if args.check and (ids_differ or providers_differ) else 0


if __name__ == "__main__":
    sys.exit(main())
