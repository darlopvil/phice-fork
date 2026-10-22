import threading
from collections.abc import Generator
from contextlib import suppress

import httpx
from flask import Blueprint, Response, make_response, request
from flask.typing import ResponseReturnValue

from src.flask_utils import get_config

from ..lib.wrappers import http_client

bp: Blueprint = Blueprint("cdn", __name__)

EXCLUDED_RESPONSE_HEADERS: list[str] = [
    "x-fb-connection-quality",
    "alt-svc",
    "x-robots-tag",
    "connection",
    "content-length",
    "access-control-allow-origin",
    "timing-allow-origin",
    "x-crypto-project",
    "x-additional-error-detail",
    "x-fb-vts-requestid",
    "x-fb-ptm-uuid",
]
INCLUDED_REQUEST_HEADERS: list[str] = [
    "range",
    "if-modified-since",
    "if-none-match",
    "if-range",
]

# One pooled client per process: connections to the CDN are reused instead of
# opening a new TLS (and HTTP/2) session for every image.
_clients: dict[str | None, httpx.Client] = {}
_clients_lock: threading.Lock = threading.Lock()


def _client(proxy: str | None) -> httpx.Client:
    with _clients_lock:
        if proxy not in _clients:
            _clients[proxy] = http_client(proxy=proxy)
        return _clients[proxy]


def _cache_control(value: str | None) -> str | None:
    # fbcdn URLs are signed and never change content, so cacheable responses are
    # marked immutable: Firefox otherwise revalidates every image on each reload.
    if value is None:
        return None
    directives: list[str] = [i.strip().lower() for i in value.split(",")]
    if any(i.startswith("max-age=") for i in directives) and not {"no-store", "no-cache", "private"} & set(directives):
        return f"{value}, immutable" if "immutable" not in directives else value
    return value


@bp.route("/cdn/<string:host>/<path:path>")
def cdn(host: str, path: str) -> ResponseReturnValue:
    if not host.endswith(".fbcdn.net"):
        return "", 403

    client_headers: dict[str, str] = {k: v for k, v in request.headers.items() if k.lower() in INCLUDED_REQUEST_HEADERS}
    client: httpx.Client = _client(get_config().proxy)
    try:
        cdn_request: httpx.Request = client.build_request(
            "GET",
            httpx.URL(scheme="https", host=host, path=f"/{path}", query=request.query_string),
            headers=client_headers,
        )
        cdn_response: httpx.Response = client.send(cdn_request, stream=True, follow_redirects=True)
    except httpx.HTTPError:
        return "", 502
    if cdn_response.status_code >= 400:
        cdn_response.close()
        return "", cdn_response.status_code if cdn_response.status_code in (403, 404, 410, 416) else 502

    def stream() -> Generator[bytes]:
        with suppress(httpx.HTTPError):
            yield from cdn_response.iter_raw()

    response_headers: dict[str, str] = {k: v for k, v in cdn_response.headers.items() if k.lower() not in EXCLUDED_RESPONSE_HEADERS}
    if (cache_control := _cache_control(cdn_response.headers.get("cache-control"))) is not None:
        response_headers["cache-control"] = cache_control
    response: Response = make_response(stream(), cdn_response.status_code, response_headers)

    @response.call_on_close
    def close() -> None:  # pyright: ignore[reportUnusedFunction]
        cdn_response.close()

    return response
