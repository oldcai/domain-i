from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path

import pytest

from domain_i.availability import Report, Row
from domain_i.cli import Deps
from domain_i.http import Response, UrllibHttp
from domain_i.metrics import Metric
from domain_i.table_metrics import (
    PLACEHOLDER,
    TableFrame,
    abbreviate_cell,
    plan_cells,
    run_cells,
)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
PAINT = __import__("domain_i.cli", fromlist=["Painter"]).Painter(False)
CREDS = {"DOMAIN_I_METRICS_CMD_TRAFFIC": "traffic-tool", "DOMAIN_I_METRICS_CMD_DR": "dr-tool --q"}
REPORT = Report(
    rows=[
        Row("a.com", "taken", None),
        Row("a.net", "available", None),
        Row("b.com", "taken", None),
        Row("c.com", "unknown", None),
    ],
    backends=["Spaceship"],
    hyphen_checked=False,
    hint=None,
    taken=2,
    total=4,
    queried="a.com",
)


def test_plan_cells_only_taken_rows_and_reuse_queried() -> None:
    cells = plan_cells(REPORT, COMMANDS, known_metrics={"a.com": [Metric("Traffic", "1.0M/mo", "s")]})
    assert set(cells) == {("b.com", "traffic-tool"), ("b.com", "dr-tool --q")}
    assert plan_cells(REPORT, COMMANDS, known_metrics={}) == [
        ("a.com", "traffic-tool"),
        ("a.com", "dr-tool --q"),
        ("b.com", "traffic-tool"),
        ("b.com", "dr-tool --q"),
    ]
    # no metrics commands -> nothing planned
    assert plan_cells(REPORT, [], known_metrics={}) == []
    # available/unknown rows never queried
    assert all(d in ("a.com", "b.com") for d, _ in plan_cells(REPORT, COMMANDS, known_metrics={}))


COMMANDS = [("DOMAIN_I_METRICS_CMD_TRAFFIC", "traffic-tool"), ("DOMAIN_I_METRICS_CMD_DR", "dr-tool --q")]


def test_frame_placeholder_then_values_and_column_removal() -> None:
    commands = COMMANDS
    empty = TableFrame(REPORT, commands, {}, paint=PAINT)
    lines = empty.render()
    assert lines[0].split() == ["Domain", "Status", "Traffic", "DR"]  # no premium -> no Price column
    b_row = next(line for line in lines if "b.com" in ANSI.sub("", line))
    assert PLACEHOLDER in ANSI.sub("", b_row)

    frame = TableFrame(
        REPORT,
        commands,
        {"a.com": [Metric("Traffic", "1.5M/mo", "t"), Metric("DR", "91", "d")], "b.com": [Metric("Traffic", "2000/mo", "t")]},
        paint=PAINT,
    )
    final = frame.render()
    a_row = next(line for line in final if "a.com" in ANSI.sub("", line))
    b_row = next(line for line in final if "b.com" in ANSI.sub("", line))
    net_row = next(line for line in final if "a.net" in ANSI.sub("", line))
    assert "1.5M" in a_row and "91" in a_row
    assert "2.0K" in b_row and "  " + "|no-dr" not in b_row
    assert "Traffic" not in net_row.split()  # available row has empty cells
    # every value gone -> columns disappear entirely
    gone = TableFrame(REPORT, commands, {}, paint=PAINT).render(final=True)
    assert all("Traffic" not in line and "DR" not in line for line in gone)


def test_align_with_placeholder_and_no_ansi_mismatch() -> None:
    commands = [("DOMAIN_I_METRICS_CMD_DR", "dr-tool --q")]
    frame = TableFrame(REPORT, commands, {"a.com": [Metric("DR", "8", "d")]}, paint=PAINT)
    w0 = max(len("Domain"), *(len(r.domain) for r in REPORT.rows))
    lines = frame.render()
    stripped = {ANSI.sub("", line) for line in lines}
    assert len(stripped) == len(lines)
    # "Taken: 2/4" has one token only; every other line has the domain cell padded
    # to the same width, so the second column starts at the same offset everywhere
    starts = {line.index(line.split()[1]) for line in stripped if len(line.split()) >= 2 and not line.startswith("Taken:")}
    assert starts == {w0 + 2}  # padded domain cell + two-space gutter


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7_456_289_29, "745.6M"), (2000, "2.0K"), (999, "999"), (0, None), (-1, None), (12.5, "13")],
)
def test_abbreviate_cell(value: float, expected: str | None) -> None:
    assert abbreviate_cell(value) == expected


