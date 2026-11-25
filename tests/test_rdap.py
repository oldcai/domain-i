import json
import os
from datetime import date
from pathlib import Path

from conftest import rdap_fixture
from fakes import FakeHttp
from domain_i.http import NetworkError, Response
from domain_i.rdap import BOOTSTRAP_URL, Rdap, info_from_rdap

DNS_JSON = rdap_fixture("dns.json")


def make(http: FakeHttp, tmp_path: Path, now: float = 1_000_000.0) -> Rdap:
    return Rdap(http, cache_dir=tmp_path / "cache", now=lambda: now)


def test_builtin_endpoints_skip_bootstrap(tmp_path: Path) -> None:
    rdap = make(FakeHttp(), tmp_path)
    assert rdap.endpoint("google.com") == "https://rdap.verisign.com/com/v1/domain/google.com"
    assert rdap.endpoint("google.net") == "https://rdap.verisign.com/net/v1/domain/google.net"
    assert rdap.endpoint("google.org") == "https://rdap.publicinterestregistry.org/rdap/domain/google.org"
    assert rdap.endpoint("cash.app") == "https://pubapi.registry.google/rdap/domain/cash.app"


def test_bootstrap_is_fetched_once_and_cached(tmp_path: Path) -> None:
    http = FakeHttp({BOOTSTRAP_URL: Response(200, DNS_JSON)})
    rdap = make(http, tmp_path)
    assert rdap.endpoint("google.co.uk") == "https://rdap.nominet.uk/uk/domain/google.co.uk"
    assert (tmp_path / "cache" / "dns.json").exists()
    again = make(http, tmp_path)
    assert again.endpoint("web.dev") == "https://pubapi.registry.google/rdap/domain/web.dev"
    assert http.urls().count(BOOTSTRAP_URL) == 1


def test_stale_bootstrap_is_refreshed(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "dns.json").write_bytes(DNS_JSON)
    os.utime(cache / "dns.json", (0, 0))
    http = FakeHttp({BOOTSTRAP_URL: Response(200, DNS_JSON)})
    make(http, tmp_path, now=30 * 86400.0).endpoint("google.co.uk")
    assert http.urls() == [BOOTSTRAP_URL]


def test_stale_bootstrap_used_when_refresh_fails(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "dns.json").write_bytes(DNS_JSON)
    os.utime(cache / "dns.json", (0, 0))
    http = FakeHttp({BOOTSTRAP_URL: NetworkError("offline")})
    rdap = make(http, tmp_path, now=30 * 86400.0)
    assert rdap.endpoint("google.co.uk") == "https://rdap.nominet.uk/uk/domain/google.co.uk"


def test_unknown_tld_has_no_endpoint(tmp_path: Path) -> None:
    http = FakeHttp({BOOTSTRAP_URL: Response(200, DNS_JSON)})
    assert make(http, tmp_path).endpoint("google.de") is None


def test_lookup_states(tmp_path: Path) -> None:
    http = FakeHttp(
        {
            "https://pubapi.registry.google/rdap/domain/cash.app": Response(200, rdap_fixture("cash.app.json")),
            "https://pubapi.registry.google/rdap/domain/free.app": Response(404, b""),
            "https://pubapi.registry.google/rdap/domain/limit.app": Response(429, b""),
            "https://pubapi.registry.google/rdap/domain/down.app": NetworkError("timed out"),
            BOOTSTRAP_URL: Response(200, DNS_JSON),
        }
    )
    rdap = make(http, tmp_path)
    found = rdap.lookup("cash.app")
    assert found.state == "registered"
    assert found.data is not None and found.data.get("ldhName") == "cash.app"
    assert found.raw is not None
    assert rdap.lookup("free.app").state == "unregistered"
    assert rdap.lookup("limit.app").state == "unknown"
    down = rdap.lookup("down.app")
    assert down.state == "unknown" and down.error is not None
    assert rdap.lookup("google.de").state == "unknown"
    accept = http.calls[0].headers.get("Accept", "")
    assert "rdap+json" in accept


def test_lookup_bad_json_is_unknown(tmp_path: Path) -> None:
    http = FakeHttp({"https://pubapi.registry.google/rdap/domain/x.app": Response(200, b"<html>")})
    assert make(http, tmp_path).lookup("x.app").state == "unknown"


def test_info_from_rdap_cash_app() -> None:
    data = json.loads(rdap_fixture("cash.app.json"))
    info = info_from_rdap(data)
    assert info.created == date(2018, 3, 29)
    assert info.updated == date(2026, 10, 17)
    assert info.expires == date(2029, 3, 29)
    assert info.registrar == "CSC Corporate Domains, Inc."
    assert info.nameservers == (
        "ns-1248.awsdns-28.org",
        "ns-1816.awsdns-35.co.uk",
        "ns-311.awsdns-38.com",
        "ns-810.awsdns-37.net",
    )
    assert info.privacy == "unknown"


def test_info_from_rdap_google_com() -> None:
    info = info_from_rdap(json.loads(rdap_fixture("google.com.json")))
    assert info.created == date(1997, 9, 15)
    assert info.registrar == "MarkMonitor Inc."
    assert "ns1.google.com" in info.nameservers


def _vcard(**props: str) -> list[object]:
    return ["vcard", [["version", {}, "text", "4.0"], *[[k, {}, "text", v] for k, v in props.items()]]]


def test_info_from_rdap_registrant_privacy() -> None:
    redacted = {"entities": [{"roles": ["registrant"], "vcardArray": _vcard(fn="REDACTED FOR PRIVACY")}]}
    assert info_from_rdap(redacted).privacy == "yes (REDACTED FOR PRIVACY)"
    org = {"entities": [{"roles": ["registrant"], "vcardArray": _vcard(fn="", org="Acme Corp")}]}
    assert info_from_rdap(org).privacy == "no (Acme Corp)"
    rfc9537 = {"redacted": [{"name": {"type": "Registrant Name"}}]}
    assert info_from_rdap(rfc9537).privacy == "yes (Registrant Name redacted)"
