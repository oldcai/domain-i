from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from conftest import FIXTURES, rdap_fixture, whois_fixture
from fakes import FakeHttp
from domain_i.cli import Deps, main
from domain_i.http import NetworkError, Response
from domain_i.metrics import Metric
from domain_i.rdap import BOOTSTRAP_URL
from domain_i.spaceship import SPACESHIP_URL

TODAY = date(2026, 11, 26)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
VERISIGN = "https://rdap.verisign.com/com/v1/domain/"
GOOGLE_RDAP = "https://pubapi.registry.google/rdap/domain/"


def splitter(word: str) -> list[str]:
    return {"foobar": ["foo", "bar"], "passwordmanager": ["password", "manager"]}.get(word, [word])


@dataclass
class Harness:
    tmp_path: Path
    whois: dict[str, str] = field(default_factory=dict)
    http: FakeHttp = field(default_factory=FakeHttp)
    environ: dict[str, str] = field(default_factory=dict)
    isatty: bool = False
    metrics: Callable[[str], list[Metric]] | None = None
    whois_calls: list[str] = field(default_factory=list)
    stdout: io.StringIO = field(default_factory=io.StringIO)
    stderr: io.StringIO = field(default_factory=io.StringIO)

    def run(self, *args: str) -> int:
        def runner(argv: list[str], timeout: float) -> str:
            self.whois_calls.append(argv[-1])
            if argv[-1] not in self.whois:
                raise FileNotFoundError("whois")
            return self.whois[argv[-1]]

        deps = Deps(
            http=self.http,
            whois_runner=runner,
            environ=self.environ,
            config_dir=self.tmp_path / "config",
            cache_dir=self.tmp_path / "cache",
            today=TODAY,
            stdout=self.stdout,
            stderr=self.stderr,
            isatty=self.isatty,
            splitter=splitter,
            metrics=self.metrics,
        )
        return main(list(args), deps)

    @property
    def out(self) -> str:
        return self.stdout.getvalue()


def table_rows(out: str) -> list[str]:
    lines = ANSI.sub("", out).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Domain "))
    rows = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        rows.append(line)
    return rows


def no_spaceship_rdap(states: dict[str, int]) -> dict[str, Response | Exception]:
    routes: dict[str, Response | Exception] = {BOOTSTRAP_URL: Response(200, rdap_fixture("dns.json"))}
    for domain, code in states.items():
        tld = domain.rsplit(".", 1)[1]
        base = {
            "com": VERISIGN,
            "net": "https://rdap.verisign.com/net/v1/domain/",
            "org": "https://rdap.publicinterestregistry.org/rdap/domain/",
            "app": GOOGLE_RDAP,
            "dev": GOOGLE_RDAP,
        }[tld]
        routes[base + domain] = Response(code, b'{"objectClassName": "domain"}')
    return routes


def test_query_registered_domain_summary(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")})
    assert h.run("https://www.Google.com/") == 0
    days = (date(2028, 9, 14) - TODAY).days
    assert h.out == (
        "google.com  REGISTERED\n"
        "Created    1997-09-15\n"
        "Updated    2019-09-09\n"
        f"Expires    2028-09-14 ({days} days left)\n"
        "Privacy    unknown\n"
        "Registrar  MarkMonitor Inc.\n"
        "DNS        ns1.google.com, ns2.google.com, ns3.google.com, ns4.google.com\n"
    )
    assert h.http.calls == []


def test_verbose_prints_raw_whois_first(tmp_path: Path) -> None:
    raw = whois_fixture("google.com")
    h = Harness(tmp_path, whois={"google.com": raw})
    assert h.run("-v", "google.com") == 0
    assert h.out.startswith(raw)
    assert h.out.rstrip().endswith("ns4.google.com")


def test_missing_fields_show_dash(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.de": whois_fixture("google.de")})
    assert h.run("google.de") == 0
    assert "Created    -\n" in h.out
    assert "Registrar  -\n" in h.out
    assert "Expires    -\n" in h.out


def test_app_falls_back_to_rdap(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"cash.app": whois_fixture("cash.app")})
    h.http.routes[GOOGLE_RDAP + "cash.app"] = Response(200, rdap_fixture("cash.app.json"))
    assert h.run("cash.app") == 0
    assert h.out.startswith("cash.app  REGISTERED\n")
    assert "Registrar  CSC Corporate Domains, Inc.\n" in h.out
    assert "Created    2018-03-29\n" in h.out


def test_app_verbose_prints_rdap_json(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"cash.app": whois_fixture("cash.app")})
    h.http.routes[GOOGLE_RDAP + "cash.app"] = Response(200, rdap_fixture("cash.app.json"))
    assert h.run("--verbose", "cash.app") == 0
    assert '"ldhName": "cash.app"' in h.out


