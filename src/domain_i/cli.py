"""Command line entry point: ``domain-i [-v] <domain | name | words...>``."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Generic, TextIO, TypeVar

from .availability import CheckResult, Report, check_domains, first_batch, resolve_tlds, run_availability
from .config import default_cache_dir, default_config_dir, read_config
from .http import Http, UrllibHttp
from .info import DomainInfo
from .inputs import DomainQuery, InputError, Splitter, name_from_domain, parse_query, words_for
from .metrics import (
    COMMAND_VAR,
    CommandRunner,
    Metric,
    MetricsFetcher,
    default_command_runner,
    fetch_metrics,
    merge_metrics,
    run_metrics_command,
)
from .rdap import Rdap, info_from_rdap
from .spaceship import Availability, check, load_credentials
from .whois import WhoisRunner, default_runner, parse_whois, run_whois

WHOIS_TIMEOUT = 10.0
BACKGROUND_BUDGET = 9.0  # seconds after start we wait for metrics / registrar privacy
TABLE_BUDGET = 30.0  # the other-TLD check has its own HTTP timeouts; this is a backstop

RESET = "\x1b[0m"
COLORS = {
    "available": "\x1b[1;32m",  # bold green
    "taken": "\x1b[1;33m",  # bold yellow
    "unknown": "\x1b[2m",  # dim
    "Traffic": "\x1b[36m",  # cyan
    "DR": "\x1b[35m",  # magenta
    "Rank": "\x1b[34m",  # blue
}
HEADER_STATE = {"available": "NOT REGISTERED", "taken": "REGISTERED", "unknown": "UNKNOWN"}
LABEL_WIDTH = len("Registrar") + 2


@dataclass
class Deps:
    http: Http
    whois_runner: WhoisRunner
    environ: Mapping[str, str]
    config_dir: Path
    cache_dir: Path
    today: date
    stdout: TextIO
    stderr: TextIO
    isatty: bool
    splitter: Splitter
    metrics: MetricsFetcher | None
    command_runner: CommandRunner = default_command_runner


class Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, key: str, text: str) -> str:
        code = COLORS.get(key)
        return f"{code}{text}{RESET}" if self.enabled and code else text


def use_color(flag: bool, environ: Mapping[str, str], isatty: bool) -> bool:
    if flag:
        return True
    force = environ.get("FORCE_COLOR")
    if force and force != "0":
        return True
    if environ.get("NO_COLOR"):
        return False
    return isatty


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-i",
        description="Concise WHOIS/RDAP summary for a domain, or availability of a name across TLDs.",
    )
    parser.add_argument("query", nargs="*", help="example.com | foobar | foo bar | foo-bar")
    parser.add_argument("-v", "--verbose", action="store_true", help="print raw WHOIS / RDAP output first")
    parser.add_argument("--no-metrics", action="store_true", help="skip the Traffic/DR/Rank lookups for registered domains")
    parser.add_argument("--color", action="store_true", help="force coloured output")
    parser.add_argument("--tlds", metavar="LIST", help="TLDs to check in availability mode, e.g. com,io,dev")
    return parser


# --- rendering -------------------------------------------------------------------


def _fmt_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _fmt_expires(value: date | None, today: date) -> str | None:
    if value is None:
        return None
    days = (value - today).days
    if days > 0:
        return f"{value.isoformat()} ({days} day{'s' if days != 1 else ''} left)"
    if days == 0:
        return f"{value.isoformat()} (expires today)"
    return f"{value.isoformat()} (expired {-days} day{'s' if days != -1 else ''} ago)"


def render_header(domain: str, state: str, paint: Painter) -> str:
    return f"{domain}  {paint(state, HEADER_STATE[state])}"


def render_info(info: DomainInfo, today: date, metrics: Sequence[Metric], paint: Painter) -> list[str]:
    rows: list[tuple[str, str | None]] = [
        ("Created", _fmt_date(info.created)),
        ("Updated", _fmt_date(info.updated)),
        ("Expires", _fmt_expires(info.expires, today)),
        ("Privacy", info.privacy),  # always shown, "unknown" when undecided
        ("Registrar", info.registrar),
        ("DNS", ", ".join(info.nameservers)),
    ]
    lines = [f"{label:<{LABEL_WIDTH}}{value}" for label, value in rows if value]
    for m in metrics:
        lines.append(paint(m.label, f"{m.label:<{LABEL_WIDTH}}{m.value} ({m.source})"))
    return lines


QUERIED_MARK = "\u2190 this"


def render_table(report: Report, paint: Painter) -> list[str]:
    with_price = any(r.price for r in report.rows)
    w0 = max([len("Domain"), *(len(r.domain) for r in report.rows)])
    w1 = max([len("Status"), *(len(r.status) for r in report.rows)])
    w2 = max([len("Price"), *(len(r.price or "-") for r in report.rows)])

    def row(domain: str, status: str, price: str | None, mark: bool = False) -> str:
        cells = [f"{domain:<{w0}}", f"{status:<{w1}}"]
        if with_price:
            cells.append(f"{price or '-':<{w2}}")
        if mark:
            cells.append(QUERIED_MARK)
        return "  ".join(cells).rstrip()

    lines = [row("Domain", "Status", "Price")]
    for r in report.rows:
        lines.append(paint(r.status, row(r.domain, r.status, r.price, r.domain == report.queried)))
    unknown = f" ({report.unknown} unknown)" if report.unknown else ""
    lines.append(f"Taken: {report.taken}/{report.total}{unknown}")
    return lines


def render_report(report: Report, paint: Painter, out: TextIO) -> None:
    for line in render_table(report, paint):
        out.write(line + "\n")
    out.write("\n")
    if report.hyphen_checked:
        out.write("All plain names are taken, so hyphenated variants were checked too.\n")
    if report.hint:
        out.write(report.hint + "\n")
    out.write(f"Backend: {', '.join(report.backends)}\n")


# --- best-effort background work ---------------------------------------------------

T = TypeVar("T")


class _Background(Generic[T]):
    """Run a best-effort job on a daemon thread so it can never block exit.

    ``result`` waits at most until ``deadline`` (a ``time.monotonic`` value)
    and returns ``fallback`` on timeout or on any exception.
    """

    def __init__(self, job: Callable[[], T], fallback: T, deadline: float) -> None:
        self._result = fallback
        self._fallback = fallback
        self._deadline = deadline
        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread.start()

    def _run(self, job: Callable[[], T]) -> None:
        try:
            self._result = job()
        except Exception:  # noqa: BLE001 - best-effort by design
            self._result = self._fallback

    def result(self) -> T:
        self._thread.join(max(0.0, self._deadline - time.monotonic()))
        return self._fallback if self._thread.is_alive() else self._result


# --- main flow -------------------------------------------------------------------------


def main(argv: Sequence[str], deps: Deps) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv))
    try:
        query = parse_query(args.query, deps.splitter)
    except InputError as exc:
        parser.error(str(exc))

    out = deps.stdout
    paint = Painter(use_color(bool(args.color), deps.environ, deps.isatty))
    config = read_config(deps.environ, deps.config_dir)
    tlds, warning = resolve_tlds(args.tlds, deps.environ, config)
    if warning:
        print(warning, file=deps.stderr)

    rdap = Rdap(deps.http, cache_dir=deps.cache_dir)
    creds = load_credentials(deps.environ, deps.config_dir)
    http = deps.http
    spaceship: Callable[[list[str]], dict[str, Availability]] | None = (
        (lambda domains: check(http, creds, domains)) if creds is not None else None
    )

    def checker(domains: list[str]) -> CheckResult:
        return check_domains(domains, spaceship, rdap.lookup)

    if not isinstance(query, DomainQuery):
        report = run_availability(query.words, tlds, None, checker)
        render_report(report, paint, out)
        return 1 if report.rows and all(r.status == "unknown" for r in report.rows) else 0

    domain = query.domain
    # The other-TLD table is shown whatever the lookup says, so start it now,
    # in parallel with WHOIS; the looked-up row is pinned to the lookup result later.
    name_words = words_for(name_from_domain(domain), deps.splitter)
    batch = first_batch(name_words, tlds, domain)
    table_job: _Background[CheckResult | None] = _Background(
        lambda: checker(batch), None, time.monotonic() + TABLE_BUDGET
    )
    deadline = time.monotonic() + BACKGROUND_BUDGET
    fetch_metrics_job = deps.metrics
    no_metrics: list[Metric] = []
    background = (
        _Background(lambda: fetch_metrics_job(domain), no_metrics, deadline)
        if fetch_metrics_job is not None and not args.no_metrics
        else None
    )
    registry_rdap: dict[str, object] | None = None
    lookup = run_whois(domain, WHOIS_TIMEOUT, deps.whois_runner)
    if args.verbose:
        if lookup.text:
            out.write(lookup.text if lookup.text.endswith("\n") else lookup.text + "\n")
        if lookup.error:
            print(lookup.error, file=deps.stderr)
    parsed = parse_whois(lookup.text)
    info = parsed.info
    reason: str
    state: str
    if info is not None:
        state, reason = "taken", ""
    elif parsed.not_found is not None:
        state, reason = "available", f"WHOIS: {parsed.not_found}"
    else:
        rdap_result = rdap.lookup(domain)
        if rdap_result.state == "registered" and rdap_result.data is not None:
            if args.verbose and rdap_result.raw:
                out.write(rdap_result.raw + "\n")
            info = info_from_rdap(rdap_result.data)
            registry_rdap = rdap_result.data
            state, reason = "taken", ""
        elif rdap_result.state == "unregistered":
            state, reason = "available", "RDAP: 404 (no such domain at the registry)"
        else:
            why = "; ".join(x for x in (lookup.error, rdap_result.error) if x)
            state = "unknown"
            reason = "No registration record found via WHOIS or RDAP" + (f" ({why})." if why else ".")
    if info is not None:
        # Only for registered domains: the user's metrics command (if any) ...
        command = (deps.environ.get(COMMAND_VAR) or config.get(COMMAND_VAR) or "").strip()
        runner = deps.command_runner
        no_result: tuple[list[Metric], str | None] = ([], "did not finish in time")
        command_job = (
            _Background(lambda: run_metrics_command(command, domain, runner), no_result, deadline)
            if command and not args.no_metrics
            else None
        )
        # ... and, when WHOIS could not decide privacy, the registrar's RDAP record
        # (registries rarely carry registrant data; many registrars dropped port 43).
        registry_data = registry_rdap
        privacy_job = (
            _Background(lambda: rdap.registrar_privacy(domain, registry_data), None, deadline)
            if info.privacy == "unknown"
            else None
        )
        privacy = privacy_job.result() if privacy_job is not None else None
        if privacy is not None:
            info = replace(info, privacy=privacy)
        custom: list[Metric] = []
        if command_job is not None:
            custom, command_error = command_job.result()
            if command_error and args.verbose:
                print(f"domain-i: metrics command failed: {command_error}", file=deps.stderr)
        builtin = background.result() if background is not None else []
        metrics = merge_metrics(custom, builtin)
        out.write(render_header(domain, "taken", paint) + "\n")
        for line in render_info(info, deps.today, metrics, paint):
            out.write(line + "\n")
    else:
        out.write(render_header(domain, state, paint) + "\n")
        out.write(reason + "\n")
    out.write("\n")

    first = table_job.result()
    known = {domain: state} if state in ("taken", "available") else {}
    if first is None:
        first = CheckResult([], "no answer from the availability backend")
    report = run_availability(name_words, tlds, domain, checker, known=known, first=first)
    if not first.rows or report.backend_failed:
        out.write(f"Other TLDs could not be checked ({first.backend}).\n")
        return 1 if state == "unknown" else 0
    render_report(report, paint, out)
    return 0


def _wordninja_split(word: str) -> list[str]:
    import wordninja

    parts: list[str] = [str(p) for p in wordninja.split(word)]
    return parts


def run() -> None:
    environ = dict(os.environ)
    config_dir = default_config_dir(environ)
    http = UrllibHttp()
    settings = {**read_config(environ, config_dir), **environ}
    deps = Deps(
        http=http,
        whois_runner=default_runner,
        environ=environ,
        config_dir=config_dir,
        cache_dir=default_cache_dir(environ),
        today=date.today(),
        stdout=sys.stdout,
        stderr=sys.stderr,
        isatty=sys.stdout.isatty(),
        splitter=_wordninja_split,
        metrics=lambda domain: fetch_metrics(http, domain, settings),
    )
    try:
        code = main(sys.argv[1:], deps)
    except KeyboardInterrupt:
        code = 130
    sys.stdout.flush()
    sys.exit(code)
