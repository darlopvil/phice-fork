from dataclasses import fields
from urllib.parse import SplitResult, urlsplit

from flask import Blueprint, Response, make_response, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from ..settings import UserSettings

bp: Blueprint = Blueprint("settings", __name__)

COOKIE_MAX_AGE: int = 34560000  # 400 days
OPTIONS: list[str] = [i.name for i in fields(UserSettings)]


def safe_referrer(value: str | None) -> str:
    """Return a path on this instance to go back to, or the settings page."""
    fallback: str = url_for("settings.settings")
    if not value or any(c.isspace() or ord(c) < 0x20 for c in value) or "\\" in value:
        return fallback

    url: SplitResult = urlsplit(value)
    if (url.scheme or url.netloc) and url.netloc != request.host:
        return fallback
    if not url.path.startswith("/") or url.path.startswith("//"):
        return fallback

    return f"{url.path}?{url.query}" if url.query else url.path


@bp.route("/settings", methods=["GET", "POST"])
def settings() -> ResponseReturnValue:
    if request.method == "POST":
        response: Response = make_response(redirect(safe_referrer(request.form.get("referrer"))))
        if request.form.get("reset"):
            for i in OPTIONS:
                response.delete_cookie(i)
        else:
            for i in OPTIONS:
                response.set_cookie(i, request.form.get(i, ""), max_age=COOKIE_MAX_AGE)
        return response

    return render_template("settings.html.jinja", referrer=safe_referrer(request.headers.get("Referer")))