def test_unregistered_domain_enters_availability_mode(tmp_path: Path) -> None:
    name = "xq7zvkpl3qwmnotreal"
    h = Harness(tmp_path, whois={f"{name}.com": whois_fixture("unregistered.com")})
    h.http.routes.update(
        no_spaceship_rdap({f"{name}.com": 404, f"{name}.net": 404, f"{name}.org": 404, f"{name}.app": 404})
    )
    assert h.run(f"{name}.com") == 0
    lines = h.out.splitlines()
    assert lines[0] == f"{name}.com  NOT REGISTERED"
    assert "No match" in lines[1]
    table = table_rows(h.out)
    assert table[0].split()[:2] == [f"{name}.com", "available"]
    assert len(table) == 4  # .com not repeated; hyphen variants skipped because some are available


def test_unregistered_hyphenated_domain_keeps_original_first(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"foo-bar.com": "No match for domain \"FOO-BAR.COM\".\n"})
    states = {f"foobar.{t}": 200 for t in ("com", "net", "org", "app")}
    states |= {f"foo-bar.{t}": 404 for t in ("com", "net", "org", "app")}
    h.http.routes.update(no_spaceship_rdap(states))
    assert h.run("foo-bar.com") == 0
    domains = [line.split()[0] for line in table_rows(h.out)]
    assert domains == [
        "foo-bar.com", "foobar.com", "foobar.net", "foobar.org", "foobar.app",
        "foo-bar.net", "foo-bar.org", "foo-bar.app",
    ]
    assert "Backend: RDAP" in h.out


def test_no_record_anywhere_is_unknown_then_availability(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"foobar.zz": "% IANA WHOIS server\nrefer:\n\ndomain: ZZ\n"})
    h.http.routes.update(no_spaceship_rdap({"foobar.com": 200, "foobar.net": 200, "foobar.org": 200, "foobar.app": 404}))
    assert h.run("foobar.zz") == 0
    assert h.out.splitlines()[0] == "foobar.zz  UNKNOWN"
    assert "foobar.zz" in h.out.split("Domain", 1)[1]


def test_name_mode_with_spaceship(tmp_path: Path) -> None:
    h = Harness(tmp_path, environ={"SPACESHIP_API_KEY": "k", "SPACESHIP_API_SECRET": "s"})
    body = {
        "domains": [
            {"domain": "foobar.com", "result": "taken", "premiumPricing": []},
            {"domain": "foobar.net", "result": "taken", "premiumPricing": []},
            {"domain": "foobar.org", "result": "taken", "premiumPricing": []},
            {
                "domain": "foobar.app",
                "result": "available",
                "premiumPricing": [{"operation": "register", "price": 1200, "currency": "usd"}],
            },
        ]
    }
    h.http.routes[SPACESHIP_URL] = Response(200, json.dumps(body).encode())
    assert h.run("foobar") == 0
    assert h.out == (
        "Domain      Status     Price\n"
        "foobar.com  taken      -\n"
        "foobar.net  taken      -\n"
        "foobar.org  taken      -\n"
        "foobar.app  available  1200.00 USD\n"
        "\n"
        "Backend: Spaceship\n"
    )
    assert h.whois_calls == []


def test_name_mode_all_taken_then_hyphen(tmp_path: Path) -> None:
    states = {f"passwordmanager.{t}": 200 for t in ("com", "net", "org", "app")}
    states |= {f"password-manager.{t}": 404 for t in ("com", "net", "org", "app")}
    h = Harness(tmp_path)
    h.http.routes.update(no_spaceship_rdap(states))
    assert h.run("password", "manager") == 0
    assert "password-manager.app  available" in h.out
    assert "hyphen" in h.out.lower()


