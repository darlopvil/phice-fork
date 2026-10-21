import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .jinja_filters import FILTERS
from .jinja_globals import GLOBALS
from .settings import PhiceConfig, UserSettings


def create_app(config_file: Path | None) -> Flask:
    app: Flask = Flask(__name__)

    custom_config: dict[str, Any] = {}
    if config_file and config_file.is_file():
        with config_file.open("r", encoding="utf-8") as f:
            custom_config = tomllib.loads(f.read())

    app.config["phice"] = PhiceConfig(**custom_config.get("phice", {}))
    app.config["user_settings"] = UserSettings(**custom_config.get("default_user_settings", {}))
    if (trusted_proxies := app.config["phice"].trusted_proxies) > 0:
        # Only the scheme and client address are taken from the proxy: X-Forwarded-Host is
        # often passed through from the client unchanged, so the Host header is used as-is.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies, x_proto=trusted_proxies)
    app.url_map.strict_slashes = False
    app.jinja_options["autoescape"] = True
    app.jinja_env.policies["urlize.rel"] = "noreferrer nofollow"
    app.jinja_env.filters.update(FILTERS)
    app.jinja_env.globals.update(GLOBALS)

    for route in ("albums", "cdn", "error", "groups", "home", "posts", "profile", "search", "settings", "share"):
        app.register_blueprint(import_module(f".routes.{route}", __name__).bp)

    return app
