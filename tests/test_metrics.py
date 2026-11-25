import json
import subprocess

import pytest

from conftest import FIXTURES
from fakes import FakeHttp
from domain_i.http import NetworkError, Response
from domain_i.metrics import (
    AHREFS_URL,
    TRANCO_URL,
    Metric,
    abbreviate,
    fetch_metrics,
    combine_command_metrics,
    merge_metrics,
    metrics_commands,
    parse_command_output,
    run_metrics_command,
)

TRANCO = FIXTURES / "metrics"


def tranco(name: str) -> bytes:
    return (TRANCO / f"tranco_{name}.json").read_bytes()


def test_tranco_rank_without_ahrefs_key() -> None:
    http = FakeHttp({TRANCO_URL + "github.com": Response(200, tranco("github.com"))})
    assert fetch_metrics(http, "github.com", {}) == [Metric("Rank", "#34", "tranco")]
    assert len(http.calls) == 1
    assert http.calls[0].timeout <= 8


def test_tranco_large_rank_is_formatted() -> None:
    http = FakeHttp({TRANCO_URL + "lobste.rs": Response(200, tranco("lobste.rs"))})
    assert fetch_metrics(http, "lobste.rs", {}) == [Metric("Rank", "#35,923", "tranco")]


def test_tranco_unlisted_domain_gives_no_row() -> None:
    http = FakeHttp({TRANCO_URL + "x.com": Response(200, tranco("no-such-domain-xyz-91827.com"))})
    assert fetch_metrics(http, "x.com", {}) == []


def test_tranco_rate_limited_or_down_gives_no_row() -> None:
    assert fetch_metrics(FakeHttp({TRANCO_URL + "a.com": Response(429, b"<html>")}), "a.com", {}) == []
    assert fetch_metrics(FakeHttp({TRANCO_URL + "a.com": NetworkError("t/o")}), "a.com", {}) == []


def _ahrefs(dr: float) -> bytes:
    # Shape from the Ahrefs API reference for /v3/public/domain-rating-free (not a recorded sample).
    return json.dumps({"domain_rating": {"domain_rating": dr, "license": "http://ahrefs.com/legal/domain-rating-license"}}).encode()


def test_ahrefs_dr_with_key() -> None:
    http = FakeHttp(
        {
            TRANCO_URL + "github.com": Response(200, tranco("github.com")),
            AHREFS_URL + "github.com": Response(200, _ahrefs(96.0)),
        }
    )
    result = fetch_metrics(http, "github.com", {"AHREFS_API_KEY": "test-key"})
    assert result == [Metric("Rank", "#34", "tranco"), Metric("DR", "96", "ahrefs")]
    ahrefs_call = next(c for c in http.calls if c.url.startswith(AHREFS_URL))
    assert ahrefs_call.headers["Authorization"] == "Bearer test-key"


def test_ahrefs_zero_or_error_gives_no_row() -> None:
    http = FakeHttp({TRANCO_URL + "a.com": Response(200, b'{"ranks": []}'), AHREFS_URL + "a.com": Response(200, _ahrefs(0))})
    assert fetch_metrics(http, "a.com", {"AHREFS_API_KEY": "k"}) == []
    http2 = FakeHttp({TRANCO_URL + "a.com": Response(200, b'{"ranks": []}'), AHREFS_URL + "a.com": Response(403, b'["Error","Forbidden"]')})
    assert fetch_metrics(http2, "a.com", {"AHREFS_API_KEY": "k"}) == []


def test_tranco_zero_rank_gives_no_row() -> None:
    http = FakeHttp({TRANCO_URL + "a.com": Response(200, b'{"ranks": [{"date": "2026-11-24", "rank": 0}]}')})
    assert fetch_metrics(http, "a.com", {}) == []


# --- external metrics command (DOMAIN_I_METRICS_CMD) ---------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "7"),
        (999, "999"),
        (999.6, "1.0K"),
        (1000, "1.0K"),
        (1_250_000, "1.3M"),
        (999_950, "1.0M"),
        (745_628_929, "745.6M"),
        (2_300_000_000, "2.3B"),
    ],
)
def test_abbreviate(value: float, expected: str) -> None:
    assert abbreviate(value) == expected


