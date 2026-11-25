"""Optional Spaceship bulk availability backend (gives premium prices)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import parse_env_file, read_config
from .http import Http, NetworkError, as_dict, as_list, as_str, parse_json

__all__ = ["SPACESHIP_URL", "Availability", "Credentials", "SpaceshipError", "check", "load_credentials", "parse_env_file"]

SPACESHIP_URL = "https://spaceship.dev/api/v1/domains/available"
KEY_VAR = "SPACESHIP_API_KEY"
SECRET_VAR = "SPACESHIP_API_SECRET"


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str

    def __repr__(self) -> str:  # never leak secrets through logging/tracebacks
        return "Credentials(<redacted>)"


@dataclass(frozen=True)
class Availability:
    domain: str
    status: str  # "available" | "taken" | "unknown"
    premium: str | None


class SpaceshipError(Exception):
    pass


def load_credentials(environ: Mapping[str, str], config_dir: Path) -> Credentials | None:
    key, secret = environ.get(KEY_VAR), environ.get(SECRET_VAR)
    if key and secret:
        return Credentials(key, secret)
    config = read_config(environ, config_dir)
    key, secret = config.get(KEY_VAR), config.get(SECRET_VAR)
    if key and secret:
        return Credentials(key, secret)
    return None


def _status(result: str | None) -> str:
    if result == "available":
        return "available"
    if result in ("taken", "unavailable", "registered"):
        return "taken"
    return "unknown"


def _premium(item: dict[str, object]) -> str | None:
    for entry in as_list(item.get("premiumPricing")):
        p = as_dict(entry)
        if p.get("operation") != "register":
            continue
        price = p.get("price")
        currency = (as_str(p.get("currency")) or "USD").upper()
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            return f"{price:.2f} {currency}"
        if isinstance(price, str) and price:
            return f"{price} {currency}"
    return None


def check(http: Http, creds: Credentials, domains: list[str], timeout: float = 15.0) -> dict[str, Availability]:
    body = json.dumps({"domains": domains}).encode()
    headers = {
        "X-Api-Key": creds.key,
        "X-Api-Secret": creds.secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = http.request("POST", SPACESHIP_URL, headers=headers, body=body, timeout=timeout)
    except NetworkError as exc:
        raise SpaceshipError(f"network error: {exc}") from exc
    if resp.status != 200:
        raise SpaceshipError(f"HTTP {resp.status}")
    try:
        data = as_dict(parse_json(resp.body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpaceshipError("invalid JSON response") from exc
    if "domains" not in data:
        raise SpaceshipError("unexpected response shape")
    results: dict[str, Availability] = {}
    for raw in as_list(data.get("domains")):
        item = as_dict(raw)
        name = (as_str(item.get("domain")) or "").lower()
        if name:
            results[name] = Availability(name, _status(as_str(item.get("result"))), _premium(item))
    return results
