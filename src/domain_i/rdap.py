"""RDAP lookups: registry endpoint discovery and response parsing."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .http import Http, NetworkError, as_dict, as_list, as_str, parse_json
from .info import DomainInfo, classify_privacy, normalise_nameservers, parse_date

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_TTL = 7 * 86400
BUILTIN_ENDPOINTS: dict[str, str] = {
    "com": "https://rdap.verisign.com/com/v1/domain/",
    "net": "https://rdap.verisign.com/net/v1/domain/",
    "org": "https://rdap.publicinterestregistry.org/rdap/domain/",
    "app": "https://pubapi.registry.google/rdap/domain/",
}
ACCEPT = "application/rdap+json, application/json;q=0.9"


@dataclass(frozen=True)
class RdapResult:
    state: str  # "registered" | "unregistered" | "unknown"
    data: dict[str, object] | None
    raw: str | None
    error: str | None


class Rdap:
    def __init__(
        self,
        http: Http,
        cache_dir: Path,
        now: Callable[[], float] = time.time,
        timeout: float = 10.0,
    ) -> None:
        self._http = http
        self._cache_file = cache_dir / "dns.json"
        self._now = now
        self._timeout = timeout
        self._bootstrap: dict[str, str] | None = None

    # -- endpoint discovery ---------------------------------------------------
    def endpoint(self, domain: str) -> str | None:
        labels = domain.split(".")
        builtin = BUILTIN_ENDPOINTS.get(labels[-1])
        if builtin is not None:
            return builtin + domain
        table = self._load_bootstrap()
        for i in range(1, len(labels)):
            base = table.get(".".join(labels[i:]))
            if base is not None:
                return base.rstrip("/") + "/domain/" + domain
        return None

    def _load_bootstrap(self) -> dict[str, str]:
        if self._bootstrap is not None:
            return self._bootstrap
        cached: bytes | None = None
        fresh = False
        try:
            cached = self._cache_file.read_bytes()
            fresh = self._now() - os.path.getmtime(self._cache_file) < BOOTSTRAP_TTL
        except OSError:
            cached = None
        body = cached if fresh else None
        if body is None:
            try:
                resp = self._http.request("GET", BOOTSTRAP_URL, headers={"Accept": "application/json"}, timeout=self._timeout)
                if resp.status == 200 and _bootstrap_table(resp.body):
                    body = resp.body
                    self._write_cache(body)
            except NetworkError:
                body = None
        if body is None:
            body = cached
        self._bootstrap = _bootstrap_table(body) if body else {}
        return self._bootstrap

    def _write_cache(self, body: bytes) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(self._cache_file)
        except OSError:
            pass  # cache is an optimisation; the lookup itself already succeeded

    # -- lookups ------------------------------------------------------------
    def lookup(self, domain: str) -> RdapResult:
        url = self.endpoint(domain)
        if url is None:
            return RdapResult("unknown", None, None, f"no RDAP server known for {domain}")
        try:
            resp = self._http.request("GET", url, headers={"Accept": ACCEPT}, timeout=self._timeout)
        except NetworkError as exc:
            return RdapResult("unknown", None, None, f"RDAP {exc}")
        if resp.status == 404:
            return RdapResult("unregistered", None, None, None)
        if resp.status != 200:
            return RdapResult("unknown", None, None, f"RDAP HTTP {resp.status}")
        try:
            data = as_dict(parse_json(resp.body))
        except (ValueError, UnicodeDecodeError):
            return RdapResult("unknown", None, None, "RDAP returned invalid JSON")
        if not data:
            return RdapResult("unknown", None, None, "RDAP returned an empty object")
        raw = json.dumps(data, indent=2, ensure_ascii=False)
        return RdapResult("registered", data, raw, None)


    def registrar_privacy(self, domain: str, registry: Mapping[str, object] | None = None) -> str | None:
        """Privacy verdict from the registrar's RDAP record, or None if it cannot be decided.

        ``registry`` is the registry RDAP response when the caller already has it.
        """
        if registry is None:
            result = self.lookup(domain)
            if result.data is None:
                return None
            registry = result.data
        url = related_url(registry)
        if url is None:
            return None
        try:
            resp = self._http.request("GET", url, headers={"Accept": ACCEPT}, timeout=self._timeout)
        except NetworkError:
            return None
        if resp.status != 200:
            return None
        try:
            data = as_dict(parse_json(resp.body))
        except (ValueError, UnicodeDecodeError):
            return None
        privacy = info_from_rdap(data).privacy
        return None if privacy == "unknown" else privacy


def related_url(data: Mapping[str, object]) -> str | None:
    """The registrar's RDAP URL advertised by a registry response (``rel: related``)."""
    for link in as_list(data.get("links")):
        item = as_dict(link)
        href = as_str(item.get("href")) or ""
        kind = (as_str(item.get("type")) or "application/rdap+json").lower()
        if item.get("rel") == "related" and href.startswith(("https://", "http://")) and "json" in kind:
            return href
    return None


