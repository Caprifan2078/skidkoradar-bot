"""Non-overlapping background schedule; Telegram long-poll remains responsive."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable

LOG = logging.getLogger('skidki.scheduler')


@dataclass
class ScheduledJob:
    name: str
    interval_seconds: int
    action: Callable[[], None]
    last_started: float | None = None
    runs: int = 0
    failures: int = 0


class BackgroundScheduler:
    """Runs at most one background task at a time in its own thread.

    No backlog: a slow run delays the next run instead of spawning more work.
    An exception in one task does not stop later tasks.
    """
    def __init__(self, jobs: list[ScheduledJob], *, clock=time.monotonic,
                 stop: threading.Event | None = None):
        self.jobs = jobs
        self.clock = clock
        self.stop = stop or threading.Event()
        self._thread: threading.Thread | None = None

    def run_due(self):
        for job in self.jobs:
            if self.stop.is_set():
                break
            now = self.clock()
            if job.last_started is not None and now - job.last_started < job.interval_seconds:
                continue
            job.last_started = now
            job.runs += 1
            try:
                job.action()
            except Exception:
                job.failures += 1
                LOG.exception('Background job failed: %s', job.name)

    def run_forever(self):
        while not self.stop.is_set():
            self.run_due()
            self.stop.wait(1)

    def start(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError('Scheduler already started')
        self._thread = threading.Thread(target=self.run_forever, name='discount-scheduler', daemon=True)
        self._thread.start()

    def shutdown(self):
        self.stop.set()
        if self._thread:
            self._thread.join(timeout=5)
