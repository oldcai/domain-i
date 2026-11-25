from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from domain_i.http import NetworkError, Response


@dataclass
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout: float


@dataclass
class FakeHttp:
    """Routes exact URLs (or URL prefixes ending with '*') to canned responses."""

    routes: dict[str, Response | Exception | Callable[[Call], Response]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float,
    ) -> Response:
        self.calls.append(Call(method, url, dict(headers), body, timeout))
        target = self.routes.get(url)
        if target is None:
            for pattern, value in self.routes.items():
                if pattern.endswith("*") and url.startswith(pattern[:-1]):
                    target = value
                    break
        if target is None:
            raise NetworkError(f"unrouted URL in test: {url}")
        if isinstance(target, Exception):
            raise target
        if isinstance(target, Response):
            return target
        return target(self.calls[-1])

    def urls(self) -> list[str]:
        return [c.url for c in self.calls]
