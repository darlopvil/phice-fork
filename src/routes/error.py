import logging
from pathlib import Path
from traceback import format_exception

from flask import Blueprint, current_app, render_template
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException, InternalServerError

from ..flask_utils import get_config
from ..lib.api import API_ERROR_CODES
from ..lib.exceptions import NetworkError, NotFoundError, ParsingError, ResponseError

bp: Blueprint = Blueprint("error_handlers", __name__)

log: logging.Logger = logging.getLogger("phice")


@bp.app_errorhandler(NotFoundError)
@bp.app_errorhandler(ResponseError)
@bp.app_errorhandler(ParsingError)
@bp.app_errorhandler(NetworkError)
@bp.app_errorhandler(HTTPException)
def error_handler(e: HTTPException | NotFoundError | ResponseError | ParsingError | NetworkError) -> ResponseReturnValue:
    status_code: int = 500
    message: str | None = None
    tb: str | None = None

    match e:
        case NotFoundError():
            status_code = 404
            message = ", ".join(e.args)
        case ResponseError() if e.code == API_ERROR_CODES["rate_limit"]:
            status_code = 429
            message = "Facebook is rate-limiting this instance. Try again later."
            log.warning("facebook: rate limited (%s)", e.message)
        case ResponseError():
            status_code = 502
            message = f"Facebook returned an error: {e.message}"
            log.warning("facebook: %s", e.message)
        case ParsingError():
            status_code = 502
            message = "Facebook returned an unexpected response. It may have changed something on its side."
            log.warning("facebook: %s", e)
        case NetworkError():
            status_code = 504 if e.timeout else 502
            message = e.message
            log.warning("facebook: %s (%s)", e.message, e.__cause__)
        case HTTPException():
            if e.code:
                status_code = e.code
            message = e.description
            # Flask has already logged the original exception with its traceback.
            if isinstance(e, InternalServerError) and (og := e.original_exception) is not None and get_config().show_tracebacks:
                tb = "".join(format_exception(og)).replace(f"{Path(current_app.root_path).parent}/", "")

    return render_template(
        "error.html.jinja",
        title=f"Error {status_code}",
        status_code=status_code,
        traceback=tb,
        message=message,
    ), status_code
