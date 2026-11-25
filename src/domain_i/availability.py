"""Availability mode: which names to check, which backend answers, and the stop rule."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .inputs import variants
from .rdap import RdapResult
from .spaceship import Availability, SpaceshipError

DEFAULT_TLDS: tuple[str, ...] = ("com", "net", "org", "app")
TLDS_VAR = "DOMAIN_I_TLDS"
_TLD = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")

SpaceshipCheck = Callable[[list[str]], dict[str, Availability]]
RdapLookup = Callable[[str], RdapResult]


@dataclass(frozen=True)
class Row:
    domain: str
    status: str  # "available" | "taken" | "unknown"
    price: str | None


@dataclass(frozen=True)
class CheckResult:
    rows: list[Row]
    backend: str


@dataclass(frozen=True)
class Report:
    rows: list[Row]
    backends: list[str]
    hyphen_checked: bool
    hint: str | None


Checker = Callable[[list[str]], CheckResult]


# --- TLD configuration -------------------------------------------------------


def parse_tlds(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for token in re.split(r"[\s,]+", raw.strip().lower()):
        tld = token.strip(".")
        if tld and _TLD.match(tld) and tld not in out:
            out.append(tld)
    return tuple(out)


def resolve_tlds(
    cli_value: str | None, environ: Mapping[str, str], config: Mapping[str, str]
) -> tuple[tuple[str, ...], str | None]:
    """Priority: ``--tlds`` > ``$DOMAIN_I_TLDS`` > config file > default.

    The highest-priority source that is *set* wins; if its value is empty or
    entirely invalid the built-in default is used and a warning is returned.
    """
    for source, value in (
        ("--tlds", cli_value),
        (f"${TLDS_VAR}", environ.get(TLDS_VAR)),
        (f"{TLDS_VAR} in config file", config.get(TLDS_VAR)),
    ):
        if value is None:
            continue
        parsed = parse_tlds(value)
        if parsed:
            return parsed, None
        default = ",".join(DEFAULT_TLDS)
        return DEFAULT_TLDS, f"domain-i: no valid TLDs in {source} ({value!r}); using default {default}"
    return DEFAULT_TLDS, None


# --- backends ------------------------------------------------------------------


def _from_rdap(result: RdapResult) -> str:
    return {"registered": "taken", "unregistered": "available"}.get(result.state, "unknown")


def rdap_rows(domains: list[str], lookup: RdapLookup) -> list[Row]:
    if not domains:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(domains))) as pool:
        results = list(pool.map(lookup, domains))
    return [Row(d, _from_rdap(r), None) for d, r in zip(domains, results)]


def check_domains(domains: list[str], spaceship: SpaceshipCheck | None, rdap: RdapLookup) -> CheckResult:
    if spaceship is None:
        return CheckResult(rdap_rows(domains, rdap), "RDAP (no Spaceship credentials)")
    try:
        answers = spaceship(domains)
    except SpaceshipError as exc:
        return CheckResult(rdap_rows(domains, rdap), f"RDAP (Spaceship failed: {exc})")
    missing = [d for d in domains if d not in answers or answers[d].status == "unknown"]
    fallback = {row.domain: row for row in rdap_rows(missing, rdap)}
    rows: list[Row] = []
    for d in domains:
        if d in fallback:
            rows.append(fallback[d])
            continue
        a = answers[d]
        rows.append(Row(d, a.status, a.premium if a.status == "available" else None))
    return CheckResult(rows, "Spaceship + RDAP" if missing else "Spaceship")


# --- flow ------------------------------------------------------------------------


def run_availability(
    words: tuple[str, ...],
    tlds: tuple[str, ...],
    original: str | None,
    checker: Checker,
) -> Report:
    plain, hyphen = variants(words)
    plain_domains = [f"{plain}.{t}" for t in tlds]
    first = [original] if original else []
    result = checker(first + [d for d in plain_domains if d != original])
    rows = list(result.rows)
    backends = [result.backend]
    plain_rows = [r for r in rows if r.domain in plain_domains]
    all_taken = bool(plain_rows) and all(r.status == "taken" for r in plain_rows)
    if not all_taken:
        return Report(rows, backends, False, None)
    if hyphen is None:
        hint = (
            f"No hyphenated variant: '{plain}' could not be split into words. "
            f"Retry with spaces to choose the split, e.g. domain-i {plain[: len(plain) // 2]} {plain[len(plain) // 2 :]}"
        )
        return Report(rows, backends, False, hint)
    seen = {r.domain for r in rows}
    second = checker([d for d in (f"{hyphen}.{t}" for t in tlds) if d not in seen])
    rows.extend(second.rows)
    if second.backend not in backends:
        backends.append(second.backend)
    return Report(rows, backends, True, None)
