import functools
import logging
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import cast
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse

import httpx
import orjson

from .api import API_ERROR_CODES, Api
from .cache import MISS, TTLCache
from .datatypes import JSON, Album, EventPage, Feed, Photo, Post, Scroll, SearchItem, User
from .exceptions import NetworkError, NotFoundError, ParsingError, ResponseError
from .parsers import (
    parse_album_item,
    parse_collection_event,
    parse_collection_media,
    parse_comment,
    parse_event,
    parse_page_video,
    parse_post,
    parse_profile_reel,
    parse_search,
)
from .utils import base64s, catch_rate_limit, network_name, unwrap_link, urlbasename
from .wrappers import http_client

COMMENT_FILTERS: defaultdict[str | None, str] = defaultdict(
    lambda: "RANKED_FILTERED_INTENT_V1",
    {
        "all": "CHRONOLOGICAL_UNFILTERED_INTENT_V1",
        "newest": "REVERSE_CHRONOLOGICAL_UNFILTERED_INTENT_V1",
    },
)
SEARCH_TYPES: defaultdict[str, tuple[str, list[str]]] = defaultdict(
    lambda: ("PAGES_TAB", []),
    {
        "pages": ("PAGES_TAB", []),
        "posts": ("POSTS_TAB", []),
        "recent_posts": ("POSTS_TAB", ['{"name":"recent_posts","args":""}']),
        "people": ("PEOPLE_TAB", []),
    },
)
PROFILE_PAGING: int = 1  # timeline pages per page: Facebook gives logged-out visitors 3 posts per page
COMMENT_PAGING: int = 2
GROUP_PAGING: int = 4
ALBUM_PAGING: int = 3
SEARCH_PAGING: int = 3
MEDIA_PAGE: int = 24  # photos per page of a profile tab
VIDEOS_PAGE: int = 12  # videos per page: their thumbnails are full size
EVENTS_PAGE: int = 12
# Facebook's ids for the apps behind profile sections, as found in their section tokens.
PROFILE_SECTION_APPS: dict[str, str] = {"photos": "2305272732", "events": "2344061033", "reels_tab": "168684841768375"}
EXCLUDED_SHARE_HEADERS: list[str] = ["rdid", "share_url", "idorvanity"]

# Everything fetched from Facebook is the same for every visitor, so it is cached
# in memory and shared: a repeated view costs no request, and a "Next" click only
# pays for the new page instead of re-fetching the profile/group header.
ROUTE_TTL: int = 24 * 60 * 60  # vanity name -> id, post token -> story id
HEADER_TTL: int = 60 * 60  # profile/group/album header, intro and description
PAGE_TTL: int = 10 * 60  # a rendered page of posts, comments or results
ROUTES: TTLCache = TTLCache(maxsize=2048)
HEADERS: TTLCache = TTLCache(maxsize=512)
PAGES: TTLCache = TTLCache(maxsize=256)

log: logging.Logger = logging.getLogger("phice")


