import functools
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from typing import cast
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse

import httpx
import orjson

from .api import API_ERROR_CODES, Api
from .cache import MISS, TTLCache
from .datatypes import JSON, Album, Feed, Photo, Post, Scroll, SearchItem, User
from .exceptions import NetworkError, NotFoundError, ParsingError, ResponseError
from .parsers import parse_album_item, parse_comment, parse_post, parse_search
from .utils import base64s, catch_rate_limit, urlbasename
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
PROFILE_PAGING: int = 3
COMMENT_PAGING: int = 2
GROUP_PAGING: int = 4
ALBUM_PAGING: int = 3
SEARCH_PAGING: int = 3
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
        route, route_type = _route(api, username)
        if not route or route_type != "profile":
            raise NotFoundError(f"Profile {username} not found")
        user_id: str = route["rootView"]["props"]["userID"]

        posts_feed: list[JSON] | None = None
        cached_header: object = HEADERS.get(("profile", user_id))
        if cached_header is not MISS and not scroll.cursor:
            # Header known: page 1 only needs the first feed query, not the header ones.
            try:
                posts_feed = api.ProfileCometTimelineFeedQuery(user_id)
            except ResponseError as e:
                if e.code != API_ERROR_CODES["rate_limit"]:
                    raise
                cached_header = MISS  # fall back to the full fetch, which can scrape the HTML
        if cached_header is not MISS:
            feed = replace(cast("Feed", cached_header), posts=[])
        else:
            feed, posts_feed = _fetch_profile_header(api, user_id)
            HEADERS.set(("profile", user_id), replace(feed, posts=[]), HEADER_TTL)

        if not scroll.cursor and posts_feed is not None:
            if i := posts_feed[0]["data"]["user"]["timeline_list_feed_units"]["edges"]:
                feed.posts.append(parse_post(i[0]["node"]))
            for i in posts_feed[1:]:
                if page_info := i["data"].get("page_info"):
                    scroll.cursor = page_info["end_cursor"]
                    scroll.has_next = page_info["has_next_page"]
                    break
            else:
                raise ParsingError("Couldn't find page_info")
        if scroll.has_next:
            with catch_rate_limit(scroll):
                for _ in range(PROFILE_PAGING):
                    response: list[JSON] = api.ProfileCometTimelineFeedRefetchQuery(user_id, scroll.cursor)
                    rest: list[JSON] = [i for i in response[1:] if "ProfileCometTimelineFeed_user" in i.get("label", "")]

                    if edges := response[0]["data"]["node"]["timeline_list_feed_units"]["edges"]:
                        feed.posts.append(parse_post(edges[0]["node"]))
                    feed.posts.extend(parse_post(i["data"]["node"]) for i in rest[:-1])
                    scroll.cursor = rest[-1]["data"]["page_info"]["end_cursor"]
                    scroll.has_next = rest[-1]["data"]["page_info"]["has_next_page"]
                    if not scroll.has_next:
                        break

    return _cache_page(page_key, (feed, scroll), scroll)


def _fetch_profile_header(api: Api, user_id: str) -> tuple[Feed, list[JSON]]:
    """Fetch and parse everything above the posts; also return the first feed query for page 1."""
    header_query: list[JSON]
    side_query: list[JSON]
    posts_query: list[JSON]
    try:
        header_query = api.ProfileCometHeaderQuery(user_id)
        side_query = api.ProfilePlusCometLoggedOutRootQuery(user_id)
        posts_query = api.ProfileCometTimelineFeedQuery(user_id)
    except ResponseError as e:
        if e.code != API_ERROR_CODES["rate_limit"]:
            raise
        html_query = api.query_from_html(f"profile.php?id={user_id}")
        if not html_query:
            raise

        header_query = html_query["ProfileCometHeaderQuery"]
        side_query = html_query["ProfilePlusCometLoggedOutRootQuery"]
        posts_query = html_query["ProfileCometTimelineFeedQuery"]

    header: JSON = header_query[0]["data"]["user"]["profile_header_renderer"]["user"]
    side: JSON = side_query[-1]["data"]["profile_tile_sections"]["edges"][0]["node"]
    posts_feed: list[JSON] = posts_query

    feed = Feed(
        id=user_id,
        token=user_id if header["url"].startswith("https://www.facebook.com/people/") else urlbasename(header["url"]),
        name=header["name"],
        verified=header["show_verified_badge_on_profile"],
    )

    if private := header["wem_private_sharing_bundle"]["private_sharing_control_model_for_user"]:
        feed.is_private = private["private_sharing_enabled"]

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

    if (i := posts_feed[0]["data"]["user"]["delegate_page"]) and (description := i["best_description"]):
        feed.description = description["text"]

    if side["profile_tile_section_type"] == "INTRO":
        for i in side["profile_tile_views"]["nodes"][1]["view_style_renderer"]["view"]["profile_tile_items"]["nodes"]:
            item: dict[str, str | None] = {"text": None, "url": None, "type": None}
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
                        if url and url.startswith("https://l.facebook.com/l.php"):
                            item["url"] = parse_qs(urlparse(url).query)["u"][0]
                        else:
                            item["url"] = url
            item["type"] = i["node"]["timeline_context_item"]["timeline_context_list_item_type"][11:].lower()

            feed.info.append(item)

    return feed, posts_feed


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
                    user_id: str = orjson.loads(photo["creation_story"]["tracking"])["actrs"]
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
