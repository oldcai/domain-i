"""Minimal HTTP abstraction over urllib so network access can be faked in tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

USER_AGENT = "domain-i/0.1"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class NetworkError(Exception):
    """Raised when no HTTP response was received at all (DNS, TLS, timeout...)."""


class Http(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float,
    ) -> Response: ...


class UrllibHttp:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float,
    ) -> Response:
        all_headers = {"User-Agent": USER_AGENT, **headers}
        req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status: int = resp.status
                return Response(status, resp.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read()
            except OSError:
                payload = b""
            return Response(exc.code, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(str(reason)) from exc


def parse_json(body: bytes) -> object:
    """Decode JSON into plain ``object``; callers narrow with the helpers below."""
    decoded: object = json.loads(body.decode("utf-8"))
    return decoded


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return []


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
