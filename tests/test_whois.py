import subprocess
from datetime import date

import pytest

from conftest import whois_fixture
from domain_i.whois import parse_whois, run_whois, strip_iana


def test_strip_iana_keeps_only_registry_sections() -> None:
    text = strip_iana(whois_fixture("google.com"))
    assert "VeriSign Global Registry Services" not in text
    assert "Domain Name: GOOGLE.COM" in text


def test_strip_iana_suffix_only_output_is_empty() -> None:
    assert strip_iana(whois_fixture("cash.app")).strip() == ""


def test_com_thin_registry_with_registrar_section() -> None:
    parsed = parse_whois(whois_fixture("google.com"))
    assert not parsed.not_found
    info = parsed.info
    assert info is not None
    assert info.created == date(1997, 9, 15)
    assert info.updated == date(2019, 9, 9)
    assert info.expires == date(2028, 9, 14)
    assert info.registrar == "MarkMonitor Inc."
    assert info.nameservers == ("ns1.google.com", "ns2.google.com", "ns3.google.com", "ns4.google.com")
    # MarkMonitor publishes empty registrant name and no organisation.
    assert info.privacy == "unknown"


def test_org() -> None:
    info = parse_whois(whois_fixture("google.org")).info
    assert info is not None
    assert info.created == date(1998, 10, 21)
    assert info.expires == date(2027, 10, 20)
    assert info.registrar == "MarkMonitor Inc."
    assert info.nameservers[0] == "ns1.google.com"


def test_net() -> None:
    info = parse_whois(whois_fixture("google.net")).info
    assert info is not None
    assert info.created is not None and info.created.year == 1999
    assert info.registrar == "MarkMonitor Inc."
    assert len(info.nameservers) == 4


def test_io_disclosed_organisation_means_no_privacy() -> None:
    info = parse_whois(whois_fixture("google.io")).info
    assert info is not None
    assert info.created == date(2002, 10, 1)
    assert info.privacy == "no (Google LLC)"
    assert info.nameservers == ("ns1.google.com", "ns4.google.com", "ns3.google.com", "ns2.google.com")


def test_cn() -> None:
    info = parse_whois(whois_fixture("baidu.cn")).info
    assert info is not None
    assert info.created == date(2003, 3, 17)
    assert info.updated is None
    assert info.expires == date(2029, 3, 17)
    assert info.registrar == "互联网域名系统北京市工程研究中心有限公司"
    assert info.privacy == "no (北京百度网讯科技有限公司)"
    assert info.nameservers == ("ns1.baidu.cn", "ns2.baidu.cn", "ns3.baidu.cn", "ns4.baidu.cn")


def test_de_minimal_record() -> None:
    parsed = parse_whois(whois_fixture("google.de"))
    info = parsed.info
    assert info is not None
    assert info.created is None
    assert info.updated == date(2018, 3, 12)
    assert info.registrar is None
    assert info.nameservers == ("ns1.google.com", "ns2.google.com", "ns3.google.com", "ns4.google.com")


@pytest.mark.parametrize("name", ["cash.app", "google.co.uk"])
def test_suffix_only_output_has_no_record_and_no_not_found(name: str) -> None:
    parsed = parse_whois(whois_fixture(name))
    assert parsed.info is None
    assert parsed.not_found is None


def test_unregistered_com() -> None:
    parsed = parse_whois(whois_fixture("unregistered.com"))
    assert parsed.info is None
    assert parsed.not_found is not None
    assert "No match" in parsed.not_found