def _bootstrap_table(body: bytes | None) -> dict[str, str]:
    if not body:
        return {}
    try:
        root = as_dict(parse_json(body))
    except (ValueError, UnicodeDecodeError):
        return {}
    table: dict[str, str] = {}
    for service in as_list(root.get("services")):
        pair = as_list(service)
        if len(pair) != 2:
            continue
        urls = [u for u in (as_str(x) for x in as_list(pair[1])) if u]
        https = [u for u in urls if u.startswith("https://")] or urls
        if not https:
            continue
        for tld in as_list(pair[0]):
            name = as_str(tld)
            if name:
                table[name.lower()] = https[0]
    return table


def _vcard_values(entity: dict[str, object], prop: str) -> list[str]:
    card = as_list(entity.get("vcardArray"))
    if len(card) < 2:
        return []
    values: list[str] = []
    for item in as_list(card[1]):
        parts = as_list(item)
        if len(parts) >= 4 and parts[0] == prop:
            value = parts[3]
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(v for v in value if isinstance(v, str) and v)
    return values


def _entities(data: Mapping[str, object]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    stack = [as_dict(e) for e in as_list(data.get("entities"))]
    while stack:
        entity = stack.pop(0)
        found.append(entity)
        stack.extend(as_dict(e) for e in as_list(entity.get("entities")))
    return found


def _roles(entity: dict[str, object]) -> list[str]:
    return [r.lower() for r in (as_str(x) for x in as_list(entity.get("roles"))) if r]


def info_from_rdap(data: Mapping[str, object]) -> DomainInfo:
    events: dict[str, str] = {}
    for event in as_list(data.get("events")):
        ev = as_dict(event)
        action, when = as_str(ev.get("eventAction")), as_str(ev.get("eventDate"))
        if action and when and action.lower() not in events:
            events[action.lower()] = when

    def event_date(*names: str) -> date | None:
        for name in names:
            if name in events:
                return parse_date(events[name])
        return None

    registrar: str | None = None
    org: str | None = None
    name: str | None = None
    others: list[str] = []
    for entity in _entities(data):
        roles = _roles(entity)
        if "registrar" in roles and registrar is None:
            fns = _vcard_values(entity, "fn")
            registrar = fns[0] if fns else None
        if "registrant" in roles:
            orgs = [o for o in _vcard_values(entity, "org") if o.strip()]
            fns = [f for f in _vcard_values(entity, "fn") if f.strip()]
            org = org or (orgs[0] if orgs else None)
            name = name or (fns[0] if fns else None)
            for remark in as_list(entity.get("remarks")):
                r = as_dict(remark)
                others.extend(t for t in (as_str(r.get("title")),) if t)
                others.extend(s for s in (as_str(d) for d in as_list(r.get("description"))) if s)

    privacy = classify_privacy(org, name, others)
    if privacy == "unknown":
        for item in as_list(data.get("redacted")):
            label = as_str(as_dict(as_dict(item).get("name")).get("type"))
            if label and label.lower().startswith("registrant"):
                privacy = f"yes ({label} redacted)"
                break

    nameservers = normalise_nameservers(
        n for n in (as_str(as_dict(ns).get("ldhName")) for ns in as_list(data.get("nameservers"))) if n
    )
    created = event_date("registration")
    updated = event_date("last changed", "last update")
    expires = event_date("expiration")
    return DomainInfo(
        created=created,
        updated=updated,
        expires=expires,
        privacy=privacy,
        registrar=registrar,
        nameservers=nameservers,
    )
