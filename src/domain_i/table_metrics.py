"""Traffic/DR cells for the availability table: plan, run and render them.

Only rows whose status is ``taken`` are queried. The rendered table always has
the columns while work is pending (cells show a dim placeholder); once every
cell is resolved, columns with no value at all are dropped.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from .metrics import CommandRunner, Metric, run_metrics_command

QUERIED_MARK = "\u2190 this"

if TYPE_CHECKING:
    from .availability import Report
    from .cli import Painter

PLACEHOLDER = "..."
MAX_WORKERS = 4


@dataclass(frozen=True)
class TablePlan:
    commands: list[tuple[str, str]]
    cells: list[tuple[str, str]]  # (domain, command)


def plan_cells(
    report: Report,
    commands: Sequence[tuple[str, str]] | None = None,
    *,
    known_metrics: Mapping[str, Sequence[Metric]] | None = None,
) -> list[tuple[str, str]]:
    """(domain, command) for every taken row without values already in hand."""
    if not commands:
        return []
    known = known_metrics or {}
    plan: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in report.rows:
        if row.status != "taken" or row.domain in known:
            continue  # the summary already answered (or nothing to ask)
        for _, command in commands:
            key = (row.domain, command)
            if key not in seen:
                seen.add(key)
                plan.append(key)
    return plan


def abbreviate_cell(value: float) -> str | None:
    """Compact traffic for a table cell: 745628929 -> '745.6M'; non-positive -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    number = Decimal(str(value))
    if number.quantize(Decimal(1), rounding=ROUND_HALF_UP) < 1000:
        return str(int(number.quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    for suffix, size in (("K", Decimal(1000)), ("M", Decimal(10) ** 6), ("B", Decimal(10) ** 9)):
        scaled = (number / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if scaled < 1000 or suffix == "B":
            return f"{scaled}{suffix}"
    raise AssertionError("unreachable")


def _cell_value(metrics: Sequence[Metric], label: str) -> str | None:
    metric = next((m for m in metrics if m.label == label), None)
    if metric is None:
        return None
    if metric.label != "Traffic":
        return metric.value
    # traffic arrives as "745.6M/mo"; keep only the compact number for the cell
    compact = metric.value.removesuffix("/mo").replace(",", "")
    if compact and compact[-1] in "KMB":
        return compact
    try:
        return abbreviate_cell(float(compact))
    except ValueError:
        return None


def run_cells(
    plan: Sequence[tuple[str, str]],
    runner: CommandRunner,
    *,
    max_workers: int = MAX_WORKERS,
    budget: float,
    collect: Callable[[dict[str, list[Metric]]], None],
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Run the planned cells with bounded concurrency, reporting values as they arrive.

    Work started after ``budget`` seconds is abandoned (its threads keep the
    pool alive but results past the budget are ignored).
    """
    if not plan:
        return
    deadline = time.monotonic() + budget

    def one(domain: str, command: str) -> tuple[str, list[Metric], str | None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return domain, [], f"out of time (budget {budget:g}s)"
        try:
            found, error = run_metrics_command(command, domain, runner)
        except Exception as exc:  # noqa: BLE001 - an injected runner may raise anything
            return domain, [], str(exc)
        return domain, found, error

    lock = threading.Lock()
    gathered: dict[str, list[Metric]] = {}

    def deliver(result: tuple[str, list[Metric], str | None]) -> None:
        domain, found, error = result
        if time.monotonic() >= deadline:
            return  # past the budget: treat as "no value"
        if error and on_error is not None and "out of time" not in error:
            on_error(f"for {domain}: {error}")
        if found:
            with lock:
                gathered.setdefault(domain, []).extend(found)
                collect(dict(gathered))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(one, domain, command) for domain, command in plan]
        for future in futures:

            def done(f: Future[tuple[str, list[Metric], str | None]]) -> None:
                try:
                    deliver(f.result())
                except Exception as exc:  # noqa: BLE001 - a broken cell is a "no value"
                    if on_error is not None:
                        on_error(f"uncaught: {exc}")

            future.add_done_callback(done)


@dataclass
class TableFrame:
    report: Report
    commands: list[tuple[str, str]]
    values: dict[str, list[Metric]] = field(default_factory=dict)
    paint: Painter | None = None
    final: bool = False

    def render(self, final: bool | None = None) -> list[str]:
        from .cli import Painter

        is_final = self.final if final is None else final
        paint = self.paint or Painter(False)
        rows = self.report.rows
        show = bool(self.commands) and any(r.status == "taken" for r in rows)
        if is_final and show:
            show = any(self.values.get(r.domain) for r in rows if r.status == "taken")
        if not show:
            return _plain_table(self.report, paint)
        traffic: dict[str, str] = {}
        dr: dict[str, str] = {}
        for domain, metrics in self.values.items():
            for label, key in (("Traffic", traffic), ("DR", dr)):
                value = _cell_value(metrics, label)
                if value:
                    key[domain] = value
        w0 = max([len("Domain"), *(len(r.domain) for r in rows)])
        w1 = max([len("Status"), *(len(r.status) for r in rows)])
        wt = max([len("Traffic"), *([len(v) for v in traffic.values()] or [len(PLACEHOLDER)])])
        wd = max([len("DR"), *([len(v) for v in dr.values()] or [len(PLACEHOLDER)])])

        def cell(domain: str, column: dict[str, str], width: int, colour: str) -> str:
            value = column.get(domain)
            text = value if value else ("" if is_final else PLACEHOLDER)
            padded = f"{text:<{width}}"
            return paint(colour, padded) if paint.enabled and value else padded

        def line(cells: list[str], mark: bool, colour: str | None) -> str:
            if mark:
                cells.append(QUERIED_MARK)
            text = "  ".join(cells).rstrip()
            return paint(colour, text) if colour else text

        header_cells = [
            f"{'Domain':<{w0}}",
            f"{'Status':<{w1}}",
            f"{'Traffic':<{wt}}",
            f"{'DR':<{wd}}",
            "Price",
        ]
        lines = [line(header_cells, False, None)]
        for r in rows:
            cells = [
                f"{r.domain:<{w0}}",
                f"{r.status:<{w1}}",
                cell(r.domain, traffic, wt, "Traffic"),
                cell(r.domain, dr, wd, "DR"),
                r.price or "-",
            ]
            lines.append(line(cells, r.domain == self.report.queried, r.status))
        unknown = f" ({self.report.unknown} unknown)" if self.report.unknown else ""
        lines.append(f"Taken: {self.report.taken}/{self.report.total}{unknown}")
        return lines


def _plain_table(report: Report, paint: Painter) -> list[str]:
    from .cli import render_table

    return render_table(report, paint)
