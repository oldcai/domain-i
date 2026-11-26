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

from .availability import Row
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


Cell = tuple[str, str]  # (domain, command)


def run_cells(
    plan: Sequence[Cell],
    runner: CommandRunner,
    *,
    max_workers: int = MAX_WORKERS,
    budget: float,
    collect: Callable[[dict[str, list[Metric]]], None],
    on_error: Callable[[str], None] | None = None,
) -> set[Cell]:
    """Run the planned cells, reporting values as they arrive.

    Each command gets its own pool of ``max_workers``, so a slow command cannot
    starve a fast one. Returns the cells that did not produce a usable answer in
    time: failures, timeouts, "pending" answers and cells still queued at the
    end of ``budget``. Cells that answered with no/zero values are not failures.
    """
    if not plan:
        return set()
    deadline = time.monotonic() + budget
    lock = threading.Lock()
    gathered: dict[str, list[Metric]] = {}
    answered: set[Cell] = set()

    def one(domain: str, command: str) -> None:
        if time.monotonic() >= deadline:
            return
        try:
            found, error = run_metrics_command(command, domain, runner)
        except Exception as exc:  # noqa: BLE001 - an injected runner may raise anything
            found, error = [], str(exc)
        if time.monotonic() >= deadline:
            return  # too late to show: still reported as not filled
        if error:
            if on_error is not None:
                on_error(f"for {domain}: {error}")
            return
        with lock:
            answered.add((domain, command))
            if found:
                gathered.setdefault(domain, []).extend(found)
                snapshot = {d: list(m) for d, m in gathered.items()}
            else:
                snapshot = None
        if snapshot is not None:
            collect(snapshot)

    by_command: dict[str, list[str]] = {}
    for domain, command in plan:
        by_command.setdefault(command, []).append(domain)
    pools = [ThreadPoolExecutor(max_workers=max_workers) for _ in by_command]
    futures: list[Future[None]] = []
    for pool, (command, domains) in zip(pools, by_command.items()):
        futures.extend(pool.submit(one, d, command) for d in domains)
    _wait_until(futures, deadline)
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)
    with lock:
        return {cell for cell in plan if cell not in answered}


def _wait_until(futures: Sequence[Future[None]], deadline: float) -> None:
    for future in futures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            future.result(timeout=remaining)
        except Exception:  # noqa: BLE001,S112 - timeouts and errors mean "not filled"
            continue


