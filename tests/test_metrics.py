import json

from conftest import FIXTURES
from fakes import FakeHttp
from domain_i.http import NetworkError, Response
from domain_i.metrics import AHREFS_URL, TRANCO_URL, Metric, fetch_metrics

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
