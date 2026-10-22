from flask import Blueprint, render_template
from flask.typing import ResponseReturnValue

bp: Blueprint = Blueprint("home", __name__)


@bp.route("/")
def home() -> ResponseReturnValue:
    return render_template("home.html.jinja")


@bp.route("/healthz")
def healthz() -> ResponseReturnValue:
    # Liveness only: never contacts Facebook, so monitors don't add load on it.
    return "ok\n", 200, {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"}
