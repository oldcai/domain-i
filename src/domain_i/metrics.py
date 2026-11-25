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

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

from .http import Http, NetworkError, as_dict, as_list, as_str, parse_json

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


# --- external metrics command (DOMAIN_I_METRICS_CMD) ----------------------------------
#
# Protocol: the command is run with the domain appended as the last argument
# and prints one JSON object on stdout. All fields are optional:
#   {"traffic": <monthly visits>, "traffic_month": "YYYY-MM", "dr": <0-100>, "source": "<name>"}

COMMAND_VAR = "DOMAIN_I_METRICS_CMD"
COMMAND_TIMEOUT = 8.0

CommandRunner = Callable[[list[str], float], str]


def default_command_runner(args: list[str], timeout: float) -> str:
    """Run without a shell; raises TimeoutExpired / CalledProcessError / OSError."""
    proc = subprocess.run(args, capture_output=True, timeout=timeout, check=True, stdin=subprocess.DEVNULL)
    return proc.stdout.decode("utf-8", errors="replace")


def abbreviate(value: float) -> str:
    """745628929 -> '745.6M'; values under 1000 are shown as integers."""
    number = Decimal(str(value))
    if number.quantize(Decimal(1), rounding=ROUND_HALF_UP) < 1000:
        return str(int(number.quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    for suffix, size in (("K", Decimal(1000)), ("M", Decimal(10) ** 6), ("B", Decimal(10) ** 9)):
        scaled = (number / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if scaled < 1000 or suffix == "B":
            return f"{scaled}{suffix}"
    raise AssertionError("unreachable")


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def parse_command_output(text: str) -> tuple[list[Metric], str | None]:
    try:
        data = parse_json(text.encode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return [], "output is not JSON"
    if not isinstance(data, dict):
        return [], "output is not a JSON object"
    obj = as_dict(data)
    source = as_str(obj.get("source")) or "custom"
    month = as_str(obj.get("traffic_month"))
    found: list[Metric] = []
    traffic = _positive_number(obj.get("traffic"))
    if traffic is not None:
        found.append(Metric("Traffic", f"{abbreviate(traffic)}/mo", f"{source}, {month}" if month else source))
    dr = _positive_number(obj.get("dr"))
    if dr is not None and dr <= 100:
        found.append(Metric("DR", f"{dr:.0f}", source))
    return found, None


def run_metrics_command(command: str, domain: str, runner: CommandRunner) -> tuple[list[Metric], str | None]:
    try:
        args = [os.path.expanduser(a) for a in shlex.split(command)]
    except ValueError as exc:
        return [], f"cannot parse {COMMAND_VAR}: {exc}"
    if not args:
        return [], f"{COMMAND_VAR} is empty"
    try:
        output = runner([*args, domain], COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], f"timed out after {COMMAND_TIMEOUT:g}s"
    except subprocess.CalledProcessError as exc:
        return [], f"exit status {exc.returncode}"
    except OSError as exc:
        return [], str(exc)
    return parse_command_output(output)


ORDER = ("Traffic", "DR", "Rank")


def merge_metrics(custom: list[Metric], builtin: list[Metric]) -> list[Metric]:
    """Custom rows win per label (so an external DR replaces Ahrefs); fixed row order."""
    by_label: dict[str, Metric] = {m.label: m for m in builtin}
    by_label.update({m.label: m for m in custom})
    ordered = [by_label[label] for label in ORDER if label in by_label]
    return ordered + [m for label, m in by_label.items() if label not in ORDER]
