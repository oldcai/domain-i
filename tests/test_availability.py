from collections.abc import Callable

import pytest

from domain_i.availability import (
    DEFAULT_TLDS,
    CheckResult,
    Row,
    check_domains,
    parse_tlds,
    resolve_tlds,
    run_availability,
)
from domain_i.rdap import RdapResult
from domain_i.spaceship import Availability, SpaceshipError


class FakeChecker:
    def __init__(self, statuses: dict[str, str], backend: str = "Fake") -> None:
        self.statuses = statuses
        self.backend = backend
        self.batches: list[list[str]] = []

    def __call__(self, domains: list[str]) -> CheckResult:
        self.batches.append(list(domains))
        return CheckResult([Row(d, self.statuses.get(d, "unknown"), None) for d in domains], self.backend)


ALL = ("com", "net", "org", "app")


def taken(name: str, tlds: tuple[str, ...] = ALL) -> dict[str, str]:
    return {f"{name}.{t}": "taken" for t in tlds}


def test_all_plain_taken_checks_hyphen_variants() -> None:
    checker = FakeChecker(taken("passwordmanager") | {"password-manager.com": "available"})
    report = run_availability(("password", "manager"), ALL, None, checker)
    assert checker.batches[0] == ["passwordmanager.com", "passwordmanager.net", "passwordmanager.org", "passwordmanager.app"]
    assert checker.batches[1] == ["password-manager.com", "password-manager.net", "password-manager.org", "password-manager.app"]
    assert [r.domain for r in report.rows][4] == "password-manager.com"
    assert report.hint is None


def test_one_available_stops_before_hyphen() -> None:
    statuses = taken("foobar") | {"foobar.app": "available"}
    checker = FakeChecker(statuses)
    report = run_availability(("foo", "bar"), ALL, None, checker)
    assert len(checker.batches) == 1
    assert len(report.rows) == 4


def test_unknown_stops_before_hyphen() -> None:
    statuses = taken("foobar")
    statuses["foobar.org"] = "unknown"
    checker = FakeChecker(statuses)
    report = run_availability(("foo", "bar"), ALL, None, checker)
    assert len(checker.batches) == 1
    assert [r.status for r in report.rows] == ["taken", "taken", "unknown", "taken"]


def test_unsplittable_word_gets_hint() -> None:
    checker = FakeChecker(taken("google"))
    report = run_availability(("google",), ALL, None, checker)
    assert len(checker.batches) == 1
    assert report.hint is not None and "space" in report.hint


def test_original_domain_is_first_row_and_not_duplicated() -> None:
    checker = FakeChecker({"foobar.io": "available"} | taken("foobar"))
    report = run_availability(("foo", "bar"), ALL, "foobar.io", checker)
    assert [r.domain for r in report.rows][:2] == ["foobar.io", "foobar.com"]
    report2 = run_availability(("foo", "bar"), ALL, "foobar.com", FakeChecker({}))
    assert [r.domain for r in report2.rows] == ["foobar.com", "foobar.net", "foobar.org", "foobar.app"]
    report3 = run_availability(("foo", "bar"), ALL, "foobar.app", FakeChecker({}))
    assert [r.domain for r in report3.rows] == ["foobar.app", "foobar.com", "foobar.net", "foobar.org"]


def test_original_domain_status_does_not_block_hyphen() -> None:
    checker = FakeChecker(taken("foobar") | {"foobar.io": "available"})
    run_availability(("foo", "bar"), ALL, "foobar.io", checker)
    assert len(checker.batches) == 2


def test_custom_tld_list_controls_hyphen_trigger() -> None:
    tlds = ("io", "dev")
    checker = FakeChecker(taken("foobar", tlds))
    report = run_availability(("foo", "bar"), tlds, None, checker)
    assert checker.batches == [["foobar.io", "foobar.dev"], ["foo-bar.io", "foo-bar.dev"]]
    assert len(report.rows) == 4
    checker2 = FakeChecker({"foobar.io": "taken", "foobar.dev": "available"})
    run_availability(("foo", "bar"), tlds, None, checker2)
    assert len(checker2.batches) == 1


