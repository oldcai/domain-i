"""Optional popularity/authority metrics for a registered domain.

Sources (all free):

* Tranco (https://tranco-list.eu) - research top-sites rank, no key needed.
  Shown as ``Rank #N (tranco)``. It is a rank, not a visit count.
* Ahrefs ``domain-rating-free`` - real Domain Rating, needs a free API key in
  ``AHREFS_API_KEY`` (environment or config file). Shown as ``DR N (ahrefs)``.

Every failure is swallowed: metrics must never break or slow the main lookup
beyond their own timeout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

from .http import Http, NetworkError, as_dict, as_list, parse_json

TRANCO_URL = "https://tranco-list.eu/api/ranks/domain/"
AHREFS_URL = "https://api.ahrefs.com/v3/public/domain-rating-free?output=json&target="
AHREFS_KEY_VAR = "AHREFS_API_KEY"
TIMEOUT = 8.0


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    source: str


MetricsFetcher = Callable[[str], list[Metric]]


def _get_json(http: Http, url: str, headers: Mapping[str, str]) -> object | None:
    try:
        resp = http.request("GET", url, headers={"Accept": "application/json", **headers}, timeout=TIMEOUT)
    except NetworkError:
        return None
    if resp.status != 200:
        return None
    try:
        return parse_json(resp.body)
    except (ValueError, UnicodeDecodeError):
        return None


def tranco_rank(http: Http, domain: str) -> Metric | None:
    data = as_dict(_get_json(http, TRANCO_URL + quote(domain), {}))
    ranks = as_list(data.get("ranks"))
    if not ranks:
        return None
    rank = as_dict(ranks[0]).get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
        return None
    return Metric("Rank", f"#{rank:,}", "tranco")


def ahrefs_dr(http: Http, domain: str, key: str) -> Metric | None:
    data = as_dict(_get_json(http, AHREFS_URL + quote(domain), {"Authorization": f"Bearer {key}"}))
    value = as_dict(data.get("domain_rating")).get("domain_rating")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return Metric("DR", f"{value:.0f}", "ahrefs")


def fetch_metrics(http: Http, domain: str, settings: Mapping[str, str]) -> list[Metric]:
    key = settings.get(AHREFS_KEY_VAR)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rank = pool.submit(tranco_rank, http, domain)
        dr = pool.submit(ahrefs_dr, http, domain, key) if key else None
        found = [rank.result(), dr.result() if dr else None]
    return [m for m in found if m is not None]
