import base64
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from agent.config import (
    e3_file_proxy_max_bytes,
    e3_file_proxy_ttl_seconds,
    file_proxy_secret,
    public_base_url,
)
from .client import get_runtime_root, make_user_key


_USED_NONCES = {}
_NONCE_LOCK = threading.Lock()
_ALLOWED_HOSTS = {"e3p.nycu.edu.tw"}


class FileProxyError(Exception):
    status_code = 400
    title = "下載失敗"
    message = "檔案代理請求失敗。"

    def __init__(self, message=None):
        super().__init__(message or self.message)
        self.message = message or self.message


class FileProxyTokenExpired(FileProxyError):
    title = "連結已過期"
    message = "這個下載連結已過期，請回到 LINE 重新點一次檔案。"


class FileProxyTokenUsed(FileProxyError):
    title = "連結已使用"
    message = "這個下載連結已經使用過，請回到 LINE 重新開啟檔案。"


class FileProxySessionExpired(FileProxyError):
    status_code = 401
    title = "E3 Session 已過期"
    message = "目前的 E3 登入已失效，請先回到 LINE 輸入 e3 relogin。"


class FileProxyTooLarge(FileProxyError):
    status_code = 413
    title = "檔案過大"
    message = "這個檔案太大，暫時不透過代理下載，請改由 E3 網頁開啟。"


class FileProxyInvalidToken(FileProxyError):
    title = "連結無效"
    message = "這個下載連結無效，請回到 LINE 重新操作。"


def _now():
    return int(time.time())


def _cleanup_nonces(now=None):
    now = now or _now()
    expired = []
    with _NONCE_LOCK:
        for nonce, exp in _USED_NONCES.items():
            if exp <= now:
                expired.append(nonce)
        for nonce in expired:
            _USED_NONCES.pop(nonce, None)


def _sign_payload(payload_json):
    secret = file_proxy_secret().encode("utf-8")
    return hmac.new(secret, payload_json.encode("utf-8"), hashlib.sha256).hexdigest()


def _urlsafe_b64encode(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _urlsafe_b64decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8")


def _is_allowed_e3_url(url):
    parts = urlsplit(str(url or "").strip())
    return parts.scheme in {"http", "https"} and parts.netloc in _ALLOWED_HOSTS and parts.path.startswith("/pluginfile.php/")


def build_proxy_url(line_user_id, source_url, filename=""):
    base_url = public_base_url()
    if not base_url or not _is_allowed_e3_url(source_url):
        return source_url

    now = _now()
    payload = {
        "uid": line_user_id,
        "url": source_url,
        "exp": now + e3_file_proxy_ttl_seconds(),
        "nonce": secrets.token_urlsafe(12),
        "name": filename,
    }
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    token = _urlsafe_b64encode(payload_json)
    signature = _sign_payload(payload_json)
    return f"{base_url}/e3/file/{token}.{signature}"


def _load_proxy_token(token):
    if "." not in token:
        raise FileProxyInvalidToken()
    payload_part, signature = token.rsplit(".", 1)
    payload_json = _urlsafe_b64decode(payload_part)
    expected = _sign_payload(payload_json)
    if not hmac.compare_digest(signature, expected):
        raise FileProxyInvalidToken()
    payload = json.loads(payload_json)
    if int(payload.get("exp") or 0) < _now():
        raise FileProxyTokenExpired()
    if not _is_allowed_e3_url(payload.get("url")):
        raise FileProxyInvalidToken("下載連結中的檔案位置不被允許。")
    return payload


def _mark_nonce_used(nonce, exp):
    _cleanup_nonces()
    with _NONCE_LOCK:
        if nonce in _USED_NONCES:
            return False
        _USED_NONCES[nonce] = exp
        return True


def _runtime_cookie_file(line_user_id):
    user_key = make_user_key(line_user_id)
    return get_runtime_root() / user_key / "cookies.json"


def _load_cookie_dict(line_user_id):
    cookie_file = _runtime_cookie_file(line_user_id)
    if not cookie_file.exists():
        raise FileProxySessionExpired("找不到 E3 session，請先回到 LINE 執行 e3 relogin。")
    with cookie_file.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise FileProxySessionExpired("E3 session 格式異常，請先回到 LINE 執行 e3 relogin。")
    return {str(k): str(v) for k, v in data.items() if v}


def _filename_from_response(source_url, response, fallback_name="download"):
    disposition = response.headers.get("Content-Disposition") or ""
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    path_name = Path(urlsplit(source_url).path).name
    return path_name or fallback_name


def prepare_proxy_download(token):
    payload = _load_proxy_token(token)
    nonce = payload["nonce"]
    exp = int(payload["exp"])
    if not _mark_nonce_used(nonce, exp):
        raise FileProxyTokenUsed()

    cookie_dict = _load_cookie_dict(payload["uid"])
    session = requests.Session()
    session.cookies.update(cookie_dict)
    response = session.get(payload["url"], stream=True, timeout=30, allow_redirects=True)
    response.raise_for_status()

    final_url = response.url
    if not _is_allowed_e3_url(final_url):
        response.close()
        raise FileProxySessionExpired("E3 導向了非預期頁面，登入可能已過期，請先回到 LINE 執行 e3 relogin。")

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            size = int(content_length)
        except ValueError:
            size = None
        else:
            if size > e3_file_proxy_max_bytes():
                response.close()
                raise FileProxyTooLarge()

    content_type = response.headers.get("Content-Type") or "application/octet-stream"
    filename = _filename_from_response(payload["url"], response, payload.get("name") or "download")
    if not mimetypes.guess_extension(content_type.split(";")[0].strip()) and filename == "download":
        guessed = Path(urlsplit(payload["url"]).path).name
        if guessed:
            filename = guessed

    return {
        "response": response,
        "filename": filename,
        "content_type": content_type,
    }


def prepare_user_download(user_id, source_url, filename="download", max_bytes=None):
    if not _is_allowed_e3_url(source_url):
        raise FileProxyInvalidToken("下載連結中的檔案位置不被允許。")

    cookie_dict = _load_cookie_dict(user_id)
    session = requests.Session()
    session.cookies.update(cookie_dict)
    response = session.get(source_url, stream=True, timeout=30, allow_redirects=True)
    response.raise_for_status()

    final_url = response.url
    if not _is_allowed_e3_url(final_url):
        response.close()
        raise FileProxySessionExpired("E3 導向了非預期頁面，登入可能已過期，請先重新登入。")

    limit = int(max_bytes or e3_file_proxy_max_bytes())
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            size = int(content_length)
        except (TypeError, ValueError):
            size = None
        else:
            if size > limit:
                response.close()
                raise FileProxyTooLarge()

    content_type = response.headers.get("Content-Type") or "application/octet-stream"
    resolved_name = _filename_from_response(source_url, response, filename or "download")
    return {
        "response": response,
        "filename": resolved_name,
        "content_type": content_type,
    }
