from __future__ import annotations

import io
import json
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest
from conftest import FIXTURES, rdap_fixture, whois_fixture
from fakes import Call, FakeHttp

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
    command_output: str | Exception = "{}"
    command_outputs: dict[str, str | Exception] = field(default_factory=dict)  # by argv[0]
    command_calls: list[list[str]] = field(default_factory=list)
    whois_calls: list[str] = field(default_factory=list)
    whois_override: Callable[[list[str], float], str] | None = None
    stdout: io.StringIO = field(default_factory=io.StringIO)
    stderr: io.StringIO = field(default_factory=io.StringIO)

    def run(self, *args: str) -> int:
        def runner(argv: list[str], timeout: float) -> str:
            self.whois_calls.append(argv[-1])
            if self.whois_override is not None:
                return self.whois_override(argv, timeout)
            if argv[-1] not in self.whois:
                raise FileNotFoundError("whois")
            return self.whois[argv[-1]]

        def command_runner(argv: list[str], timeout: float) -> str:
            self.command_calls.append(argv)
            output = self.command_outputs.get(argv[0], self.command_output)
            if isinstance(output, Exception):
                raise output
            return output

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
            command_runner=command_runner,
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
        if not line.strip() or line.startswith("Taken:"):
            break
        rows.append(line)
    return rows


def summary_lines(out: str) -> list[str]:
    """The lookup summary: everything before the first blank line."""
    lines = out.splitlines()
    return lines[: lines.index("")] if "" in lines else lines


def spaceship_answers(statuses: dict[str, str], default: str = "taken") -> Callable[[Call], Response]:
    """A Spaceship route that answers for whatever domains were asked."""

    def answer(call: Call) -> Response:
        asked = json.loads(call.body or b"{}")["domains"]
        items = [{"domain": d, "result": statuses.get(d, default), "premiumPricing": []} for d in asked]
        return Response(200, json.dumps({"domains": items}).encode())

    return answer


CREDS = {"SPACESHIP_API_KEY": "k", "SPACESHIP_API_SECRET": "s"}


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
    h.http.routes[VERISIGN + "google.com"] = Response(200, rdap_fixture("google.com.json"))
    h.http.routes["https://rdap.markmonitor.com/rdap/domain/GOOGLE.COM"] = Response(
        200, rdap_fixture("registrar_google.com.json")
    )
    assert h.run("https://www.Google.com/") == 0
    days = (date(2028, 9, 14) - TODAY).days
    assert summary_lines(h.out) == [
        "google.com  REGISTERED",
        "Created    1997-09-15",
        "Updated    2019-09-09",
        f"Expires    2028-09-14 ({days} days left)",
        "Privacy    yes (REDACTED FOR PRIVACY)",
        "Registrar  MarkMonitor Inc.",
        "DNS        ns1.google.com, ns2.google.com, ns3.google.com, ns4.google.com",
    ]


