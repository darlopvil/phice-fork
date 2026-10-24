from flask import Blueprint, render_template
from flask.typing import ResponseReturnValue

from ..flask_utils import get_config
from ..lib.exceptions import NotFoundError
from ..lib.extractor import get_event

bp: Blueprint = Blueprint("events", __name__)


@bp.route("/events/<string:token>")
def events(token: str) -> ResponseReturnValue:
    if not token.isdigit():
        raise NotFoundError("Event not found")

    event = get_event(token, proxy=get_config().proxy)

    return render_template("event.html.jinja", event=event, title=event.name)
