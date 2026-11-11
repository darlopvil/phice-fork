from collections.abc import Mapping

from flask import Blueprint, abort, render_template, request
from flask.typing import ResponseReturnValue

from ..flask_utils import get_config, get_user_settings
from ..lib.extractor import get_post

bp: Blueprint = Blueprint("posts", __name__)


def post_token(endpoint: str | None, token: str | None, args: Mapping[str, str]) -> str | None:
    """The post id or token a post route points to: in the path, or in the query for Facebook's .php routes."""
    match endpoint:
        case "posts.photo":
            return args.get("fbid")
        case "posts.permalink" | "posts.story":
            return args.get("story_fbid")
        case "posts.watch":
            return args.get("v")
        case _:
            return token


@bp.route("/<string:author>/posts/<string:_>/<string:token>", endpoint="posts")
@bp.route("/<string:author>/posts/<string:token>", endpoint="posts")
@bp.route("/<string:author>/videos/<string:_>/<string:token>", endpoint="videos")
@bp.route("/<string:author>/videos/<string:token>", endpoint="videos")
@bp.route("/reel/<string:token>", endpoint="reel")
@bp.route("/groups/<string:author>/permalink/<string:token>", endpoint="groups_posts")
@bp.route("/groups/<string:author>/posts/<string:token>", endpoint="groups_posts")
@bp.route("/photo.php", endpoint="photo")
@bp.route("/photo", endpoint="photo")
@bp.route("/permalink.php", endpoint="permalink")
@bp.route("/story.php", endpoint="story")
@bp.route("/watch", endpoint="watch")
def posts(author: str | None = None, token: str | None = None, _: str | None = None) -> ResponseReturnValue:  # pyright: ignore[reportUnusedParameter] # noqa: ARG001
    post_id: str | None = post_token(request.endpoint, token, request.args)
    if not post_id:
        abort(400)

    post, scroll = get_post(
        post_id,
        request.args.get("cursor"),
        request.args.get("comment_id"),
        request.args.get("sort", get_user_settings().comments_sort),
        proxy=get_config().proxy,
    )

    return render_template("posts.html.jinja", post=post, scroll=scroll, title=post.text[:58])
