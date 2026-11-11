"""Facebook's embed plugins, for iframes: /plugins/post.php and /plugins/video.php take the same href."""

from contextlib import suppress
from typing import Any, cast
from urllib.parse import SplitResult, parse_qsl, urlsplit

from flask import Blueprint, abort, current_app, render_template, request
from flask.typing import ResponseReturnValue
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException

from ..flask_utils import get_config, get_user_settings
from ..lib.extractor import get_post, get_shared_url
from .posts import post_token

bp: Blueprint = Blueprint("embed", __name__)

FACEBOOK_HOSTS: frozenset[str] = frozenset(
    {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com"}
)


def is_facebook_url(href: str) -> bool:
    url: SplitResult = urlsplit(href)
    return url.scheme in {"http", "https"} and (url.hostname or "").lower() in FACEBOOK_HOSTS


def _href_post_id(href: str) -> str:
    """The post an embed's href points to, found with the same routes that serve posts."""
    if not is_facebook_url(href):
        abort(400, "The href must be a facebook.com URL.")
    url: SplitResult = urlsplit(href)
    if url.path.startswith("/share/"):
        # Share links lead to a post, and Phice already resolves them for its /share route.
        shared: str | None = get_shared_url(url.path.removeprefix("/share/"), get_config().proxy)
        if shared is None:
            abort(404, "This share link doesn't lead to a post.")
        url = urlsplit(shared)

    endpoint: str = ""
    view_args: dict[str, Any] = {}
    with suppress(HTTPException):  # no route, or not one Phice serves
        endpoint, view_args = cast("tuple[str, dict[str, Any]]", current_app.url_map.bind("phice").match(url.path))
    post_id: str | None = None
    if endpoint.startswith("posts."):
        post_id = post_token(endpoint, view_args.get("token"), MultiDict(parse_qsl(url.query)))
    if not post_id:
        abort(400, "The href must point to a post, video, reel or photo.")
    return post_id


@bp.route("/plugins/post.php", endpoint="post")
@bp.route("/plugins/video.php", endpoint="video")
def embed() -> ResponseReturnValue:
    post_id: str = _href_post_id(request.args.get("href", ""))
    # The default comment order, so an embed and its full post page share one request to Facebook.
    post, _ = get_post(post_id, sort=get_user_settings().comments_sort, proxy=get_config().proxy)

    # As in Facebook's video plugin, a video comes without its post unless show_text asks for it.
    media_only: bool = (
        request.endpoint == "embed.video"
        and request.args.get("show_text", "").lower() not in {"1", "true"}
        and post.attachment is not None
    )
    return render_template("embed.html.jinja", post=post, media_only=media_only, title=post.text[:58])