def _guard[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turn a payload shape the parsers don't expect into a ParsingError, logging where it broke."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            log.exception("facebook: unexpected response in %s", fn.__name__)
            raise ParsingError(f"{fn.__name__}: unexpected response") from e

    return wrapper


def _route(api: Api, path: str) -> tuple[JSON | None, str | None]:
    if (hit := ROUTES.get(("route", path))) is not MISS:
        return cast("tuple[JSON | None, str | None]", hit)
    route, route_type = api.route(path)
    if route:
        ROUTES.set(("route", path), (route, route_type), ROUTE_TTL)
    return route, route_type


def _user_id(api: Api, username: str) -> str:
    route, route_type = _route(api, username)
    if not route or route_type != "profile":
        raise NotFoundError(f"Profile {username} not found")
    return route["rootView"]["props"]["userID"]


def _cache_page[T](key: tuple[str | None, ...], result: T, scroll: Scroll) -> T:
    # A page cut short by a rate limit is not cached, so the next view retries it.
    if not scroll.rate_limited:
        PAGES.set(key, result, PAGE_TTL)
    return result


@_guard
def get_profile(username: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("profile", username, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    feed: Feed
    with Api(proxy=proxy) as api:
        user_id: str = _user_id(api, username)

        posts_feed: list[JSON] | None = None
        first_page: bool = not scroll.cursor
        cached_header: object = HEADERS.get(("profile", user_id))
        if cached_header is not MISS and first_page:
            # Header known: page 1 only needs the first page of the timeline, not the header queries.
            try:
                posts_feed = api.ProfileCometTimelineFeedRefetchQuery(user_id, None)
            except ResponseError as e:
                if e.code != API_ERROR_CODES["rate_limit"]:
                    raise
                cached_header = MISS  # fall back to the full fetch, which can scrape the HTML
        if cached_header is not MISS:
            feed = replace(cast("Feed", cached_header), posts=[])
        else:
            feed, posts_feed, _ = _fetch_profile_header(api, user_id)
            HEADERS.set(("profile", user_id), replace(feed, posts=[]), HEADER_TTL)

        if first_page and posts_feed is not None:
            _add_timeline_page(feed, scroll, posts_feed)
        if scroll.has_next:
            with catch_rate_limit(scroll):
                # Page 1 already holds the first timeline page, so it pages once less.
                for _ in range(PROFILE_PAGING - 1 if first_page else PROFILE_PAGING):
                    _add_timeline_page(feed, scroll, api.ProfileCometTimelineFeedRefetchQuery(user_id, scroll.cursor))
                    if not scroll.has_next:
                        break

    return _cache_page(page_key, (feed, scroll), scroll)


def _part_data(part: JSON) -> JSON:
    return part.get("data") or {}


def _add_timeline_page(feed: Feed, scroll: Scroll, response: list[JSON]) -> None:
    """Posts and next cursor from one page of a profile's timeline.

    The refetch query answers with a first edge, then one streamed post per part and the cursor in
    the last one; the page HTML's first query (rate-limit fallback) only gives its first edge.
    """
    data: JSON = response[0]["data"]
    root: JSON = data.get("node") or data["user"]
    if edges := root["timeline_list_feed_units"]["edges"]:
        feed.posts.append(parse_post(edges[0]["node"]))
    if "node" in data:
        parts: list[JSON] = [i for i in response[1:] if "ProfileCometTimelineFeed_user" in i.get("label", "")]
        feed.posts.extend(parse_post(i["data"]["node"]) for i in parts if _part_data(i).get("node"))
    page_info: JSON | None = next(
        (_part_data(i)["page_info"] for i in reversed(response[1:]) if _part_data(i).get("page_info")), None
    )
    if page_info is None:
        raise ParsingError("Couldn't find page_info")
    scroll.cursor = page_info["end_cursor"]
    scroll.has_next = page_info["has_next_page"]


def _profile_status(side: JSON) -> str | None:
    """The status text at the top of a profile's intro: a Page's description."""
    for view in side["profile_tile_views"]["nodes"]:
        renderer: JSON = view.get("view_style_renderer") or {}
        tile_view: JSON = renderer.get("view") or {}
        tile_items: JSON = tile_view.get("profile_tile_items") or {}
        items: list[JSON] = tile_items.get("nodes") or []
        for item in items:
            node: JSON = item.get("node") or {}
            status: JSON = node.get("profile_status_text") or {}
            if text := status.get("text"):
                return str(text)
    return None


def _profile_header(
    api: Api, user_id: str, section: Callable[[], JSON | None] | None = None, *, needs_page_id: bool = False
) -> tuple[Feed, JSON | None]:
    """A profile's header for its tabs, from the cache when possible.

    Tabs fetch it without the first page of the timeline, which only the Posts tab shows. That page is
    where some Pages give their id and description, so a header built without it is cached apart, and a
    tab that needs the Page id (`needs_page_id`) fetches the whole header when that one lacks it.
    A tab that also needs its section passes the request for it, which goes out with the header's.
    """
    cached: object = HEADERS.get(("profile", user_id))
    if cached is MISS:
        cached = HEADERS.get(("profile_tab", user_id))
        if cached is not MISS and needs_page_id and not cast("Feed", cached).page_id:
            cached = MISS
    if cached is not MISS:
        return replace(cast("Feed", cached), posts=[], media=[], events=[], lists=[]), section() if section else None
    feed, _, section_data = _fetch_profile_header(api, user_id, section, with_timeline=needs_page_id)
    key: str = "profile" if needs_page_id else "profile_tab"
    HEADERS.set((key, user_id), replace(feed, posts=[], media=[], events=[], lists=[]), HEADER_TTL)
    return feed, section_data


def _profile_section(api: Api, user_id: str, username: str, section: str, *, first_part: bool = False) -> JSON | None:
    """The first page of a profile section, falling back to the page HTML when rate-limited."""
    token: str = base64s(f"app_section:{user_id}:{PROFILE_SECTION_APPS[section]}")
    try:
        response: list[JSON] = api.ProfileCometTopAppSectionQuery(user_id, token, first_part=first_part)
    except ResponseError as e:
        if e.code != API_ERROR_CODES["rate_limit"]:
            raise
        response = api.query_from_html(f"{username}/{section}").get("ProfileCometTopAppSectionQuery") or []
        if not response:
            raise
    return response[0]["data"]["node"]


def _add_media(feed: Feed, connection: JSON, scroll: Scroll) -> None:
    feed.media.extend(m for i in connection["edges"] if (m := parse_collection_media(i["node"], feed.id)))
    scroll.cursor = connection["page_info"]["end_cursor"]
    scroll.has_next = connection["page_info"]["has_next_page"]


@_guard
def get_profile_photos(username: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("profile_photos", username, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    with Api(proxy=proxy) as api:
        user_id: str = _user_id(api, username)
        collection_id: object = ROUTES.get(("photos", user_id))
        # The first page comes with the section; later ones only need its collection id.
        wants_section: bool = collection_id is MISS or not scroll.cursor
        feed, section = _profile_header(
            api, user_id, functools.partial(_profile_section, api, user_id, username, "photos") if wants_section else None
        )
        if wants_section:
            collections: list[JSON] = (section or {}).get("all_collections", {}).get("nodes") or []
            if not collections:
                return _cache_page(page_key, (feed, Scroll(None)), scroll)
            collection: JSON = collections[0]["style_renderer"]["collection"]
            collection_id = collection["id"]
            ROUTES.set(("photos", feed.id), collection_id, ROUTE_TTL)
            if not scroll.cursor:
                _add_media(feed, collection["pageItems"], scroll)
        with catch_rate_limit(scroll):
            for _ in range(3):
                if not scroll.has_next or len(feed.media) >= MEDIA_PAGE:
                    break
                _add_media(
                    feed,
                    api.ProfileCometAppCollectionPhotosRendererPaginationQuery(
                        str(collection_id),
                        scroll.cursor,
                        MEDIA_PAGE - len(feed.media),
                    )[0]["data"]["node"]["pageItems"],
                    scroll,
                )

    return _cache_page(page_key, (feed, scroll), scroll)


def _add_reels(feed: Feed, connection: JSON, scroll: Scroll) -> None:
    feed.media.extend(r for i in connection["edges"] if (r := parse_profile_reel(i)))
    scroll.cursor = connection["page_info"]["end_cursor"]
    scroll.has_next = connection["page_info"]["has_next_page"]


@_guard
def get_profile_reels(username: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("profile_reels", username, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    with Api(proxy=proxy) as api:
        user_id: str = _user_id(api, username)
        collection_id: object = ROUTES.get(("reels", user_id))
        # The section comes with the first ten reels; later pages only need its collection id.
        wants_section: bool = collection_id is MISS or not cursor
        feed, section = _profile_header(
            api,
            user_id,
            functools.partial(_profile_section, api, user_id, username, "reels_tab", first_part=True) if wants_section else None,
        )
        if wants_section:
            all_collections: JSON = (section or {}).get("all_collections") or {}
            collections: list[JSON] = all_collections.get("nodes") or []
            collection: JSON = collections[0]["style_renderer"]["collection"] if collections else {}
            if not collection.get("aggregated_fb_shorts"):
                return _cache_page(page_key, (feed, Scroll(None)), scroll)
            collection_id = collection["id"]
            ROUTES.set(("reels", feed.id), collection_id, ROUTE_TTL)
            if not cursor:
                _add_reels(feed, collection["aggregated_fb_shorts"], scroll)
        if cursor:
            response: list[JSON] = api.ProfileCometAppCollectionReelsRendererPaginationQuery(str(collection_id), cursor)
            _add_reels(feed, response[0]["data"]["node"]["aggregated_fb_shorts"], scroll)

    return _cache_page(page_key, (feed, scroll), scroll)


@_guard
def get_profile_videos(username: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("profile_videos", username, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    with Api(proxy=proxy) as api:
        feed, _ = _profile_header(api, _user_id(api, username), needs_page_id=True)
        if not feed.page_id:
            # Only Pages have a videos tab Facebook shows logged-out visitors.
            return _cache_page(page_key, (feed, Scroll(None)), scroll)
        connection: JSON
        try:
            response: list[JSON] = api.PagesCometChannelTabAllVideosCardImplPaginationQuery(feed.page_id, cursor, VIDEOS_PAGE)
            connection = response[0]["data"]["node"]["all_videos"]
        except ResponseError as e:
            if e.code != API_ERROR_CODES["rate_limit"] or cursor:
                raise
            parts: list[JSON] = api.query_from_html(f"{feed.token}/videos").get("CometProfilePlusVideosRootQuery") or []
            found: JSON | None = next((i["data"]["all_videos"] for i in parts if "all_videos" in (i.get("data") or {})), None)
            if found is None:
                raise
            connection = found
        feed.media.extend(parse_page_video(i["node"]) for i in connection["edges"])
        scroll.cursor = connection["page_info"]["end_cursor"]
        scroll.has_next = connection["page_info"]["has_next_page"]

    return _cache_page(page_key, (feed, scroll), scroll)


def _add_events(feed: Feed, connection: JSON, scroll: Scroll) -> None:
    feed.events.extend(e for i in connection["edges"] if (e := parse_collection_event(i["node"])))
    scroll.cursor = connection["page_info"]["end_cursor"]
    scroll.has_next = connection["page_info"]["has_next_page"]


@_guard
def get_profile_events(
    username: str,
    list_slug: str | None = None,
    cursor: str | None = None,
    *,
    proxy: str | None = None,
) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("profile_events", username, list_slug, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    with Api(proxy=proxy) as api:
        user_id: str = _user_id(api, username)
        # A profile's events come in lists ("Upcoming", "Past"...): (slug, name, collection id), the default first.
        lists: list[tuple[str, str, str]]
        first_page: JSON | None = None
        cached_lists: object = HEADERS.get(("events_lists", user_id))
        wants_section: bool = cached_lists is MISS or not (cursor or list_slug)
        feed, section = _profile_header(
            api, user_id, functools.partial(_profile_section, api, user_id, username, "events") if wants_section else None
        )
        if not wants_section:
            lists = cast("list[tuple[str, str, str]]", cached_lists)
        else:
            nav_collections: JSON = (section or {}).get("nav_collections") or {}
            all_collections: JSON = (section or {}).get("all_collections") or {}
            nav: list[JSON] = nav_collections.get("nodes") or []
            lists = [(urlbasename(i["url"]), i["name"], i["id"]) for i in nav]
            collections: list[JSON] = all_collections.get("nodes") or []
            if collections:
                collection: JSON = collections[0]["style_renderer"]["collection"]
                if not any(i[2] == collection["id"] for i in lists):
                    lists.insert(0, ("", collection.get("name") or "Events", collection["id"]))
                first_page = collection
            HEADERS.set(("events_lists", feed.id), lists, HEADER_TTL)
        feed.lists = [(slug, name) for slug, name, _ in lists]
        if not lists:
            return _cache_page(page_key, (feed, Scroll(None)), scroll)

        collection_id: str = lists[0][2]
        if list_slug:
            if (selected := next((i for i in lists if i[0] == list_slug), None)) is None:
                raise NotFoundError(f"{feed.name} has no {list_slug} list")
            collection_id = selected[2]
        if not cursor and first_page is not None and first_page["id"] == collection_id:
            _add_events(feed, first_page["pageItems"], scroll)
        elif not cursor:
            scroll.has_next = True  # the first page of another list: the pagination query without a cursor
        with catch_rate_limit(scroll):
            for _ in range(3):
                if not scroll.has_next or len(feed.events) >= EVENTS_PAGE:
                    break
                _add_events(
                    feed,
                    api.ProfileCometAppCollectionEventsRendererPaginationQuery(
                        collection_id,
                        scroll.cursor,
                        EVENTS_PAGE - len(feed.events),
                    )[0]["data"]["node"]["pageItems"],
                    scroll,
                )

    return _cache_page(page_key, (feed, scroll), scroll)


def _fetch_profile_header(
    api: Api, user_id: str, extra: Callable[[], JSON | None] | None = None, *, with_timeline: bool = True
) -> tuple[Feed, list[JSON] | None, JSON | None]:
    """Fetch and parse everything above the posts; also return the first timeline page for page 1.

    Its queries, and the extra one a tab may pass, don't depend on each other, so they go out at once,
    as a browser's would: the slowest one sets the time instead of their sum.
    """
    header_query: list[JSON]
    side_query: list[JSON]
    posts_query: list[JSON] | None = None
    extra_data: JSON | None = None
    with ThreadPoolExecutor(max_workers=4) as pool:
        extra_future: Future[JSON | None] | None = pool.submit(extra) if extra else None
        header_future: Future[list[JSON]] = pool.submit(api.ProfileCometHeaderQuery, user_id)
        side_future: Future[list[JSON]] = pool.submit(api.ProfilePlusCometLoggedOutRootQuery, user_id)
        posts_future: Future[list[JSON]] | None = (
            pool.submit(api.ProfileCometTimelineFeedRefetchQuery, user_id, None) if with_timeline else None
        )
        try:
            header_query = header_future.result()
            side_query = side_future.result()
            posts_query = posts_future.result() if posts_future is not None else None
        except ResponseError as e:
            if e.code != API_ERROR_CODES["rate_limit"]:
                raise
            html_query = api.query_from_html(f"profile.php?id={user_id}")
            if not html_query:
                raise

            header_query = html_query["ProfileCometHeaderQuery"]
            side_query = html_query["ProfilePlusCometLoggedOutRootQuery"]
            posts_query = html_query["ProfileCometTimelineFeedQuery"]
        if extra_future is not None:
            extra_data = extra_future.result()

    header: JSON = header_query[0]["data"]["user"]["profile_header_renderer"]["user"]
    side: JSON = side_query[-1]["data"]["profile_tile_sections"]["edges"][0]["node"]
    posts_feed: list[JSON] | None = posts_query

    feed = Feed(
        id=user_id,
        token=user_id if header["url"].startswith("https://www.facebook.com/people/") else urlbasename(header["url"]),
        name=header["name"],
        verified=header["show_verified_badge_on_profile"],
    )

    if private := header["wem_private_sharing_bundle"]["private_sharing_control_model_for_user"]:
        feed.is_private = private["private_sharing_enabled"]

    tabs: JSON = header.get("profile_tabs") or {}
    profile_user: JSON = tabs.get("profile_user") or {}
    posts_data: JSON = posts_feed[0]["data"] if posts_feed else {}
    posts_user: JSON = posts_data.get("node") or posts_data.get("user") or {}
    delegate_page: JSON | None = profile_user.get("delegate_page") or header.get("delegate_page") or posts_user.get("delegate_page")
    if delegate_page:
        feed.page_id = delegate_page.get("id")

    if header["profilePhoto"] and header["profilePicLarge"]:
        feed.profile_photo = Photo(
            url=header["profilePicLarge"]["uri"],
            id=header["profilePhoto"]["id"],
            alt_text=f"{header['name']}'s profile picture",
        )

    if cover_photo := (cast("JSON | None", header["cover_photo"]) or {}).get("photo"):
        feed.cover_photo = Photo(
            url=cover_photo["image"]["uri"],
            id=cover_photo["id"],
            alt_text=f"{header['name']}'s cover picture",
        )

    if header["profile_social_context"]:
        for i in header["profile_social_context"]["content"]:
            if "followers" in i["text"]["text"]:
                feed.followers = i["text"]["text"].split(" ", 1)[0]
            elif "following" in i["text"]["text"]:
                feed.following = i["text"]["text"].split(" ", 1)[0]
            elif "likes" in i["text"]["text"]:
                feed.likes = i["text"]["text"].split(" ", 1)[0]

    # A Page's description is the status text of its intro (the page HTML also carries it elsewhere).
    fallback_page: JSON = posts_user.get("delegate_page") or {}
    best_description: JSON = fallback_page.get("best_description") or {}
    if description := _profile_status(side) or best_description.get("text"):
        feed.description = description

    if side["profile_tile_section_type"] == "INTRO":
        for i in side["profile_tile_views"]["nodes"][1]["view_style_renderer"]["view"]["profile_tile_items"]["nodes"]:
            item: dict[str, str | None] = {"text": None, "url": None, "type": None, "network": None}
            renderer: JSON = i["node"]["timeline_context_item"]["renderer"]

            match renderer["__typename"]:
                case "WhatsappNumberIntroCardItemRenderer":
                    item["text"] = renderer["wa_number"]
                    item["url"] = renderer["wa_link"]
                case _:
                    context_item_title: JSON | None = renderer["context_item"]["title"]
                    if context_item_title is None:
                        continue

                    item["text"] = context_item_title["text"]
                    if subtitle := renderer["context_item"].get("subtitle"):
                        item["text"] += f" {subtitle['text']}"
                    if ranges := context_item_title["ranges"]:
                        url: str | None = ranges[0]["entity"]["url"]
                        item["url"] = unwrap_link(url) if url else url
            item["type"] = i["node"]["timeline_context_item"]["timeline_context_list_item_type"][11:].lower()
            if item["type"] == "other_account" and item["url"]:
                # Facebook only gives the username, so the network comes from the link.
                item["network"] = network_name(item["url"])

            feed.info.append(item)

    return feed, posts_feed, extra_data


@_guard
def get_post(
    token: str,
    cursor: str | None = None,
    focus: str | None = None,
    sort: str | None = "filtered",
    *,
    proxy: str | None = None,
) -> tuple[Post, Scroll]:
    sort_type: str = COMMENT_FILTERS[sort]
    page_key: tuple[str | None, ...] = ("post", token, cursor, focus, sort_type)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Post, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    post: Post
    with Api(proxy=proxy) as api:
        post_id: str | None = _resolve_post_id(api, token)
        if post_id is None:
            raise NotFoundError("Post not found")

        post_payload: JSON = api.CometSinglePostDialogContentQuery(post_id, focus)[0]["data"]["node"]

        post = parse_post(post_payload)

        if isinstance(post.attachment, Photo) and "dst-jpg_" in post.attachment.url:
            post.attachment.url = full_size_photo_url(api, post_id) or post.attachment.url

        if post.feedback_id is not None:
            comments_payload: JSON
            if sort_type != "RANKED_FILTERED_INTENT_V1":
                comments_payload = api.CommentListComponentsRootQuery(
                    post.feedback_id,
                    sort_type,
                    focus,
                )[0]["data"]["node"]["comment_rendering_instance_for_feed_location"]["comments"]
            else:
                comments_payload = post_payload["comet_sections"]["feedback"]["story"]["story_ufi_container"]["story"][
                    "feedback_context"
                ]["feedback_target_with_context"]["comment_list_renderer"]["feedback"][
                    "comment_rendering_instance_for_feed_location"
                ]["comments"]

            if focus:
                main_comment: JSON = comments_payload["edges"][0]["node"]
                for i in comments_payload["edges"]:
                    if i["node"]["legacy_fbid"] == focus:
                        main_comment = i["node"]
                        break

                post.comments.append(parse_comment(main_comment))
                if not scroll.cursor:
                    replies: JSON = main_comment["feedback"]["replies_connection"]

                    post.comments.extend(parse_comment(i["node"]) for i in replies["edges"])
                    scroll.cursor = replies["page_info"]["end_cursor"]
                    scroll.has_next = replies["page_info"]["has_next_page"]
                if scroll.has_next:
                    with catch_rate_limit(scroll):
                        for _ in range(COMMENT_PAGING):
                            next_replies: JSON = api.Depth1CommentsListPaginationQuery(
                                main_comment["feedback"]["id"],
                                main_comment["feedback"]["expansion_info"]["expansion_token"],
                                scroll.cursor,
                            )[0]["data"]["node"]["replies_connection"]

                            post.comments.extend(parse_comment(i["node"]) for i in next_replies["edges"])
                            scroll.cursor = next_replies["page_info"]["end_cursor"]
                            scroll.has_next = next_replies["page_info"]["has_next_page"]
                            if not scroll.has_next:
                                break
            else:
                if not scroll.cursor:
                    post.comments.extend(parse_comment(i["node"]) for i in comments_payload["edges"])
                    scroll.cursor = comments_payload["page_info"]["end_cursor"]
                    scroll.has_next = comments_payload["page_info"]["has_next_page"]
                if scroll.has_next:
                    with catch_rate_limit(scroll):
                        for _ in range(COMMENT_PAGING):
                            next_comments: JSON = api.CommentsListComponentsPaginationQuery(
                                post.feedback_id,
                                scroll.cursor,
                            )[0]["data"]["node"]["comment_rendering_instance_for_feed_location"]["comments"]

                            post.comments.extend(parse_comment(i["node"]) for i in next_comments["edges"])
                            scroll.cursor = next_comments["page_info"]["end_cursor"]
                            scroll.has_next = next_comments["page_info"]["has_next_page"]
                            if not scroll.has_next:
                                break

    return _cache_page(page_key, (post, scroll), scroll)


def full_size_photo_url(api: Api, post_id: str) -> str | None:
    """Full-size URL of a post's single photo, from its "Download" menu item.

    It only upgrades the resolution, so any failure is logged and the post keeps its regular photo.
    """
    try:
        items: list[JSON] = api.CometFeedStoryMenuQuery(post_id)[0]["data"]["feed_unit"]["nfx_action_menu_items"]
        return next(
            i["story"]["attachments"][0]["media"]["download_link"][:-5] for i in items if i["__typename"] == "PhotoDownloadMenuItem"
        )
    except (ResponseError, NetworkError, ParsingError, KeyError, IndexError, TypeError, StopIteration) as e:
        log.warning("facebook: full-size photo lookup failed for %s: %s", post_id, e)
        return None


def _resolve_post_id(api: Api, token: str) -> str | None:
    if (hit := ROUTES.get(("post_id", token))) is not MISS:
        return cast("str", hit)

    route, route_type = _route(api, token)
    post_id: str | None = None
    if route and route_type:
        match route_type:
            case "post" | "group_post":
                post_id = route["rootView"]["props"]["storyID"]
            case "videos":
                props: JSON = route["rootView"]["props"]
                page_id: str = props["pageID"]
                video_id: str = props["v"]
                if page_id and video_id:
                    post_id = base64s(f"S:_I{page_id}:{video_id}:{video_id}")
            case "reel":
                reel: JSON | None = api.FBReelsRootWithEntrypointQuery(token)[0]["data"]["video"]
                if reel:
                    post_id = reel["creation_story"]["id"]
            case "photos":
                photo: JSON | None = api.CometPhotoRootContentQuery(token)[0]["data"]["currMedia"]
                if photo:
                    # Photos uploaded straight to a profile or album have no creation story: their owner is the same actor.
                    story: JSON | None = photo.get("creation_story")
                    user_id: str = orjson.loads(story["tracking"])["actrs"] if story else photo["owner"]["id"]
                    post_id = base64s(f"S:_I{user_id}:VK:{photo['id']}")
            case _:
                pass
    if post_id is not None:
        ROUTES.set(("post_id", token), post_id, ROUTE_TTL)
    return post_id


@_guard
def get_group(token: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Feed, Scroll]:
    page_key: tuple[str | None, ...] = ("group", token, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Feed, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    feed: Feed
    with Api(proxy=proxy) as api:
        route, route_type = _route(api, f"groups/{token}")
        if not route or route_type != "group":
            raise NotFoundError(f"Group {token} not found")
        group_id: str = route["rootView"]["props"]["groupID"]

        cached_header: object = HEADERS.get(("group", group_id))
        if cached_header is not MISS:
            feed = replace(cast("Feed", cached_header), posts=[])
        else:
            feed = _fetch_group_header(api, group_id)
            HEADERS.set(("group", group_id), replace(feed, posts=[]), HEADER_TTL)

        if not scroll.cursor:
            posts_feed: list[JSON] = api.CometGroupDiscussionRootSuccessQuery(group_id)
            if post := posts_feed[1]["data"].get("node"):
                feed.posts.append(parse_post(post))
            for i in posts_feed[1:]:
                if page_info := i["data"].get("page_info"):
                    scroll.cursor = page_info["end_cursor"]
                    scroll.has_next = page_info["has_next_page"]
                    break
            else:
                raise ParsingError("Couldn't find page_info")
        if scroll.has_next:
            with catch_rate_limit(scroll):
                for _ in range(GROUP_PAGING):
                    response: list[JSON] = api.GroupsCometFeedRegularStoriesPaginationQuery(group_id, scroll.cursor)
                    rest: list[JSON] = [
                        i for i in response[1:] if "GroupsCometFeedRegularStories_group_group_feed" in i.get("label", "")
                    ]

                    if edges := response[0]["data"]["node"]["group_feed"]["edges"]:
                        feed.posts.append(parse_post(edges[0]["node"]))
                    feed.posts.extend(parse_post(i["data"]["node"]) for i in rest[:-1])
                    scroll.cursor = rest[-1]["data"]["page_info"]["end_cursor"]
                    scroll.has_next = rest[-1]["data"]["page_info"]["has_next_page"]
                    if not scroll.has_next:
                        break

    return _cache_page(page_key, (feed, scroll), scroll)


def _fetch_group_header(api: Api, group_id: str) -> Feed:
    header: JSON = api.CometGroupRootQuery(group_id)[0]["data"]["group"]["profile_header_renderer"]["group"]
    side_panel: JSON = api.GroupsCometDiscussionLayoutRootQuery(
        group_id,
    )[-1]["data"]["comet_discussion_tab_cards"][0]["group"]

    feed = Feed(
        id=group_id,
        token=urlbasename(header["url"]),
        name=header["name"],
        description=side_panel["description_with_entities"]["text"],
        members=header["group_member_profiles"]["formatted_count_text"].split(" ", 1)[0],
        is_group=True,
        is_private=side_panel["privacy_info"]["label"]["text"] == "Private",
    )

    if cover_photo := (cast("JSON | None", header["cover_renderer"]["cover_photo_content"]) or {}).get("photo"):
        feed.cover_photo = Photo(
            url=cover_photo["image"]["uri"],
            id=cover_photo["id"],
            alt_text=f"{header['name']}'s cover picture",
        )

    if locations := side_panel["group_locations"]:
        feed.info = [
            {
                "type": "location",
                "text": ", ".join(i["name"] for i in locations),
                "url": None,
            }
        ]

    return feed


@_guard
def get_album(token: str, cursor: str | None = None, *, proxy: str | None = None) -> tuple[Album, Scroll]:
    page_key: tuple[str | None, ...] = ("album", token, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[Album, Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    album: Album
    with Api(proxy=proxy) as api:
        cached_header: object = HEADERS.get(("album", token)) if cursor else MISS
        if cached_header is not MISS:
            album = replace(cast("Album", cached_header), items=[])
        else:
            album_data: JSON | None = api.CometPhotoAlbumQuery(token)[0]["data"]["album"]
            if not album_data:
                raise NotFoundError("Album not found")

            album = Album(
                id=album_data["id"],
                title=album_data["title"]["text"],
            )
            if description := album_data.get("message"):
                album.description = description["text"]
            HEADERS.set(("album", token), replace(album, items=[]), HEADER_TTL)

            if not scroll.cursor:
                album.items.extend(parse_album_item(i["node"]) for i in album_data["media"]["edges"])
                scroll.cursor = album_data["media"]["page_info"]["end_cursor"]
                scroll.has_next = album_data["media"]["page_info"]["has_next_page"]
        if scroll.has_next:
            with catch_rate_limit(scroll):
                for _ in range(ALBUM_PAGING):
                    next_items: JSON = api.CometAlbumPhotoCollagePaginationQuery(
                        album.id,
                        scroll.cursor,
                    )[0]["data"]["node"]["media"]

                    album.items.extend(parse_album_item(i["node"]) for i in next_items["edges"])
                    scroll.cursor = next_items["page_info"]["end_cursor"]
                    scroll.has_next = next_items["page_info"]["has_next_page"]
                    if not scroll.has_next:
                        break

    return _cache_page(page_key, (album, scroll), scroll)


@_guard
def get_event(event_id: str, *, proxy: str | None = None) -> EventPage:
    page_key: tuple[str | None, ...] = ("event", event_id)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("EventPage", hit)

    header_query: list[JSON]
    about_query: list[JSON]
    with Api(proxy=proxy) as api:
        try:
            header_query = api.EventCometPermalinkHeaderQuery(event_id)
            about_query = api.PublicEventCometAboutRootQuery(event_id)
        except ResponseError as e:
            if e.code != API_ERROR_CODES["rate_limit"]:
                raise
            html_query = api.query_from_html(f"events/{event_id}")
            if "EventCometPermalinkHeaderQuery" not in html_query or "PublicEventCometAboutRootQuery" not in html_query:
                raise
            header_query = html_query["EventCometPermalinkHeaderQuery"]
            about_query = html_query["PublicEventCometAboutRootQuery"]

    header: JSON | None = header_query[0]["data"].get("event")
    about: JSON | None = about_query[0]["data"].get("event")
    if not header or not about:
        raise NotFoundError("Event not found (it may be private or deleted)")

    event: EventPage = parse_event(header, about)
    PAGES.set(page_key, event, PAGE_TTL)
    return event


@_guard
def get_search(
    query: str,
    category: str,
    cursor: str | None = None,
    *,
    proxy: str | None = None,
) -> tuple[list[SearchItem], Scroll]:
    page_key: tuple[str | None, ...] = ("search", query, category, cursor)
    if (hit := PAGES.get(page_key)) is not MISS:
        return cast("tuple[list[SearchItem], Scroll]", hit)

    scroll: Scroll = Scroll(cursor)
    results: list[SearchItem] = []
    with Api(proxy=proxy) as api:
        search_type, filters = SEARCH_TYPES[category]
        with catch_rate_limit(scroll):
            for _ in range(SEARCH_PAGING):
                results_payload: JSON = api.SearchCometResultsPaginatedResultsQuery(
                    query,
                    search_type,
                    scroll.cursor,
                    filters,
                )[0]["data"]["serpResponse"]["results"]

                for i in results_payload["edges"]:
                    item: User | Post | None = parse_search(i)
                    if item is not None:
                        results.append(item)

                scroll.cursor = results_payload["page_info"]["end_cursor"]
                scroll.has_next = results_payload["page_info"]["has_next_page"]
                if not scroll.has_next:
                    break

    return _cache_page(page_key, (results, scroll), scroll)


@_guard
def get_shared_url(route: str, proxy: str | None = None) -> str | None:
    if (hit := ROUTES.get(("share", route))) is not MISS:
        return cast("str", hit)

    log.info("facebook: share /%s", route)
    try:
        with http_client(proxy=proxy) as client:
            r: httpx.Response = client.get(f"https://www.facebook.com/share/{route}", follow_redirects=False)
    except httpx.TimeoutException as e:
        raise NetworkError("Facebook took too long to respond.", timeout=True) from e
    except httpx.HTTPError as e:
        raise NetworkError("Couldn't reach Facebook.") from e

    location: str | None = r.headers.get("location")
    if r.status_code != 302 or location is None:
        return None

    url: ParseResult = urlparse(location)
    url = url._replace(
        netloc="",
        scheme="",
        query=urlencode([(k, v) for k, v in parse_qs(url.query).items() if k not in EXCLUDED_SHARE_HEADERS], doseq=True),
    )

    shared_url: str = url.geturl()
    ROUTES.set(("share", route), shared_url, ROUTE_TTL)
    return shared_url