def test_parse_full_output() -> None:
    text = '{"traffic": 745628929, "traffic_month": "2026-10", "dr": 96.4, "source": "acme", "extra": [1]}'
    metrics, error = parse_command_output(text)
    assert error is None
    assert metrics == [Metric("Traffic", "745.6M/mo", "acme, 2026-10"), Metric("DR", "96", "acme")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"traffic": 1200}', [Metric("Traffic", "1.2K/mo", "custom")]),
        ('{"traffic": 1200, "source": ""}', [Metric("Traffic", "1.2K/mo", "custom")]),
        ('{"dr": 12, "source": "x"}', [Metric("DR", "12", "x")]),
        ('{"traffic_month": "2026-10"}', []),
        ("{}", []),
        ('{"traffic": 0, "dr": 0}', []),
        ('{"traffic": "lots", "dr": true}', []),
        ('{"traffic": -5, "dr": 101}', []),
    ],
)
def test_parse_partial_or_invalid_values(text: str, expected: list[Metric]) -> None:
    assert parse_command_output(text)[0] == expected


@pytest.mark.parametrize("text", ["not json", "[1, 2]", "", '"str"'])
def test_parse_bad_output_reports_error(text: str) -> None:
    metrics, error = parse_command_output(text)
    assert metrics == []
    assert error is not None


def test_run_command_appends_domain_and_expands_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    seen: list[tuple[list[str], float]] = []

    def runner(args: list[str], timeout: float) -> str:
        seen.append((args, timeout))
        return '{"traffic": 5000, "source": "demo"}'

    metrics, error = run_metrics_command("python3 ~/bin/metrics.py --flag 'two words'", "example.com", runner)
    assert seen == [(["python3", "/home/someone/bin/metrics.py", "--flag", "two words", "example.com"], 8.0)]
    assert metrics == [Metric("Traffic", "5.0K/mo", "demo")]
    assert error is None


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (subprocess.TimeoutExpired(["x"], 8.0), "timed out"),
        (subprocess.CalledProcessError(3, ["x"]), "exit status 3"),
        (FileNotFoundError(2, "No such file or directory"), "No such file"),
    ],
)
def test_run_command_failures(exc: Exception, fragment: str) -> None:
    def runner(args: list[str], timeout: float) -> str:
        raise exc

    metrics, error = run_metrics_command("missing-tool", "example.com", runner)
    assert metrics == []
    assert error is not None and fragment in error


def test_run_command_unbalanced_quotes() -> None:
    def runner(args: list[str], timeout: float) -> str:
        raise AssertionError("must not run")

    metrics, error = run_metrics_command("tool 'oops", "example.com", runner)
    assert metrics == [] and error is not None


def test_merge_order_and_dr_preference() -> None:
    custom = [Metric("Traffic", "1.0K/mo", "c"), Metric("DR", "50", "c")]
    builtin = [Metric("Rank", "#1", "tranco"), Metric("DR", "90", "ahrefs")]
    assert merge_metrics(custom, builtin) == [
        Metric("Traffic", "1.0K/mo", "c"),
        Metric("DR", "50", "c"),
        Metric("Rank", "#1", "tranco"),
    ]
    assert merge_metrics([], builtin) == [Metric("DR", "90", "ahrefs"), Metric("Rank", "#1", "tranco")]


def test_metrics_commands_discovery_order_and_precedence() -> None:
    environ = {"DOMAIN_I_METRICS_CMD_Z": "z", "DOMAIN_I_METRICS_CMD": "base", "DOMAIN_I_METRICS_CMDX": "ignored"}
    config = {"DOMAIN_I_METRICS_CMD_A": "a", "DOMAIN_I_METRICS_CMD_Z": "from-file", "OTHER": "x"}
    assert metrics_commands(environ, config) == [
        ("DOMAIN_I_METRICS_CMD", "base"),
        ("DOMAIN_I_METRICS_CMD_A", "a"),
        ("DOMAIN_I_METRICS_CMD_Z", "z"),
    ]
    assert metrics_commands({"DOMAIN_I_METRICS_CMD_A": "  "}, config) == [("DOMAIN_I_METRICS_CMD_Z", "from-file")]
    assert metrics_commands({}, {}) == []


def test_combine_command_metrics_first_value_per_field_wins() -> None:
    first = [Metric("Traffic", "1.0K/mo", "a")]
    second = [Metric("Traffic", "9.0K/mo", "b"), Metric("DR", "50", "b")]
    assert combine_command_metrics([first, [], second]) == [Metric("Traffic", "1.0K/mo", "a"), Metric("DR", "50", "b")]
