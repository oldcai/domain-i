"""Registration date of a domain for the availability table.

Registry RDAP first (``registration`` event), then the system ``whois`` client.
Both are public and need no key.
"""

from __future__ import annotations

from .rdap import Rdap, info_from_rdap
from .whois import WhoisRunner, parse_whois, run_whois

WHOIS_TIMEOUT = 10.0


def fetch_created(domain: str, rdap: Rdap, whois_runner: WhoisRunner) -> tuple[str | None, str | None]:
    """``(YYYY-MM-DD, None)`` or ``(None, reason)``."""
    result = rdap.lookup(domain)
    if result.data is not None:
        created = info_from_rdap(result.data).created
        if created is not None:
            return created.isoformat(), None
        rdap_reason = "RDAP has no registration event"
    else:
        rdap_reason = result.error or f"RDAP says {result.state}"
    lookup = run_whois(domain, WHOIS_TIMEOUT, whois_runner)
    if lookup.error:
        return None, f"{rdap_reason}; {lookup.error}"
    info = parse_whois(lookup.text).info
    if info is not None and info.created is not None:
        return info.created.isoformat(), None
    return None, f"{rdap_reason}; WHOIS has no creation date"