def test_unsplittable_all_taken_shows_hint(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.routes.update(no_spaceship_rdap({f"google.{t}": 200 for t in ("com", "net", "org", "app")}))
    assert h.run("google") == 0
    assert "No hyphenated variant" in h.out


def test_tld_flag_and_env(tmp_path: Path) -> None:
    h = Harness(tmp_path, environ={"DOMAIN_I_TLDS": "dev"})
    h.http.routes.update(no_spaceship_rdap({"foobar.dev": 404, "foobar.com": 200}))
    assert h.run("foobar") == 0
    assert "foobar.dev" in h.out and "foobar.com" not in h.out
    h2 = Harness(tmp_path, environ={"DOMAIN_I_TLDS": "dev"})
    h2.http.routes.update(no_spaceship_rdap({"foobar.dev": 404, "foobar.com": 200}))
    assert h2.run("--tlds", ".COM", "foobar") == 0
    assert "foobar.com" in h2.out and "foobar.dev" not in h2.out


def test_tlds_from_config_file(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env").write_text("DOMAIN_I_TLDS=dev\n")
    h = Harness(tmp_path)
    h.http.routes.update(no_spaceship_rdap({"foobar.dev": 404}))
    assert h.run("foobar") == 0
    assert "foobar.dev" in h.out


def test_invalid_tlds_warns_and_uses_default(tmp_path: Path) -> None:
    h = Harness(tmp_path, environ={"DOMAIN_I_TLDS": "!!!"})
    h.http.routes.update(no_spaceship_rdap({f"foobar.{t}": 404 for t in ("com", "net", "org", "app")}))
    assert h.run("foobar") == 0
    assert "DOMAIN_I_TLDS" in h.stderr.getvalue()
    assert "foobar.app" in h.out


def test_all_network_failed_exits_1(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.routes["*"] = NetworkError("offline")
    assert h.run("foobar") == 1
    assert "unknown" in h.out


@pytest.mark.parametrize("args", [[], ["!!!"], ["--bogus", "x"]])
def test_argument_errors_exit_2(tmp_path: Path, args: list[str]) -> None:
    h = Harness(tmp_path)
    with pytest.raises(SystemExit) as exc:
        h.run(*args)
    assert exc.value.code == 2


# --- colour ------------------------------------------------------------------


def _availability_harness(tmp_path: Path, **kw: object) -> Harness:
    h = Harness(tmp_path, **kw)  # type: ignore[arg-type]
    h.http.routes.update(no_spaceship_rdap({"foobar.com": 200, "foobar.net": 404, "foobar.org": 500, "foobar.app": 404}))
    return h


def test_no_colour_when_not_a_tty(tmp_path: Path) -> None:
    h = _availability_harness(tmp_path)
    h.run("foobar")
    assert not ANSI.search(h.out)


def test_colour_on_tty_and_no_color_respected(tmp_path: Path) -> None:
    h = _availability_harness(tmp_path, isatty=True)
    h.run("foobar")
    assert ANSI.search(h.out)
    h2 = _availability_harness(tmp_path, isatty=True, environ={"NO_COLOR": "1"})
    h2.run("foobar")
    assert not ANSI.search(h2.out)


@pytest.mark.parametrize(("environ", "flag"), [({"FORCE_COLOR": "1"}, None), ({}, "--color"), ({"NO_COLOR": "1"}, "--color")])
def test_forced_colour(tmp_path: Path, environ: dict[str, str], flag: str | None) -> None:
    h = _availability_harness(tmp_path, environ=environ)
    h.run(*([flag] if flag else []), "foobar")
    assert ANSI.search(h.out)


def test_each_status_has_its_own_colour_and_alignment_survives(tmp_path: Path) -> None:
    plain = _availability_harness(tmp_path)
    plain.run("foobar")
    coloured = _availability_harness(tmp_path, environ={"FORCE_COLOR": "1"})
    coloured.run("foobar")
    assert ANSI.sub("", coloured.out) == plain.out
    by_status = {}
    for line in coloured.out.splitlines():
        stripped = ANSI.sub("", line).split()
        if len(stripped) >= 2 and stripped[0].startswith("foobar."):
            by_status[stripped[1]] = ANSI.findall(line)[0]
    assert set(by_status) == {"taken", "available", "unknown"}
    assert len(set(by_status.values())) == 3


def test_query_header_colours_differ(tmp_path: Path) -> None:
    reg = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ={"FORCE_COLOR": "1"})
    reg.run("google.com")
    name = "xq7zvkpl3qwm-notreal"
    unreg = Harness(tmp_path, whois={f"{name}.com": whois_fixture("unregistered.com")}, environ={"FORCE_COLOR": "1"})
    unreg.http.routes["*"] = Response(404, b"")
    unreg.run(f"{name}.com")
    reg_code = ANSI.findall(reg.out.splitlines()[0])[0]
    unreg_code = ANSI.findall(unreg.out.splitlines()[0])[0]
    assert reg_code != unreg_code
    assert ANSI.sub("", reg.out.splitlines()[0]) == "google.com  REGISTERED"


# --- metrics -------------------------------------------------------------------


def test_metrics_shown_for_registered_domain(tmp_path: Path) -> None:
    def metrics(domain: str) -> list[Metric]:
        return [Metric("Traffic", "1.2M/mo", "similarweb"), Metric("Rank", "#1", "tranco")]

    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, metrics=metrics)
    assert h.run("google.com") == 0
    assert "Traffic    1.2M/mo (similarweb)\n" in h.out
    assert "Rank       #1 (tranco)\n" in h.out
    forced = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, metrics=metrics, environ={"FORCE_COLOR": "1"})
    forced.run("google.com")
    traffic = next(line for line in forced.out.splitlines() if "Traffic" in line)
    assert ANSI.search(traffic)


def test_metrics_failure_is_silent(tmp_path: Path) -> None:
    def metrics(domain: str) -> list[Metric]:
        raise RuntimeError("boom")

    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, metrics=metrics)
    assert h.run("google.com") == 0
    assert "Traffic" not in h.out
    assert "boom" not in h.out


def test_no_metrics_flag_skips_fetch(tmp_path: Path) -> None:
    called: list[str] = []

    def metrics(domain: str) -> list[Metric]:
        called.append(domain)
        return [Metric("Traffic", "1/mo", "x")]

    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, metrics=metrics)
    assert h.run("--no-metrics", "google.com") == 0
    assert called == []
    assert "Traffic" not in h.out


def test_fixture_dir_exists() -> None:
    assert (FIXTURES / "whois").is_dir()