def test_privacy_lookup_failure_keeps_unknown(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")})
    h.http.routes["*"] = NetworkError("offline")
    assert h.run("google.com") == 0
    assert "Privacy    unknown\n" in h.out


def test_privacy_lookup_skipped_when_whois_decides(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.io": whois_fixture("google.io")}, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({})
    assert h.run("google.io") == 0
    assert "Privacy    no (Google LLC)\n" in h.out
    assert h.http.urls() == [SPACESHIP_URL]


def test_app_privacy_follows_registrar_link_from_registry_rdap(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"cash.app": whois_fixture("cash.app")}, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({})
    h.http.routes[GOOGLE_RDAP + "cash.app"] = Response(200, rdap_fixture("cash.app.json"))
    h.http.routes["https://rdap.cscglobal.com/dbs/rdap-api/v1/domain/cash.app"] = Response(
        200, rdap_fixture("registrar_spaceship.com.json")
    )
    assert h.run("cash.app") == 0
    assert "Privacy    yes (Privacy service provided by WITHHELD FOR PRIVACY LLC)\n" in h.out
    assert h.http.urls().count(GOOGLE_RDAP + "cash.app") == 1


def test_verbose_prints_raw_whois_first(tmp_path: Path) -> None:
    raw = whois_fixture("google.com")
    h = Harness(tmp_path, whois={"google.com": raw})
    assert h.run("-v", "google.com") == 0
    assert h.out.startswith(raw)
    assert summary_lines(h.out[len(raw) :])[-1].endswith("ns4.google.com")


def test_missing_fields_are_hidden_but_privacy_stays(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.de": whois_fixture("google.de")})
    assert h.run("google.de") == 0
    assert summary_lines(h.out) == [
        "google.de  REGISTERED",
        "Updated    2018-03-12",
        "Privacy    unknown",
        "DNS        ns1.google.com, ns2.google.com, ns3.google.com, ns4.google.com",
    ]


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
    h = Harness(tmp_path, environ=dict(CREDS))
    premium = {"foobar.app": [{"operation": "register", "price": 1200, "currency": "usd"}]}
    statuses = {"foobar.com": "taken", "foobar.net": "taken", "foobar.org": "taken", "foobar.app": "available"}

    def answer(call: Call) -> Response:
        asked = json.loads(call.body or b"{}")["domains"]
        items = [
            {"domain": d, "result": statuses.get(d, "available"), "premiumPricing": premium.get(d, [])} for d in asked
        ]
        return Response(200, json.dumps({"domains": items}).encode())

    h.http.routes[SPACESHIP_URL] = answer
    assert h.run("foobar") == 0
    assert h.out == (
        "Domain       Status     Price\n"
        "foobar.com   taken      -\n"
        "foobar.net   taken      -\n"
        "foobar.org   taken      -\n"
        "foobar.app   available  1200.00 USD\n"
        "foo-bar.com  available  -\n"
        "Taken: 3/4\n"
        "\n"
        "foobar.com is taken, so foo-bar.com was checked too.\n"
        "Backend: Spaceship\n"
    )
    assert h.whois_calls == []
    assert h.command_calls == []


def test_price_column_hidden_without_premium(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.http.routes.update(no_spaceship_rdap({"foobar.com": 200, "foobar.net": 404, "foobar.org": 404, "foobar.app": 404}))
    h.http.routes[VERISIGN + "foo-bar.com"] = Response(404, b"")
    assert h.run("foobar") == 0
    assert h.out.splitlines()[:3] == [
        "Domain       Status",
        "foobar.com   taken",
        "foobar.net   available",
    ]


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
    (tmp_path / "config" / "config.env").write_text("DOMAIN_I_TLDS=dev\n")
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
    h.http.routes[VERISIGN + "foo-bar.com"] = Response(404, b"")
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
        if ANSI.match(line) and len(stripped) >= 2 and stripped[0].startswith(("foobar.", "foo-bar.")):
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


# --- DOMAIN_I_METRICS_CMD -----------------------------------------------------------


def _builtin(domain: str) -> list[Metric]:
    return [Metric("DR", "90", "ahrefs"), Metric("Rank", "#34", "tranco")]


def _registered(tmp_path: Path, **kw: object) -> Harness:
    h = Harness(tmp_path, whois={"google.io": whois_fixture("google.io")}, **kw)  # type: ignore[arg-type]
    return h


def test_metrics_command_rows_order_and_dr_preference(tmp_path: Path) -> None:
    h = _registered(
        tmp_path,
        environ={"DOMAIN_I_METRICS_CMD": "my-metrics --fast"},
        metrics=_builtin,
        command_output='{"traffic": 745628929, "traffic_month": "2026-10", "dr": 91, "source": "demo"}',
    )
    assert h.run("google.io") == 0
    assert h.command_calls == [["my-metrics", "--fast", "google.io"]]
    assert summary_lines(h.out)[-3:] == [
        "Traffic    745.6M/mo (demo, 2026-10)",
        "DR         91 (demo)",
        "Rank       #34 (tranco)",
    ]


def test_metrics_command_without_dr_keeps_builtin_dr(tmp_path: Path) -> None:
    h = _registered(
        tmp_path, environ={"DOMAIN_I_METRICS_CMD": "m"}, metrics=_builtin, command_output='{"traffic": 999}'
    )
    h.run("google.io")
    assert summary_lines(h.out)[-3:] == ["Traffic    999/mo (custom)", "DR         90 (ahrefs)", "Rank       #34 (tranco)"]


def test_metrics_command_from_config_file(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.env").write_text("DOMAIN_I_METRICS_CMD=from-file\n")
    h = _registered(tmp_path, command_output='{"traffic": 1000}')
    h.run("google.io")
    assert h.command_calls == [["from-file", "google.io"]]
    assert "Traffic    1.0K/mo (custom)\n" in h.out


def test_env_metrics_command_overrides_config_file(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.env").write_text("DOMAIN_I_METRICS_CMD=from-file\n")
    h = _registered(tmp_path, environ={"DOMAIN_I_METRICS_CMD": "from-env"})
    h.run("google.io")
    assert h.command_calls == [["from-env", "google.io"]]


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        '{"traffic": 0}',
        subprocess.TimeoutExpired(["m"], 8.0),
        subprocess.CalledProcessError(1, ["m"]),
        FileNotFoundError(2, "No such file or directory"),
    ],
)
def test_metrics_command_failures_are_silent(tmp_path: Path, output: str | Exception) -> None:
    h = _registered(tmp_path, environ={"DOMAIN_I_METRICS_CMD": "m"}, command_output=output)
    assert h.run("google.io") == 0
    assert "Traffic" not in h.out
    assert h.stderr.getvalue() == ""


def test_metrics_command_failure_reason_on_stderr_with_verbose(tmp_path: Path) -> None:
    h = _registered(
        tmp_path, environ={"DOMAIN_I_METRICS_CMD": "m"}, command_output=subprocess.CalledProcessError(3, ["m"])
    )
    assert h.run("-v", "google.io") == 0
    assert "metrics command" in h.stderr.getvalue()
    assert "exit status 3" in h.stderr.getvalue()


def test_metrics_command_not_configured_or_disabled(tmp_path: Path) -> None:
    h = _registered(tmp_path)
    h.run("google.io")
    assert h.command_calls == []
    h2 = _registered(tmp_path, environ={"DOMAIN_I_METRICS_CMD": "m"}, command_output='{"traffic": 5}')
    h2.run("--no-metrics", "google.io")
    assert h2.command_calls == []
    assert "Traffic" not in h2.out


def test_metrics_command_not_run_for_unregistered_domain(tmp_path: Path) -> None:
    name = "xq7zvkpl3qwmnotreal"
    h = Harness(
        tmp_path,
        whois={f"{name}.com": whois_fixture("unregistered.com")},
        environ={"DOMAIN_I_METRICS_CMD": "m"},
    )
    h.http.routes["*"] = Response(404, b"")
    h.run(f"{name}.com")
    assert h.command_calls == []


def test_traffic_and_dr_have_distinct_colours(tmp_path: Path) -> None:
    h = _registered(
        tmp_path,
        environ={"DOMAIN_I_METRICS_CMD": "m", "FORCE_COLOR": "1"},
        metrics=_builtin,
        command_output='{"traffic": 5000, "dr": 40}',
    )
    h.run("google.io")
    codes = {ANSI.sub("", line).split()[0]: ANSI.findall(line)[0] for line in summary_lines(h.out)[-3:]}
    assert len({codes["Traffic"], codes["DR"], codes["Rank"]}) == 3


# --- other TLDs after a lookup ---------------------------------------------------------


def test_registered_domain_row_stays_in_place_with_marker(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS))
    # The backend disagrees about google.com; the row must follow the lookup.
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.com": "available", "google.org": "available"})
    assert h.run("--no-metrics", "--tlds", "net,com,org", "google.com") == 0
    lines = ANSI.sub("", h.out).splitlines()
    assert lines[lines.index("") + 1].startswith("Domain")
    assert table_rows(h.out) == [
        "google.net  taken",
        "google.com  taken      \u2190 this",
        "google.org  available",
    ]
    assert "Taken: 2/3" in h.out


def test_registered_domain_outside_tld_list_is_first_row(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.io": whois_fixture("google.io")}, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    assert h.run("google.io") == 0
    rows = table_rows(h.out)
    assert [r.split()[0] for r in rows] == ["google.io", "google.com", "google.net", "google.org", "google.app"]
    assert rows[0].endswith("\u2190 this")
    assert "Taken: 3/4\n" in h.out  # the queried .io is not one of the configured names


def test_registered_and_all_others_taken_checks_hyphen(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"foobar.com": whois_fixture("google.com")}, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"foo-bar.net": "available"})
    assert h.run("foobar.com") == 0
    domains = [r.split()[0] for r in table_rows(h.out)]
    assert domains[4:] == ["foo-bar.com", "foo-bar.net", "foo-bar.org", "foo-bar.app"]
    assert "Taken: 4/4\n" in h.out
    assert "hyphenated variants were checked" in h.out


def test_unknown_lookup_still_marks_row_and_uses_backend_status(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"foobar.zz": "% IANA WHOIS server\nrefer:\n\ndomain: ZZ\n"}, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"foobar.zz": "unknown", "foobar.app": "available"})
    h.http.routes[BOOTSTRAP_URL] = Response(200, rdap_fixture("dns.json"))
    assert h.run("foobar.zz") == 0
    assert h.out.splitlines()[0] == "foobar.zz  UNKNOWN"
    rows = table_rows(h.out)
    assert rows[0].split()[:2] == ["foobar.zz", "unknown"] and rows[0].endswith("\u2190 this")