class Runner:
    """Fakes the subprocess runner: blocks until released, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self.results: dict[str, str] = {"traffic-tool": '{"traffic": 123456, "source": "t"}', "dr-tool": '{"dr": 42, "source": "d"}'}

    def release(self) -> None:
        self._gate.set()

    def __call__(self, args: list[str], timeout: float) -> str:
        with self._lock:
            self.calls.append(args)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self._gate.wait(timeout)
        with self._lock:
            self.active -= 1
        return self.results[args[0]]


def test_run_cells_respects_concurrency_cap_and_collects() -> None:
    runner = Runner()
    plan = [("d1.com", "traffic-tool"), ("d2.com", "traffic-tool"), ("d3.com", "traffic-tool"), ("d4.com", "traffic-tool"), ("d5.com", "traffic-tool"), ("d6.com", "traffic-tool")]
    result: dict[str, list[Metric]] = {}

    def collect(values: dict[str, list[Metric]]) -> None:
        result.update(values)

    worker = threading.Thread(target=lambda: run_cells(plan, runner, max_workers=4, collect=collect, budget=10.0))
    worker.start()
    time.sleep(0.2)
    assert runner.peak <= 4
    assert len(runner.calls) == 4  # the other two are queued
    runner.release()
    worker.join(timeout=5)
    assert runner.peak <= 4
    assert result == {d: [Metric("Traffic", "123.5K/mo", "t")] for d in ("d1.com", "d2.com", "d3.com", "d4.com", "d5.com", "d6.com")}


def test_run_cells_failures_and_timeout_are_silent(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], timeout: float) -> str:
        calls.append(args)
        if args[0] == "bad":
            raise ValueError("exit status 3")
        if args[0] == "slow":
            time.sleep(0.5)
        return '{"traffic": 1000}'

    plan = [("ok.com", "good"), ("bad.com", "bad"), ("slow.com", "slow")]
    values: dict[str, list[Metric]] = {}
    errors: list[str] = []
    run_cells(plan, runner, max_workers=3, collect=values.update, budget=0.2, on_error=errors.append)
    assert values == {"ok.com": [Metric("Traffic", "1.0K/mo", "custom")]}
    assert "slow.com" not in values  # past the budget -> no value, no error


def test_run_cells_reports_errors_with_names() -> None:
    def runner(args: list[str], timeout: float) -> str:
        raise ValueError("boom")

    errors: list[str] = []
    run_cells([("x.com", "broken")], runner, max_workers=1, collect=lambda v: None, budget=5.0, on_error=errors.append)
    assert errors == ["for x.com: boom"]


def test_price_column_follows_data_like_traffic_and_dr() -> None:
    premium = Report(
        rows=[Row("p.app", "available", "827.17 USD"), Row("p.com", "taken", None)],
        backends=[], hyphen_checked=False, hint=None,
    )
    commands = [("DOMAIN_I_METRICS_CMD_TRAFFIC", "t")]
    # pending: Traffic shows placeholder, Price already decided by data
    pending_frame = TableFrame(premium, commands, {}, paint=PAINT)
    assert "Traffic" in pending_frame.render()[0]
    assert "Price" in pending_frame.render()[0]
    # final with traffic but no price anywhere -> Price column dropped
    plain = Report(
        rows=[Row("p.com", "taken", None)],
        backends=[], hyphen_checked=False, hint=None,
    )
    final = TableFrame(plain, commands, {"p.com": [Metric("Traffic", "167/mo", "demo")]}, paint=PAINT).render(final=True)
    header = final[0].split()
    assert header == ["Domain", "Status", "Traffic"]
    premium_final = TableFrame(premium, commands, {"p.com": [Metric("Traffic", "167/mo", "demo")]}, paint=PAINT).render(final=True)
    assert premium_final[0].split() == ["Domain", "Status", "Traffic", "Price"]
    assert any("827.17 USD" in line for line in premium_final)


# --- placeholder only for queried rows; frames: single print path ----------------------


class RecordingWriter(io.StringIO):
    """StringIO that remembers every write call so frames can be asserted on."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return super().write(text)

    @property
    def value(self) -> str:
        return self.getvalue()


def _deps_for(tmp_path: Path, out: RecordingWriter, isatty: bool) -> Deps:
    from datetime import date

    from domain_i.cli import Deps
    from domain_i.whois import default_runner

    sleeps = iter([0.05, 0.2, 0.35, 0.5])

    def runner(args: list[str], timeout: float) -> str:
        time.sleep(next(sleeps, 0.05))
        return '{"traffic": 1000, "source": "t"}'

    return Deps(
        http=UrllibHttp(),
        whois_runner=default_runner,
        environ={"DOMAIN_I_METRICS_CMD_TRAFFIC": "traffic-tool", "SPACESHIP_API_KEY": "k", "SPACESHIP_API_SECRET": "s"},
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        today=date(2026, 11, 26),
        stdout=out,
        stderr=io.StringIO(),
        isatty=isatty,
        splitter=lambda w: [w],
        metrics=None,
        command_runner=runner,
    )


class BulkHttp:
    """Spaceship bulk endpoint that answers for whatever was asked."""

    def __init__(self, result: str) -> None:
        self.result = result

    def request(self, method: str, url: str, *, headers: object, body: bytes | None = None, timeout: float = 0) -> Response:
        asked = json.loads(body or b"{}").get("domains", [])
        items = [{"domain": d, "result": self.result, "premiumPricing": []} for d in asked]
        return Response(200, json.dumps({"domains": items}).encode())


def test_placeholder_only_on_queried_rows(tmp_path: Path) -> None:
    from domain_i.cli import main

    out = RecordingWriter()
    deps = _deps_for(tmp_path, out, isatty=True)
    deps.http = BulkHttp("available")
    assert main(["qwertyuiop"], deps) == 0
    assert "..." not in out.value


def test_frames_erase_and_single_print_path(tmp_path: Path) -> None:
    from domain_i.cli import main

    out = RecordingWriter()
    deps = _deps_for(tmp_path, out, isatty=True)
    deps.http = BulkHttp("taken")
    main(["qwertyuiop"], deps)
    text = out.value
    frames = text.split("\x1b[J")  # every erase cuts a frame boundary
    assert len(frames) >= 3  # initial + at least one update + final
    # every frame after the first starts with cursor-up + erase
    assert text.count("\x1b[") >= len(frames) - 1
    # exactly one final render: the trailing frame ends with the Backend line once
    assert text.count("Backend: ") == 1
    assert text.count("Taken: 4/4") == len(frames)  # once per frame, inside the frame


def test_non_tty_prints_single_frame(tmp_path: Path) -> None:
    from domain_i.cli import main

    out = RecordingWriter()
    deps = _deps_for(tmp_path, out, isatty=False)
    deps.http = BulkHttp("taken")
    main(["qwertyuiop"], deps)
    assert "\x1b[" not in out.value
    assert out.value.count("Backend: ") == 1
