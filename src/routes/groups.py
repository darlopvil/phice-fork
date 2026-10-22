from flask import Blueprint, abort, render_template, request
from flask.typing import ResponseReturnValue
from werkzeug.utils import secure_filename

from ..flask_utils import get_config
from ..lib.extractor import get_group

bp: Blueprint = Blueprint("groups", __name__)


@bp.route("/groups/<string:token>")
def groups(token: str) -> ResponseReturnValue:
    if "rss" in request.args and not get_config().enable_rss:
        abort(403, "RSS feeds are disabled in this instance")

    feed, scroll = get_group(token, request.args.get("cursor"), proxy=get_config().proxy)

    if "rss" in request.args:
        return render_template("timeline.rss.jinja", feed=feed), {
            "content-type": "application/rss+xml; charset=utf-8",
            "content-disposition": f'inline; filename="{secure_filename(feed.token) or "feed"}.rss"',
        }
    return render_template("timeline.html.jinja", feed=feed, scroll=scroll, title=feed.name)