def test_table_failure_keeps_summary_and_exit_code(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")})
    h.http.routes["*"] = NetworkError("offline")
    assert h.run("google.com") == 0
    assert summary_lines(h.out)[0] == "google.com  REGISTERED"
    assert "Domain " not in h.out
    assert "Other TLDs could not be checked" in h.out


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"com": 200, "net": 200, "org": 200, "app": 200}, "Taken: 4/4"),
        ({"com": 200, "net": 200, "org": 200, "app": 404}, "Taken: 3/4"),
        ({"com": 200, "net": 200, "org": 500, "app": 404}, "Taken: 2/4 (1 unknown)"),
    ],
)
def test_taken_summary_line(tmp_path: Path, states: dict[str, int], expected: str) -> None:
    h = Harness(tmp_path)
    h.http.routes.update(no_spaceship_rdap({f"foobar.{t}": code for t, code in states.items()}))
    h.http.routes["https://rdap.verisign.com/com/v1/domain/foo-bar.com"] = Response(404, b"")
    h.http.routes["*"] = Response(404, b"")
    h.run("foobar")
    assert expected + "\n" in h.out


def test_availability_check_starts_while_whois_runs(tmp_path: Path) -> None:
    started = threading.Event()
    h = Harness(tmp_path, environ=dict(CREDS))

    def answer(call: Call) -> Response:
        started.set()
        return spaceship_answers({})(call)

    h.http.routes[SPACESHIP_URL] = answer
    seen_during_whois: list[bool] = []

    def slow_whois(argv: list[str], timeout: float) -> str:
        seen_during_whois.append(started.wait(timeout=2.0))
        return whois_fixture("google.com")

    h.whois_override = slow_whois
    assert h.run("--no-metrics", "google.com") == 0
    assert seen_during_whois == [True]