@pytest.mark.parametrize(
    ("registrant", "expected"),
    [
        ("Registrant Name: REDACTED FOR PRIVACY\nRegistrant Organization: \n", "yes (REDACTED FOR PRIVACY)"),
        ("Registrant Organization: Domains By Proxy, LLC\n", "yes (Domains By Proxy, LLC)"),
        (
            "Registrant Name: Redacted for Privacy\nRegistrant Organization: Privacy service provided by Withheld for Privacy ehf\n",
            "yes (Privacy service provided by Withheld for Privacy ehf)",
        ),
        ("Registrant Organization: Contact Privacy Inc. Customer 1234\n", "yes (Contact Privacy Inc. Customer 1234)"),
        ("Registrant Name: Data Protected\n", "yes (Data Protected)"),
        ("Registrant Name: GDPR Masked\n", "yes (GDPR Masked)"),
        ("Registrant Name: Not Disclosed\n", "yes (Not Disclosed)"),
        ("Registrant Organization: c/o whoisproxy.com\n", "yes (c/o whoisproxy.com)"),
        ("Registrant Organization: Registrant of sourcehut.org\n", "yes (Registrant of sourcehut.org)"),
        ("Registrant Organization: Acme Corp\nRegistrant Name: REDACTED\n", "no (Acme Corp)"),
        ("Registrant Name: Jane Example\n", "no (Jane Example)"),
        ("Registrant Country: US\n", "unknown"),
    ],
)
def test_privacy_classification(registrant: str, expected: str) -> None:
    text = "Domain Name: example.com\nCreation Date: 2020-01-02T00:00:00Z\n" + registrant
    info = parse_whois(text).info
    assert info is not None
    assert info.privacy == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Creation Date: 15-Sep-1997", date(1997, 9, 15)),
        ("Creation Date: 1997.09.15", date(1997, 9, 15)),
        ("Creation Date: 1997/09/15 10:00:00", date(1997, 9, 15)),
        ("Creation Date: 15.09.1997", date(1997, 9, 15)),
        ("Registered on: 15 September 1997", date(1997, 9, 15)),
    ],
)
def test_date_formats(line: str, expected: date) -> None:
    info = parse_whois(f"Domain Name: example.com\n{line}\n").info
    assert info is not None
    assert info.created == expected


@pytest.mark.parametrize(
    "marker",
    ["NOT FOUND", "No Data Found", "No entries found for the selected source(s).", "example.com is free", "Status: free", "No matching record."],
)
def test_not_found_markers(marker: str) -> None:
    assert parse_whois(f"% header\n{marker}\n").not_found is not None


def test_run_whois_times_out() -> None:
    def runner(args: list[str], timeout: float) -> str:
        raise subprocess.TimeoutExpired(args, timeout)

    result = run_whois("example.com", timeout=0.1, runner=runner)
    assert result.text == ""
    assert result.error is not None and "timed out" in result.error


def test_run_whois_missing_binary() -> None:
    def runner(args: list[str], timeout: float) -> str:
        raise FileNotFoundError("whois")

    result = run_whois("example.com", runner=runner)
    assert result.error is not None


def test_rs_small_site_individual_registrant_is_hidden() -> None:
    info = parse_whois(whois_fixture("lobste.rs")).info
    assert info is not None
    assert info.created == date(2011, 12, 13)
    assert info.updated == date(2025, 3, 17)
    assert info.expires == date(2029, 12, 13)
    assert info.registrar == "Mainstream Public Cloud Services d.o.o."
    assert info.privacy == "yes (Individual)"
    assert info.nameservers[0] == "ns1.dnsimple.com"


def test_jp_bracket_format() -> None:
    info = parse_whois(whois_fixture("google.jp")).info
    assert info is not None
    assert info.created == date(2005, 5, 30)
    assert info.expires == date(2027, 5, 31)
    assert info.updated == date(2026, 6, 1)
    assert info.privacy == "no (Google LLC)"
    assert info.nameservers == ("ns1.google.com", "ns2.google.com", "ns3.google.com", "ns4.google.com")


def test_eu_indented_block_format() -> None:
    info = parse_whois(whois_fixture("europa.eu")).info
    assert info is not None
    assert info.registrar == "ClearMedia NV"
    assert info.privacy == "yes (NOT DISCLOSED!)"
    assert info.nameservers[:3] == ("ns2lux.europa.eu", "ans1.cw.net", "ns3bru.europa.eu")
