import uuid
from typing import Any

from flask import request, url_for

from .flask_utils import get_config, get_user_settings
from .lib.cache import MISS, TTLCache
from .lib.datatypes import Post, PostType, Reactions
from .settings import THEMES

REACTION_ICONS: dict[str, str] = {
    "like": "thumbs-up",
    "love": "heart",
    "care": "hand-holding-heart",
    "haha": "face-grin-squint",
    "wow": "face-surprise",
    "sad": "face-sad-tear",
    "angry": "face-angry",
}

# Facebook cursors only go forward, so the cursor that led to each "Next" page is
# remembered server-side to offer a "Previous" link. Nothing is added to the URLs.
PREVIOUS_CURSORS: TTLCache = TTLCache(maxsize=4096)
PREVIOUS_CURSOR_TTL: int = 24 * 60 * 60


def types(obj: object) -> str:
    return type(obj).__name__


def post_url(p: Post, *, external: bool = False) -> str:
    author: str = p.author.username or p.author.id

    if p.from_group:
        return url_for("posts.groups_posts", author=p.from_group.username, token=p.post_id, _external=external)
    if p.post_type == PostType.VIDEO:
        return url_for("posts.videos", author=author, token=p.post_id, _external=external)
    if p.post_type == PostType.REEL:
        return url_for("posts.reel", token=p.post_id, _external=external)
    return url_for("posts.posts", author=author, token=p.post_id, _external=external)


def juuid() -> str:
    return uuid.uuid1().hex


RSS_ENDPOINTS: tuple[str, ...] = ("profile.profile", "profile.profile_php", "groups.groups")


def rss_url() -> str | None:
    """Absolute URL of the current profile/group feed, or None if the page has none."""
    if not get_config().enable_rss or request.endpoint not in RSS_ENDPOINTS:
        return None
    args: dict[str, str | None] = dict(request.view_args or {})
    if request.endpoint == "profile.profile_php":
        args["id"] = request.args.get("id")
    return url_for(request.endpoint, **args, rss=1, _external=True)


def self_url(**changes: str | None) -> str:
    """URL of the current page with some query parameters changed; None removes one."""
    values: dict[str, Any] = {**(request.view_args or {}), **request.args.to_dict()}
    for k, v in changes.items():
        if v is None:
            values.pop(k, None)
        else:
            values[k] = v
    return url_for(request.endpoint or "home.home", **values)


def paging(next_cursor: str | None, *, has_next: bool | None) -> dict[str, str | None]:
    """Start/Previous/Next links for the current paginated page."""
    current: str | None = request.args.get("cursor") or None
    base: tuple[str | None, ...] = (
        request.endpoint,
        *sorted(f"{k}={v}" for k, v in {**(request.view_args or {}), **request.args.to_dict()}.items() if k != "cursor"),
    )
    links: dict[str, str | None] = {"start": None, "previous": None, "next": None}

    if has_next and next_cursor:
        PREVIOUS_CURSORS.set((*base, next_cursor), current, PREVIOUS_CURSOR_TTL)
        links["next"] = self_url(cursor=next_cursor)
    if current:
        previous: object = PREVIOUS_CURSORS.get((*base, current))
        if previous is MISS:
            links["start"] = self_url(cursor=None)
        elif previous is None:
            links["previous"] = self_url(cursor=None)
        else:
            links["start"] = self_url(cursor=None)
            links["previous"] = self_url(cursor=str(previous))
    return links


def top_reactions(reactions: Reactions, limit: int = 3) -> list[str]:
    """Icons of the most used reactions, most used first."""
    counts: list[tuple[int, str]] = [(getattr(reactions, k), icon) for k, icon in REACTION_ICONS.items()]
    return [icon for count, icon in sorted(counts, key=lambda i: -i[0]) if count > 0][:limit]


GLOBALS: dict[str, Any] = {
    "type": types,
    "get_config": get_config,
    "get_user_settings": get_user_settings,
    "THEMES": THEMES,
    "post_url": post_url,
    "uuid": juuid,
    "rss_url": rss_url,
    "self_url": self_url,
    "paging": paging,
    "top_reactions": top_reactions,
}