# --- backend selection ---------------------------------------------------


def rdap_stub(states: dict[str, str]) -> Callable[[str], RdapResult]:
    def lookup(domain: str) -> RdapResult:
        return RdapResult(states.get(domain, "unknown"), None, None, None)

    return lookup


def test_spaceship_backend_used_when_available() -> None:
    def spaceship(domains: list[str]) -> dict[str, Availability]:
        return {d: Availability(d, "taken", None) for d in domains}

    def rdap(domain: str) -> RdapResult:
        raise AssertionError("RDAP must not be called")

    result = check_domains(["a.com", "a.net"], spaceship, rdap)
    assert result.backend == "Spaceship"
    assert [r.status for r in result.rows] == ["taken", "taken"]


def test_spaceship_failure_falls_back_to_rdap() -> None:
    def spaceship(domains: list[str]) -> dict[str, Availability]:
        raise SpaceshipError("HTTP 500")

    def rdap(domain: str) -> RdapResult:
        return RdapResult("unregistered" if domain == "a.app" else "registered", None, None, None)

    result = check_domains(["a.com", "a.app"], spaceship, rdap)
    assert [r.status for r in result.rows] == ["taken", "available"]
    assert result.backend.startswith("RDAP")
    assert "Spaceship failed" in result.backend and "HTTP 500" in result.backend


def test_no_credentials_uses_rdap() -> None:
    def rdap(domain: str) -> RdapResult:
        return RdapResult("unknown", None, None, "no RDAP server")

    result = check_domains(["a.zzz"], None, rdap)
    assert result.rows == [Row("a.zzz", "unknown", None)]
    assert result.backend.startswith("RDAP")


def test_spaceship_partial_answer_is_completed_with_rdap() -> None:
    def spaceship(domains: list[str]) -> dict[str, Availability]:
        return {"a.com": Availability("a.com", "available", "12.00 USD")}

    def rdap(domain: str) -> RdapResult:
        return RdapResult("registered", None, None, None)

    result = check_domains(["a.com", "a.de"], spaceship, rdap)
    assert result.rows == [Row("a.com", "available", "12.00 USD"), Row("a.de", "taken", None)]
    assert result.backend == "Spaceship + RDAP"


def test_premium_price_only_shown_for_available() -> None:
    def spaceship(domains: list[str]) -> dict[str, Availability]:
        return {
            "a.app": Availability("a.app", "taken", "827.17 USD"),
            "b.app": Availability("b.app", "available", "827.17 USD"),
        }

    result = check_domains(["a.app", "b.app"], spaceship, rdap_stub({}))
    assert result.rows == [Row("a.app", "taken", None), Row("b.app", "available", "827.17 USD")]


# --- TLD configuration --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("com,io,dev", ("com", "io", "dev")),
        (".COM .Io  dev", ("com", "io", "dev")),
        ("com, com ,net,.com", ("com", "net")),
        ("co.uk, .CO.UK,app", ("co.uk", "app")),
        ("com,inv@lid!,net", ("com", "net")),
    ],
)
def test_parse_tlds_is_lenient(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_tlds(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", ",,", "!!! ???"])
def test_parse_tlds_empty_or_invalid(raw: str) -> None:
    assert parse_tlds(raw) == ()


def test_resolve_tlds_priority() -> None:
    env = {"DOMAIN_I_TLDS": "io"}
    config = {"DOMAIN_I_TLDS": "dev"}
    assert resolve_tlds("ai", env, config) == (("ai",), None)
    assert resolve_tlds(None, env, config) == (("io",), None)
    assert resolve_tlds(None, {}, config) == (("dev",), None)
    assert resolve_tlds(None, {}, {}) == (DEFAULT_TLDS, None)
    assert DEFAULT_TLDS == ("com", "net", "org", "app")


def test_resolve_tlds_invalid_falls_back_with_warning() -> None:
    tlds, warning = resolve_tlds(None, {"DOMAIN_I_TLDS": "!!!"}, {"DOMAIN_I_TLDS": "dev"})
    assert tlds == DEFAULT_TLDS
    assert warning is not None and "DOMAIN_I_TLDS" in warning

