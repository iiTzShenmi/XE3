from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


def build_session(cookies=None, *, referer: str | None = None) -> requests.Session:
    session = requests.Session()
    headers = {"User-Agent": config.USER_AGENT}
    if referer:
        headers["Referer"] = referer
    session.headers.update(headers)
    if cookies and isinstance(cookies, dict):
        session.cookies.update(cookies)

    retry = Retry(
        total=config.REQUEST_RETRIES,
        connect=config.REQUEST_RETRIES,
        read=config.REQUEST_RETRIES,
        status=config.REQUEST_RETRIES,
        backoff_factor=config.REQUEST_BACKOFF_SECONDS,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    original_request = session.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    return session
