import json
from pathlib import Path

import pytest

from conftest import FIXTURES
from fakes import FakeHttp
from domain_i.http import NetworkError, Response
from domain_i.spaceship import (
    SPACESHIP_URL,
    Credentials,
    SpaceshipError,
    check,
    load_credentials,
    parse_env_file,
)

BULK = (FIXTURES / "spaceship_bulk.json").read_bytes()
CREDS = Credentials("test-key", "test-secret")


def test_parse_env_file_handles_export_quotes_and_comments() -> None:
    text = "# comment\nexport SPACESHIP_API_KEY='k1'\nSPACESHIP_API_SECRET=\"s1\"\n\nOTHER = x \nbroken line\n"
    assert parse_env_file(text) == {"SPACESHIP_API_KEY": "k1", "SPACESHIP_API_SECRET": "s1", "OTHER": "x"}


def test_credentials_from_environment(tmp_path: Path) -> None:
    env = {"SPACESHIP_API_KEY": "k", "SPACESHIP_API_SECRET": "s"}
    assert load_credentials(env, config_dir=tmp_path) == Credentials("k", "s")


def test_credentials_from_domain_i_env_file(tmp_path: Path) -> None:
    f = tmp_path / "creds.env"
    f.write_text("SPACESHIP_API_KEY=k2\nSPACESHIP_API_SECRET=s2\n")
    assert load_credentials({"DOMAIN_I_ENV": str(f)}, config_dir=tmp_path / "cfg") == Credentials("k2", "s2")


def test_credentials_from_default_config_file(tmp_path: Path) -> None:
    (tmp_path / "env").write_text("export SPACESHIP_API_KEY=k3\nexport SPACESHIP_API_SECRET=s3\n")
    assert load_credentials({}, config_dir=tmp_path) == Credentials("k3", "s3")


def test_explicit_domain_i_env_overrides_default_file(tmp_path: Path) -> None:
    (tmp_path / "env").write_text("SPACESHIP_API_KEY=k3\nSPACESHIP_API_SECRET=s3\n")
    assert load_credentials({"DOMAIN_I_ENV": "/dev/null"}, config_dir=tmp_path) is None


def test_partial_environment_falls_through(tmp_path: Path) -> None:
    (tmp_path / "env").write_text("SPACESHIP_API_KEY=k3\nSPACESHIP_API_SECRET=s3\n")
    assert load_credentials({"SPACESHIP_API_SECRET": "s"}, config_dir=tmp_path) == Credentials("k3", "s3")


def test_no_credentials(tmp_path: Path) -> None:
    assert load_credentials({}, config_dir=tmp_path) is None
    assert load_credentials({"DOMAIN_I_ENV": str(tmp_path / "missing")}, config_dir=tmp_path) is None


def test_check_parses_status_and_premium_price() -> None:
    http = FakeHttp({SPACESHIP_URL: Response(200, BULK)})
    results = check(http, CREDS, ["google.com", "xq7zvkpl3qwm.com", "cash.app", "xq7zvkpl3qwm.app"])
    assert results["google.com"].status == "taken"
    assert results["xq7zvkpl3qwm.com"].status == "available"
    assert results["xq7zvkpl3qwm.com"].premium is None
    assert results["cash.app"].premium == "827.17 USD"
    call = http.calls[0]
    assert call.method == "POST"
    assert call.headers["X-Api-Key"] == "test-key"
    assert call.headers["X-Api-Secret"] == "test-secret"
    assert call.body is not None
    assert json.loads(call.body) == {"domains": ["google.com", "xq7zvkpl3qwm.com", "cash.app", "xq7zvkpl3qwm.app"]}


def test_check_unknown_result_value() -> None:
    body = json.dumps({"domains": [{"domain": "a.com", "result": "weird", "premiumPricing": []}]}).encode()
    results = check(FakeHttp({SPACESHIP_URL: Response(200, body)}), CREDS, ["a.com"])
    assert results["a.com"].status == "unknown"


@pytest.mark.parametrize(
    "response",
    [Response(401, b'{"detail":"unauthorized"}'), Response(200, b"not json"), Response(200, b'{"x":1}'), NetworkError("down")],
)
def test_check_failures_raise(response: Response | Exception) -> None:
    with pytest.raises(SpaceshipError):
        check(FakeHttp({SPACESHIP_URL: response}), CREDS, ["a.com"])


def test_error_message_never_contains_secret() -> None:
    with pytest.raises(SpaceshipError) as exc:
        check(FakeHttp({SPACESHIP_URL: Response(401, b"bad test-secret")}), CREDS, ["a.com"])
    assert "test-secret" not in str(exc.value)
