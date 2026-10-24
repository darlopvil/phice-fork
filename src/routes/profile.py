from flask import Blueprint, abort, render_template, request
from flask.typing import ResponseReturnValue
from werkzeug.utils import secure_filename

from ..flask_utils import get_config
from ..lib.extractor import get_profile, get_profile_photos

bp: Blueprint = Blueprint("profile", __name__)


@bp.route("/people/<string:_>/<string:username>")
@bp.route("/profile.php", endpoint="profile_php")
@bp.route("/p/<string:username>")
@bp.route("/<string:username>/timeline")
@bp.route("/<string:username>")
def profile(username: str = "", _: str | None = None) -> ResponseReturnValue:
    token: str | None = username
    if request.endpoint == "profile.profile_php":
        token = request.args.get("id")

    if not token:
        abort(400)
    if "rss" in request.args and not get_config().enable_rss:
        abort(403, "RSS feeds are disabled in this instance")

    feed, scroll = get_profile(token, request.args.get("cursor"), proxy=get_config().proxy)

    if "rss" in request.args:
        return render_template("timeline.rss.jinja", feed=feed), {
            "content-type": "application/rss+xml; charset=utf-8",
            "content-disposition": f'inline; filename="{secure_filename(feed.token) or "feed"}.rss"',
        }
    return render_template("timeline.html.jinja", feed=feed, scroll=scroll, title=feed.name)


@bp.route("/<string:username>/photos")
def photos(username: str) -> ResponseReturnValue:
    feed, scroll = get_profile_photos(username, request.args.get("cursor"), proxy=get_config().proxy)

    return render_template("profile_photos.html.jinja", feed=feed, scroll=scroll, title=f"{feed.name} · Photos")
