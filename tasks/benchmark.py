"""Measure the filelock performance matrix and print it as a report.

The numbers establish a baseline rather than gate a build: run it before and after a change to compare medians and tail
latency, the way :issue:`642` describes. Run with ``python tasks/benchmark.py`` or ``tox -e bench``.
"""

from __future__ import annotations

import asyncio
import os
import platform
import statistics
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import Event, Process
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final

import filelock
from filelock import AsyncFileLock, FileLock, SoftFileLease, SoftFileLock, StrictSoftFileLock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from multiprocessing.synchronize import Event as EventType

_ITERATIONS: Final[int] = 200
_WARMUP: Final[int] = 20
_CONTENTION_PROCESSES: Final[int] = 8
_CONTENTION_ACQUISITIONS: Final[int] = 500


@dataclass(frozen=True)
class Sample:
    """One measured metric, in the unit named by ``unit``."""

    name: str
    median: float
    p95: float
    unit: str


def main() -> None:
    print(_environment())
    samples: list[Sample] = []
    with TemporaryDirectory(prefix="filelock-bench-") as directory:
        root = Path(directory)
        samples += _construction(root)
        samples += _uncontended(root)
        samples.append(_timeout_cpu(root))
        samples.append(_descriptor_growth(root))
        samples += _cancellation_latency(root)
        samples += _contention(root)
    _report(samples)


def _environment() -> str:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    build = "gil" if gil else "free-threaded"
    return (
        f"filelock {filelock.__version__} | {platform.python_implementation()} {platform.python_version()} ({build}) | "
        f"{platform.system()} {platform.machine()} | {os.cpu_count()} cpus"
    )


def _backends(root: Path) -> Iterator[tuple[str, Callable[[], filelock.BaseFileLock]]]:
    # Each entry builds a fresh lock on its own path so no two cases contend. ReadWriteLock is soft-optional, so it is
    # measured separately where it applies rather than through this exclusive-lock set.
    yield "FileLock", lambda: FileLock(str(root / "file.lock"))
    yield "SoftFileLock", lambda: SoftFileLock(str(root / "soft.lock"))
    yield "StrictSoftFileLock", lambda: StrictSoftFileLock(str(root / "strict.lock"))
    yield "SoftFileLease", lambda: SoftFileLease(str(root / "lease.lock"), lease_duration=30)


def _construction(root: Path) -> list[Sample]:
    return [_timed(f"construct {name}", build, unit="us", scale=1e6) for name, build in _backends(root)]


def _uncontended(root: Path) -> list[Sample]:
    samples: list[Sample] = []
    for name, build in _backends(root):
        lock = build()

        def acquire_release(held: filelock.BaseFileLock = lock) -> None:
            held.acquire()
            held.release()

        samples.append(_timed(f"acquire+release {name}", acquire_release, unit="us", scale=1e6))
    return samples


def _timeout_cpu(root: Path) -> Sample:
    # A contended acquire with a zero poll interval should spin on the clock, not the CPU. Hold the lock, time out a
    # second acquirer, and report the CPU time the wait burned; a busy loop would show wall-sized CPU here.
    holder = FileLock(str(root / "timeout.lock"))
    holder.acquire()
    waiter = FileLock(str(root / "timeout.lock"))
    try:
        started = time.process_time()
        with suppress(filelock.Timeout):
            waiter.acquire(timeout=0.25, poll_interval=0.0)
        cpu = time.process_time() - started
    finally:
        holder.release()
    return Sample("timeout cpu (0.25s wall, poll=0)", cpu * 1e3, cpu * 1e3, "ms cpu")


def _descriptor_growth(root: Path) -> Sample:
    # Acquire and release many times; a descriptor leak would show as a rising open-descriptor count.
    lock = FileLock(str(root / "fd.lock"))
    before = _open_descriptors()
    for _ in range(100):
        lock.acquire()
        lock.release()
    grew = _open_descriptors() - before
    return Sample("descriptor growth over 100 cycles", float(grew), float(grew), "fds")


def _cancellation_latency(root: Path) -> list[Sample]:
    # Hold the lock from another process so the async waiter blocks on the OS lock rather than on a peer instance in the
    # same task, which the deadlock guard would refuse. Then time canceling the awaiting task to the acquire unwinding.
    path = str(root / "cancel.lock")
    acquired, release = Event(), Event()
    holder = Process(target=_hold_until, args=(path, acquired, release))
    holder.start()

    async def measure() -> float:
        waiter = AsyncFileLock(path)
        task = asyncio.ensure_future(waiter.acquire(poll_interval=0.01))
        await asyncio.sleep(0.02)
        started = time.perf_counter()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return time.perf_counter() - started

    try:
        acquired.wait(timeout=5)
        latencies = [asyncio.run(measure()) for _ in range(20)]
    finally:
        release.set()
        holder.join(timeout=5)
    return [
        Sample(
            "async acquire cancellation latency",
            statistics.median(latencies) * 1e3,
            _percentile(latencies, 95) * 1e3,
            "ms",
        )
    ]


def _hold_until(path: str, acquired: EventType, release: EventType) -> None:
    lock = FileLock(path)
    lock.acquire()
    acquired.set()
    release.wait(timeout=30)
    lock.release()


def _contention(root: Path) -> list[Sample]:
    samples: list[Sample] = []
    for name, path in (("FileLock", root / "c-file.lock"), ("StrictSoftFileLock", root / "c-strict.lock")):
        started = time.perf_counter()
        workers = [Process(target=_hammer, args=(name, str(path))) for _ in range(_CONTENTION_PROCESSES)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        elapsed = time.perf_counter() - started
        total = _CONTENTION_PROCESSES * _CONTENTION_ACQUISITIONS
        per = elapsed * 1e3 / total
        samples.append(Sample(f"contention {name} ({total} acquisitions)", per, per, "ms/acq"))
    return samples


def _hammer(backend: str, path: str) -> None:
    lock = FileLock(path) if backend == "FileLock" else StrictSoftFileLock(path)
    for _ in range(_CONTENTION_ACQUISITIONS):
        lock.acquire()
        lock.release()


def _timed(name: str, run: Callable[[], object], *, unit: str, scale: float) -> Sample:
    for _ in range(_WARMUP):
        run()
    durations = [_one(run) for _ in range(_ITERATIONS)]
    return Sample(name, statistics.median(durations) * scale, _percentile(durations, 95) * scale, unit)


def _one(run: Callable[[], object]) -> float:
    started = time.perf_counter()
    run()
    return time.perf_counter() - started


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(percentile / 100 * len(ordered)))
    return ordered[index]


def _open_descriptors() -> int:
    for directory in ("/proc/self/fd", "/dev/fd"):
        with suppress(OSError):
            return sum(1 for _ in Path(directory).iterdir())
    return 0  # pragma: no cover  # neither procfs nor /dev/fd exists on this platform


def _report(samples: list[Sample]) -> None:
    width = max(len(sample.name) for sample in samples)
    print(f"\n{'metric':<{width}}  {'median':>12}  {'p95':>12}  unit")
    print("-" * (width + 34))
    for sample in samples:
        print(f"{sample.name:<{width}}  {sample.median:>12.3f}  {sample.p95:>12.3f}  {sample.unit}")


if __name__ == "__main__":
    main()