# --- several metrics commands ------------------------------------------------------------


def test_two_commands_each_contribute_a_field(tmp_path: Path) -> None:
    h = _registered(
        tmp_path,
        environ={"DOMAIN_I_METRICS_CMD_TRAFFIC": "traffic-tool", "DOMAIN_I_METRICS_CMD_DR": "dr-tool --x"},
        metrics=_builtin,
        command_outputs={
            "traffic-tool": '{"traffic": 745628929, "traffic_month": "2026-10", "source": "t"}',
            "dr-tool": '{"dr": 97, "source": "ahrefs"}',
        },
    )
    assert h.run("google.io") == 0
    assert sorted(h.command_calls) == [["dr-tool", "--x", "google.io"], ["traffic-tool", "google.io"]]
    assert summary_lines(h.out)[-3:] == [
        "Traffic    745.6M/mo (t, 2026-10)",
        "DR         97 (ahrefs)",
        "Rank       #34 (tranco)",
    ]


def test_first_command_in_name_order_wins_for_the_same_field(tmp_path: Path) -> None:
    h = _registered(
        tmp_path,
        environ={
            "DOMAIN_I_METRICS_CMD_B": "b",
            "DOMAIN_I_METRICS_CMD": "plain",
            "DOMAIN_I_METRICS_CMD_A": "a",
        },
        command_outputs={
            "plain": '{"traffic": 0}',  # zero -> no value, falls through
            "a": '{"traffic": 2000, "dr": 10, "source": "a"}',
            "b": '{"traffic": 5000, "dr": 50, "source": "b"}',
        },
    )
    h.run("google.io")
    assert "Traffic    2.0K/mo (a)\n" in h.out
    assert "DR         10 (a)\n" in h.out


