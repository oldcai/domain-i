"""Run the system ``whois`` client and condense its output."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .info import DomainInfo, classify_privacy, normalise_nameservers, parse_date

WhoisRunner = Callable[[list[str], float], str]

CREATED_KEYS = (
    "creation date", "created", "created on", "created date", "registration time", "registered on",
    "registered", "registration date", "domain registration date", "domain create date", "record created",
    "登録年月日",
)
UPDATED_KEYS = (
    "updated date", "last updated", "last updated on", "last modified", "last-modified", "changed",
    "modified", "modification date", "updated", "last update", "domain last updated date", "最終更新",
)
EXPIRES_KEYS = (
    "registry expiry date", "registrar registration expiration date", "expiration time", "expiry date",
    "expiration date", "expires on", "expires", "expire date", "paid-till", "renewal date",
    "domain expiration date", "record expires on", "有効期限",
)
REGISTRAR_KEYS = ("registrar", "sponsoring registrar", "registrar name", "registrar organization")
NS_KEYS = ("name server", "nameserver", "nserver", "name servers", "nameservers", "dns")
DOMAIN_KEYS = ("domain name", "domain")
REGISTRANT_ORG_KEYS = ("registrant organization", "registrant organisation", "registrant", "登録者名", "org", "holder")
REGISTRANT_NAME_KEYS = ("registrant name", "registrant contact name")

NOT_FOUND = re.compile(
    r"(no match for|^no match|not found|no data found|no entries found|is free\b|^status:\s*free\b|"
    r"no matching record|domain not found|no object found|object does not exist|"
    r"this domain name has not been registered|is available for registration|^available\b)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class WhoisLookup:
    text: str
    error: str | None = None


@dataclass(frozen=True)
class WhoisParse:
    info: DomainInfo | None
    not_found: str | None


def default_runner(args: list[str], timeout: float) -> str:
    proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    return proc.stdout.decode("utf-8", errors="replace")


def run_whois(domain: str, timeout: float = 10.0, runner: WhoisRunner = default_runner) -> WhoisLookup:
    try:
        return WhoisLookup(runner(["whois", domain], timeout))
    except subprocess.TimeoutExpired:
        return WhoisLookup("", f"whois timed out after {timeout:g}s")
    except OSError as exc:
        return WhoisLookup("", f"whois could not run: {exc}")


def strip_iana(text: str) -> str:
    """Drop the IANA TLD record that macOS/BSD whois prints before the referral."""
    if "% IANA WHOIS server" not in text:
        return text
    sections = re.split(r"^# (?=\S+\s*$)", text, flags=re.MULTILINE)
    rest = [s.split("\n", 1)[1] if "\n" in s else "" for s in sections[1:]]
    return "\n".join(rest)


_BRACKET = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


def _fields(text: str) -> list[tuple[str, str]]:
    """Flatten the common WHOIS layouts into ``(lowercase key, value)`` pairs.

    Handles ``Key: value`` lines, JPRS-style ``[Key]   value`` lines, and
    indented blocks (``Registrar:`` followed by indented lines, as used by
    .eu and Nominet), which yield ``(block, line)`` and ``(block key, value)``.
    """
    out: list[tuple[str, str]] = []
    block: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            block = None
            continue
        if stripped[0] in "%#>":
            continue
        bracket = _BRACKET.match(stripped)
        if bracket:
            out.append((bracket[1].strip().lower(), bracket[2].strip()))
            continue
        indented = line[:1].isspace()
        if indented and block is not None:
            key, sep, value = stripped.partition(":")
            key, value = key.strip().lower(), value.strip()
            # "Name: X" style sub-fields; keys never contain dots or brackets,
            # which keeps "ns1.example.eu (2001:db8::1)" a plain block line.
            if sep and value and re.fullmatch(r"[a-z][a-z /-]*", key):
                out.append((key, value))
                out.append((f"{block} {key}", value))
            else:
                out.append((block, stripped))
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip().lower(), value.strip()
        block = key if not value and not indented else None
        out.append((key, value))
    return out


def _first(fields: list[tuple[str, str]], keys: tuple[str, ...]) -> str | None:
    for wanted in keys:
        for key, value in fields:
            if key == wanted and value:
                return value
    return None


def parse_whois(raw: str) -> WhoisParse:
    text = strip_iana(raw)
    fields = _fields(text)

    def first_date(keys: tuple[str, ...]) -> date | None:
        for wanted in keys:
            for key, value in fields:
                if key == wanted and value:
                    parsed = parse_date(value)
                    if parsed is not None:
                        return parsed
        return None

    nameservers = normalise_nameservers(v for k, v in fields if k in NS_KEYS)
    registrant_values = [v for k, v in fields if k.startswith("registrant") and v]
    info = DomainInfo(
        created=first_date(CREATED_KEYS),
        updated=first_date(UPDATED_KEYS),
        expires=first_date(EXPIRES_KEYS),
        privacy=classify_privacy(
            _first(fields, REGISTRANT_ORG_KEYS), _first(fields, REGISTRANT_NAME_KEYS), registrant_values
        ),
        registrar=_first(fields, REGISTRAR_KEYS),
        nameservers=nameservers,
    )
    has_domain_line = _first(fields, DOMAIN_KEYS) is not None
    if not info.is_empty() or (has_domain_line and not NOT_FOUND.search(text)):
        return WhoisParse(info, None)
    match = NOT_FOUND.search(text)
    if match:
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        return WhoisParse(None, text[line_start : line_end if line_end != -1 else None].strip())
    return WhoisParse(None, None)
