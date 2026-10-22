import uuid
from typing import Any

from flask import request, url_for

from .flask_utils import get_config, get_user_settings
from .lib.datatypes import Post, PostType
from .settings import THEMES


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


GLOBALS: dict[str, Any] = {
    "type": types,
    "get_config": get_config,
    "get_user_settings": get_user_settings,
    "THEMES": THEMES,
    "post_url": post_url,
    "uuid": juuid,
    "rss_url": rss_url,
}
