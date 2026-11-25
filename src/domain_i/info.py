"""Registration summary model shared by the WHOIS and RDAP parsers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

PRIVACY_MARKERS: tuple[str, ...] = (
    "redacted for privacy",
    "domains by proxy",
    "withheld for privacy",
    "withheldforprivacy",
    "contact privacy",
    "privacyguardian",
    "privacy guardian",
    "whois privacy",
    "whoisguard",
    "whois protection",
    "data protected",
    "gdpr masked",
    "not disclosed",
    "privacy protect",
    "perfect privacy",
    "privacy service",
    "identity protection",
    "domain protection services",
    "super privacy",
    "private by design",
    "statutory masking",
    "non-public data",
    "please query the rdds",
    "redacted",
    "proxy",
)


@dataclass(frozen=True)
class DomainInfo:
    created: date | None = None
    updated: date | None = None
    expires: date | None = None
    privacy: str = "unknown"
    registrar: str | None = None
    nameservers: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.created or self.updated or self.expires or self.registrar or self.nameservers)


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(value: str) -> date | None:
    """Best-effort parse of the many date spellings WHOIS servers use."""
    text = value.strip().lower()
    if not text:
        return None
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d{1,2})[-. /]([a-z]{3})[a-z]*[-. /,]+(\d{4})", text)
    if m and m[2] in _MONTHS:
        return _safe_date(int(m[3]), _MONTHS[m[2]], int(m[1]))
    m = re.search(r"([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", text)
    if m and m[1] in _MONTHS:
        return _safe_date(int(m[3]), _MONTHS[m[1]], int(m[2]))
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return _safe_date(int(m[3]), int(m[2]), int(m[1]))
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


# Registries that hide natural persons sometimes publish just a placeholder word.
PLACEHOLDER_HOLDERS: tuple[str, ...] = ("individual", "private person", "private", "hidden", "n/a")


def privacy_marker(value: str) -> str | None:
    lowered = value.lower()
    if lowered.strip().rstrip(".") in PLACEHOLDER_HOLDERS:
        return lowered.strip()
    for marker in PRIVACY_MARKERS:
        if marker in lowered:
            return marker
    return None


def classify_privacy(organisation: str | None, name: str | None, others: Iterable[str] = ()) -> str:
    """``yes (...)`` when a privacy/proxy marker is seen, ``no (...)`` for a real holder."""
    org = (organisation or "").strip()
    person = (name or "").strip()
    if org and privacy_marker(org) is None:
        return f"no ({shorten(org)})"
    for candidate in (org, person, *others):
        if candidate.strip() and privacy_marker(candidate) is not None:
            return f"yes ({shorten(candidate.strip())})"
    if person:
        return f"no ({shorten(person)})"
    return "unknown"


def shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalise_nameservers(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        parts = value.strip().split()
        if not parts:
            continue
        host = parts[0].lower().rstrip(".")
        if host and "." in host and host not in seen:
            seen.append(host)
    return tuple(seen)