def test_one_failing_command_does_not_hide_the_other(tmp_path: Path) -> None:
    h = _registered(
        tmp_path,
        environ={"DOMAIN_I_METRICS_CMD": "broken", "DOMAIN_I_METRICS_CMD_DR": "dr-tool"},
        command_outputs={"broken": subprocess.TimeoutExpired(["broken"], 8.0), "dr-tool": '{"dr": 20, "source": "x"}'},
    )
    assert h.run("-v", "google.io") == 0
    assert "DR         20 (x)\n" in h.out
    assert "Traffic" not in h.out
    assert "DOMAIN_I_METRICS_CMD failed: timed out" in h.stderr.getvalue()


def test_env_overrides_same_named_command_and_empty_disables(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.env").write_text(
        "DOMAIN_I_METRICS_CMD_TRAFFIC=file-traffic\nDOMAIN_I_METRICS_CMD_DR=file-dr\n"
    )
    h = _registered(tmp_path, environ={"DOMAIN_I_METRICS_CMD_DR": "env-dr", "DOMAIN_I_METRICS_CMD_TRAFFIC": ""})
    h.run("google.io")
    assert h.command_calls == [["env-dr", "google.io"]]


def test_no_metrics_skips_every_command(tmp_path: Path) -> None:
    h = _registered(tmp_path, environ={"DOMAIN_I_METRICS_CMD": "a", "DOMAIN_I_METRICS_CMD_DR": "b"})
    h.run("--no-metrics", "google.io")
    assert h.command_calls == []


def test_custom_tld_list_primary_hyphen_in_cli(tmp_path: Path) -> None:
    h = Harness(tmp_path, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"foobar.io": "taken"}, default="available")
    assert h.run("--tlds", "io,com", "foobar") == 0
    assert [r.split()[0] for r in table_rows(h.out)] == ["foobar.io", "foobar.com", "foo-bar.io"]
    assert "foobar.io is taken, so foo-bar.io was checked too.\n" in h.out


def test_unsplittable_name_with_taken_primary_gets_hint(tmp_path: Path) -> None:
    h = Harness(tmp_path, environ=dict(CREDS))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    assert h.run("google") == 0
    assert "No hyphenated variant" in h.out
    assert "was checked too" not in h.out


CREDS2 = dict(CREDS) | {"DOMAIN_I_METRICS_CMD_TRAFFIC": "traffic-tool", "DOMAIN_I_METRICS_CMD_DR": "dr-tool --q"}
TRAFFIC_OUT = '{"traffic": 123456, "source": "t"}'
DR_OUT = '{"dr": 42, "source": "d"}'


