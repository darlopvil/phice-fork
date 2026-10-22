import httpx


def http_client(
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    follow_redirects: bool = False,
    base_url: str = "",
    facebook_cookies: bool = True,
) -> httpx.Client:
    client_headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.facebook.com",
        "Alt-Used": "www.facebook.com",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "TE": "trailers",
    }
    # Only display preferences, shared by countless browsers. No identity cookies (datr, sb):
    # logged-out queries work without them, and fixed values would make every instance look alike.
    client_cookies: dict[str, str] = {"dpr": "2", "wd": "1920x1080"} if facebook_cookies else {}
    client_proxy: str | None = proxy or None

    return httpx.Client(
        headers=(client_headers | (headers or {})),
        cookies=client_cookies,
        proxy=client_proxy,
        timeout=15,
        http2=True,
        follow_redirects=follow_redirects,
        base_url=base_url,
    )
