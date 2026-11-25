from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def whois_fixture(name: str) -> str:
    return (FIXTURES / "whois" / f"{name}.txt").read_text(encoding="utf-8")


def rdap_fixture(name: str) -> bytes:
    return (FIXTURES / "rdap" / name).read_bytes()


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guard: nothing under test may touch the real home, cache or credentials."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("SPACESHIP_API_KEY", "SPACESHIP_API_SECRET", "DOMAIN_I_ENV", "NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