def test_table_placeholder_then_final_columns(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    h.command_outputs = {"traffic-tool": TRAFFIC_OUT, "dr-tool": DR_OUT}
    assert h.run("google.com") == 0
    lines = ANSI.sub("", h.out).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Domain"))
    table = lines[start:]
    assert table[0].split()[:5] == ["Domain", "Status", "Traffic", "DR", "Price"]
    row = next(line for line in table if line.startswith("google.com"))
    assert row.split()[:4] == ["google.com", "taken", "123.5K", "42"] and row.endswith("this")
    assert "google.net" in h.out and "google.app" in h.out
    # final output: no placeholder anywhere
    assert "..." not in h.out


def test_table_columns_drop_when_no_values(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    h.command_outputs = {"traffic-tool": "{}", "dr-tool": '{"dr": 0}'}
    assert h.run("google.com") == 0
    lines = ANSI.sub("", h.out).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Domain"))
    assert lines[start].split()[:2] == ["Domain", "Status"]


def test_table_without_taken_rows_or_commands_is_unchanged(tmp_path: Path) -> None:
    # all available -> no new columns even with commands configured
    name = "xq7zvkpl3qwmnotreal"
    h = Harness(
        tmp_path,
        whois={f"{name}.com": whois_fixture("unregistered.com")},
        environ=dict(CREDS2),
    )
    h.http.routes[SPACESHIP_URL] = spaceship_answers({}, default="available")
    h.run(f"{name}.com")
    lines = ANSI.sub("", h.out).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Domain"))
    assert lines[start].split()[:2] == ["Domain", "Status"]
    assert h.command_calls == []


def test_nontty_output_with_columns_matches_valueless_except_new_cells(tmp_path: Path) -> None:
    def table_lines(values: dict[str, str | Exception], commands: bool) -> list[list[str]]:
        env = dict(CREDS2) if commands else dict(CREDS)
        h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=env)
        h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
        h.command_outputs = values
        h.run("google.com")
        lines = ANSI.sub("", h.stdout.getvalue()).splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("Domain"))
        return [line.split() for line in lines[start:]]

    with_values = table_lines({"traffic-tool": TRAFFIC_OUT, "dr-tool": DR_OUT}, commands=True)
    disabled = table_lines({}, commands=False)
    assert len(with_values) == len(disabled)
    # same rows; the first two columns match everywhere
    for row_with, row_without in zip(with_values, disabled):
        assert row_with[:2] == row_without[:2]
        if row_without and row_without[0] == "Taken:":
            assert " ".join(row_with) == " ".join(row_without)


def test_tty_final_render_equals_nontty_after_stripping_ansi(tmp_path: Path) -> None:
    outs = []
    for isatty in (False, True):
        h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2), isatty=isatty)
        h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
        h.command_outputs = {"traffic-tool": TRAFFIC_OUT, "dr-tool": DR_OUT}
        h.run("google.com")
        outs.append(ANSI.sub("", h.stdout.getvalue()))
    # The TTY run paints placeholder frames too, so compare only the tail after
    # the table header (the final frame and everything below it are identical).
    tail = [lines[outs[i].index("Domain ") :] for i, lines in enumerate([o.splitlines() for o in outs])]
    assert tail[0] == tail[1]


def test_queried_domain_reuses_summary_metrics_no_extra_calls(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    h.command_outputs = {"traffic-tool": TRAFFIC_OUT, "dr-tool": DR_OUT}
    h.run("google.com")
    # google.com runs exactly once per command (the summary); the table adds
    # only the other taken rows, never the queried domain again
    assert sorted(h.command_calls) == [
        ["dr-tool", "--q", "google.com"],
        ["dr-tool", "--q", "google.net"],
        ["dr-tool", "--q", "google.org"],
        ["traffic-tool", "google.com"],
        ["traffic-tool", "google.net"],
        ["traffic-tool", "google.org"],
    ]


def test_table_cell_failure_noted_on_stderr_with_verbose(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2))
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    h.command_outputs = {
        "traffic-tool": subprocess.TimeoutExpired(["traffic-tool"], 8),
        "dr-tool": DR_OUT,
    }
    h.run("-v", "google.com")
    assert "DOMAIN_I_METRICS_CMD_TRAFFIC failed" in h.stderr.getvalue()
    row = next(line for line in ANSI.sub("", h.out).splitlines() if line.startswith("google.net"))
    assert row.split()[2] != "..."  # final render has no placeholder


def test_colour_for_metric_cells_follows_summary_rules(tmp_path: Path) -> None:
    h = Harness(tmp_path, whois={"google.com": whois_fixture("google.com")}, environ=dict(CREDS2) | {"FORCE_COLOR": "1"})
    h.http.routes[SPACESHIP_URL] = spaceship_answers({"google.app": "available"})
    h.command_outputs = {"traffic-tool": TRAFFIC_OUT, "dr-tool": DR_OUT}
    h.run("google.com")
    stripped = ANSI.sub("", h.out).splitlines()
    idx = next(i for i, line in enumerate(stripped) if "123.5K/mo" in line)
    table_idx = next(i for i, line in enumerate(stripped) if line.startswith("google.net"))
    # the code directly before the traffic value in the table row, not the
    # row-leading status colour
    before_value = re.search(r"\x1b\[([0-9;]+)m123\.5K", h.out.splitlines()[table_idx])
    assert before_value is not None and f"\x1b[{before_value[1]}m" == ANSI.findall(h.out.splitlines()[idx])[0]
