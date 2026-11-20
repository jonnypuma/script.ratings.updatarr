"""Small JSON HTTP client. API keys stay in the query or headers and out of errors."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "script.ratings.updatarr/1.3 (Kodi)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class HttpStatus(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        self.status = int(status)
        self.message = message
        self.retry_after = retry_after
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
        self._pool: dict[tuple[str, str, int], http.client.HTTPConnection] = {}

    def close(self) -> None:
        for conn in self._pool.values():
            try:
                conn.close()
            except Exception:
                pass
        self._pool.clear()

    def get_json(self, url: str, headers: dict | None = None):
        return self.exchange(url, headers).payload

    def exchange(self, url: str, headers: dict | None = None) -> HttpResult:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if headers:
            request_headers.update(headers)
        use_pool = self.opener is urllib.request.urlopen
        last_error: HttpStatus | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                if use_pool:
                    raw, response_headers = self._pooled_get(url, request_headers)
                else:
                    request = urllib.request.Request(url, headers=request_headers)
                    with self.opener(request, timeout=self.timeout) as response:
                        raw = response.read()
                        response_headers = _headers(response)
            except urllib.error.HTTPError as exc:
                last_error = HttpStatus(exc.code, _body_text(exc))
                if exc.code in RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
                    self.sleeper(_retry_delay(exc, attempt))
                    continue
                raise last_error from exc
            except HttpStatus as exc:
                last_error = exc
                if exc.status in RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
                    delay = exc.retry_after if exc.retry_after is not None else min(60.0, float(2 ** (attempt + 1)))
                    self.sleeper(delay)
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = HttpStatus(0, str(reason))
                if attempt + 1 < MAX_ATTEMPTS:
                    self.sleeper(min(60.0, float(2 ** (attempt + 1))))
                    continue
                raise last_error from exc
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if not text.strip():
                return HttpResult(None, response_headers)
            return HttpResult(json.loads(text), response_headers)
        if last_error:
            raise last_error
        raise HttpStatus(0, "No response")

    def _pooled_get(self, url: str, headers: dict) -> tuple[bytes, dict]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn = self._pool.get(key)
        if conn is None:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = cls(host, port, timeout=self.timeout)
            self._pool[key] = conn
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            response_headers = _headers(response)
            status = int(response.status)
        except Exception:
            self._drop_conn(key)
            raise
        if status >= 400:
            if response_headers.get("connection", "").lower() == "close":
                self._drop_conn(key)
            msg = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
            snippet = " ".join(msg.split())[:180]
            retry_after = None
            raw_retry = response_headers.get("retry-after")
            if raw_retry:
                try:
                    retry_after = min(300.0, max(1.0, float(raw_retry)))
                except ValueError:
                    retry_after = None
            raise HttpStatus(status, snippet, retry_after=retry_after)
        return raw, response_headers

    def _drop_conn(self, key: tuple[str, str, int]) -> None:
        conn = self._pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


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