@dataclass
class TableFrame:
    """One frame of the availability table.

    Async columns: ``Created`` (registry RDAP / whois, independent of metrics
    commands) and ``Traffic`` / ``DR`` (metrics commands). While a frame is not
    final, a column is shown when it has work in flight and its in-flight cells
    show a placeholder; in the final frame a column stays only if some row has a
    value. Price is decided by data from the start.
    """

    report: Report
    commands: list[tuple[str, str]]
    values: dict[str, list[Metric]] = field(default_factory=dict)
    paint: Painter | None = None
    final: bool = False
    created: dict[str, str] = field(default_factory=dict)
    pending_created: set[str] = field(default_factory=set)
    pending_metrics: set[str] | None = None  # None: every taken row (when commands exist)
    failed_metrics: set[Cell] = field(default_factory=set)
    failed_created: set[str] = field(default_factory=set)

    def notes(self) -> list[str]:
        """Say which cells are missing because lookups did not finish, not because
        the value is zero/unknown at the source."""
        lines: list[str] = []
        metric_domains = {d for d, _ in self.failed_metrics}
        if metric_domains:
            n = len(metric_domains)
            lines.append(
                f"Traffic/DR pending for {n} domain{'s' if n != 1 else ''} "
                "(timed out or not ready) - run again to fill."
            )
        if self.failed_created:
            n = len(self.failed_created)
            lines.append(f"Created unavailable for {n} domain{'s' if n != 1 else ''} (RDAP and WHOIS gave no date).")
        return lines

    def render(self, final: bool | None = None) -> list[str]:
        from .cli import Painter

        is_final = self.final if final is None else final
        paint = self.paint or Painter(False)
        rows = self.report.rows
        taken = {r.domain for r in rows if r.status == "taken"}
        metric_pending = (
            set() if not self.commands else (taken if self.pending_metrics is None else self.pending_metrics & taken)
        )
        created_pending = self.pending_created & taken
        traffic: dict[str, str] = {}
        dr: dict[str, str] = {}
        for domain, metrics in self.values.items():
            for label, key in (("Traffic", traffic), ("DR", dr)):
                value = _cell_value(metrics, label)
                if value:
                    key[domain] = value
        created = {d: v for d, v in self.created.items() if v}

        def keep(values: dict[str, str], pending: set[str]) -> bool:
            return bool(values) or (not is_final and bool(pending))

        show_created = keep(created, created_pending)
        show_traffic = keep(traffic, metric_pending)
        show_dr = keep(dr, metric_pending)
        show_price = any(r.price for r in rows)
        if not (show_created or show_traffic or show_dr):
            return _plain_table(self.report, paint)

        def width(header: str, values: dict[str, str], pending: set[str]) -> int:
            sizes = [len(header), *(len(v) for v in values.values())]
            if pending and not is_final:
                sizes.append(len(PLACEHOLDER))
            return max(sizes)

        w0 = max([len("Domain"), *(len(r.domain) for r in rows)])
        w1 = max([len("Status"), *(len(r.status) for r in rows)])
        wc = width("Created", created, created_pending)
        wt = width("Traffic", traffic, metric_pending)
        wd = width("DR", dr, metric_pending)

        def cell(domain: str, column: dict[str, str], pending: set[str], w: int, colour: str | None) -> str:
            value = column.get(domain)
            # only cells that are actually being looked up may show the placeholder
            text = value if value else (PLACEHOLDER if domain in pending and not is_final else "")
            padded = f"{text:<{w}}"
            return paint(colour, padded) if colour and paint.enabled and value else padded

        def line(cells: list[str], mark: bool, colour: str | None) -> str:
            if mark:
                cells.append(QUERIED_MARK)
            text = "  ".join(cells).rstrip()
            return paint(colour, text) if colour else text

        def row_cells(r: Row) -> list[str]:
            cells = [f"{r.domain:<{w0}}", f"{r.status:<{w1}}"]
            if show_created:
                cells.append(cell(r.domain, created, created_pending, wc, None))
            if show_traffic:
                cells.append(cell(r.domain, traffic, metric_pending, wt, "Traffic"))
            if show_dr:
                cells.append(cell(r.domain, dr, metric_pending, wd, "DR"))
            if show_price:
                cells.append(r.price or "-")
            return cells

        header_cells = [f"{'Domain':<{w0}}", f"{'Status':<{w1}}"]
        if show_created:
            header_cells.append(f"{'Created':<{wc}}")
        if show_traffic:
            header_cells.append(f"{'Traffic':<{wt}}")
        if show_dr:
            header_cells.append(f"{'DR':<{wd}}")
        if show_price:
            header_cells.append("Price")
        lines = [line(header_cells, False, None)]
        for r in rows:
            lines.append(line(row_cells(r), r.domain == self.report.queried, r.status))
        unknown = f" ({self.report.unknown} unknown)" if self.report.unknown else ""
        lines.append(f"Taken: {self.report.taken}/{self.report.total}{unknown}")
        return lines


def run_created(
    domains: Sequence[str],
    fetch: Callable[[str], tuple[str | None, str | None]],
    *,
    max_workers: int = MAX_WORKERS,
    budget: float,
    collect: Callable[[str, str], None],
    on_error: Callable[[str], None] | None = None,
) -> set[str]:
    """Fetch registration dates (at most ``max_workers`` at once, so at most that
    many whois processes). Returns the domains that got no date in time."""
    if not domains:
        return set()
    deadline = time.monotonic() + budget
    filled: set[str] = set()
    lock = threading.Lock()

    def one(domain: str) -> None:
        if time.monotonic() >= deadline:
            return
        try:
            created, reason = fetch(domain)
        except Exception as exc:  # noqa: BLE001 - a broken cell is a "no value"
            created, reason = None, str(exc)
        if time.monotonic() >= deadline:
            return
        if created:
            with lock:
                filled.add(domain)
            collect(domain, created)
        elif reason and on_error is not None:
            on_error(f"for {domain}: {reason}")

    pool = ThreadPoolExecutor(max_workers=max_workers)
    futures = [pool.submit(one, d) for d in domains]
    _wait_until(futures, deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    with lock:
        return {d for d in domains if d not in filled}


def _plain_table(report: Report, paint: Painter) -> list[str]:
    from .cli import render_table

    return render_table(report, paint)
