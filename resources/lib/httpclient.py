"""Small JSON HTTP client. API keys stay in the query or headers and out of errors."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


USER_AGENT = "script.ratings.updatarr/1.2 (Kodi)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class HttpStatus(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = int(status)
        self.message = message
        super().__init__(f"HTTP {self.status}: {message}")


class HttpResult:
    def __init__(self, payload, headers: dict) -> None:
        self.payload = payload
        self.headers = headers


class JsonClient:
    def __init__(self, opener=None, sleeper=None, timeout: float = 20) -> None:
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper or time.sleep
        self.timeout = timeout

    def get_json(self, url: str, headers: dict | None = None):
        return self.exchange(url, headers).payload

    def exchange(self, url: str, headers: dict | None = None) -> HttpResult:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if headers:
            request_headers.update(headers)
        last_error: HttpStatus | None = None
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read()
                    response_headers = _headers(response)
            except urllib.error.HTTPError as exc:
                last_error = HttpStatus(exc.code, _body_text(exc))
                if exc.code in RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
                    self.sleeper(_retry_delay(exc, attempt))
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                raise HttpStatus(0, str(getattr(exc, "reason", exc))) from exc
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if not text.strip():
                return HttpResult(None, response_headers)
            return HttpResult(json.loads(text), response_headers)
        if last_error:
            raise last_error
        raise HttpStatus(0, "No response")


def _body_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    return " ".join(text.split())[:180]


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    header = ""
    try:
        header = exc.headers.get("Retry-After") or ""
    except Exception:
        header = ""
    try:
        return min(300.0, max(1.0, float(header)))
    except (TypeError, ValueError):
        return min(60.0, float(2 ** (attempt + 1)))


def _headers(response) -> dict:
    raw = getattr(response, "headers", None)
    if not raw:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in raw.items()}
    except Exception:
        return {}
