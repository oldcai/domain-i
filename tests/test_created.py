from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import rdap_fixture, whois_fixture
from fakes import FakeHttp

from domain_i.created import fetch_created
from domain_i.http import NetworkError, Response
from domain_i.rdap import BOOTSTRAP_URL, Rdap

VERISIGN = "https://rdap.verisign.com/com/v1/domain/"


def make(http: FakeHttp, tmp_path: Path) -> Rdap:
    return Rdap(http, cache_dir=tmp_path / "cache")


def no_whois(args: list[str], timeout: float) -> str:
    raise FileNotFoundError("whois")


def test_created_from_rdap_registration_event(tmp_path: Path) -> None:
    http = FakeHttp({VERISIGN + "google.com": Response(200, rdap_fixture("google.com.json"))})
    assert fetch_created("google.com", make(http, tmp_path), no_whois) == ("1997-09-15", None)


def test_rdap_without_registration_event_falls_back_to_whois(tmp_path: Path) -> None:
    http = FakeHttp({VERISIGN + "google.com": Response(200, json.dumps({"objectClassName": "domain", "events": []}).encode())})
    calls: list[list[str]] = []

    def whois(args: list[str], timeout: float) -> str:
        calls.append(args)
        return whois_fixture("google.com")

    assert fetch_created("google.com", make(http, tmp_path), whois) == ("1997-09-15", None)
    assert calls == [["whois", "google.com"]]


def test_rdap_failure_or_missing_server_falls_back_to_whois(tmp_path: Path) -> None:
    http = FakeHttp(
        {VERISIGN + "baidu.com": NetworkError("down"), BOOTSTRAP_URL: Response(200, rdap_fixture("dns.json"))}
    )
    rdap = make(http, tmp_path)
    assert fetch_created("baidu.com", rdap, lambda a, t: whois_fixture("google.com"))[0] == "1997-09-15"
    # .cn has no RDAP server in the bootstrap fixture -> whois
    assert fetch_created("baidu.cn", rdap, lambda a, t: whois_fixture("baidu.cn"))[0] == "2003-03-17"


def test_both_sources_fail_gives_empty_with_reason(tmp_path: Path) -> None:
    http = FakeHttp({VERISIGN + "a.com": Response(500, b"")})

    def timeout(args: list[str], t: float) -> str:
        raise subprocess.TimeoutExpired(args, t)

    created, reason = fetch_created("a.com", make(http, tmp_path), timeout)
    assert created is None
    assert reason is not None and "RDAP HTTP 500" in reason and "timed out" in reason


def test_rdap_lookups_are_memoised(tmp_path: Path) -> None:
    http = FakeHttp({VERISIGN + "google.com": Response(200, rdap_fixture("google.com.json"))})
    rdap = make(http, tmp_path)
    rdap.lookup("google.com")
    fetch_created("google.com", rdap, no_whois)
    assert http.urls().count(VERISIGN + "google.com") == 1
