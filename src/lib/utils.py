import base64
from collections.abc import Generator
from contextlib import contextmanager
from urllib.parse import urlparse

from .api import API_ERROR_CODES
from .datatypes import Scroll
from .exceptions import ResponseError


def base64s(s: str) -> str:
    return base64.standard_b64encode(s.encode()).decode()


def base64s_decode(s: str) -> str:
    return base64.standard_b64decode(s.encode()).decode()


def urlbasename(url: str) -> str:
    return list(filter(None, urlparse(url).path.split("/")))[-1]


# Networks profiles commonly link to, by domain (subdomains included).
SOCIAL_NETWORKS: dict[str, str] = {
    "bandcamp.com": "Bandcamp",
    "bsky.app": "Bluesky",
    "discord.com": "Discord",
    "discord.gg": "Discord",
    "github.com": "GitHub",
    "instagram.com": "Instagram",
    "kick.com": "Kick",
    "linkedin.com": "LinkedIn",
    "patreon.com": "Patreon",
    "pinterest.com": "Pinterest",
    "reddit.com": "Reddit",
    "snapchat.com": "Snapchat",
    "soundcloud.com": "SoundCloud",
    "spotify.com": "Spotify",
    "t.me": "Telegram",
    "threads.com": "Threads",
    "threads.net": "Threads",
    "tiktok.com": "TikTok",
    "tumblr.com": "Tumblr",
    "twitch.tv": "Twitch",
    "twitter.com": "X",
    "vimeo.com": "Vimeo",
    "vk.com": "VK",
    "wa.me": "WhatsApp",
    "x.com": "X",
    "youtu.be": "YouTube",
    "youtube.com": "YouTube",
}


def network_name(url: str) -> str | None:
    """Name of the network a URL belongs to, or its bare domain when it isn't a known one."""
    host: str = (urlparse(url).hostname or "").removeprefix("www.").removeprefix("m.")
    for domain, name in SOCIAL_NETWORKS.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return host or None


@contextmanager
def catch_rate_limit(obj: Scroll) -> Generator[None]:
    try:
        yield
    except ResponseError as e:
        if e.code != API_ERROR_CODES["rate_limit"]:
            raise
        obj.rate_limited = True
        obj.cursor = None
        obj.has_next = False
