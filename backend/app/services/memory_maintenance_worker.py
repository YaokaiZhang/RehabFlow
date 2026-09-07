from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.services.memory_agent import process_queued_memory_maintenance


logger = logging.getLogger(__name__)


class MemoryMaintenanceWorker:
    """Poll the durable maintenance queue from a process-scoped lifecycle task."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        batch_size: int = 10,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float | None = None,
        processor: Callable[..., int] = process_queued_memory_maintenance,
    ) -> None:
        self.session_factory = session_factory
        self.batch_size = max(1, int(batch_size))
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self.timeout_seconds = timeout_seconds
        self.processor = processor
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def process_once(self) -> int:
        db = self.session_factory()
        try:
            kwargs: dict[str, Any] = {"limit": self.batch_size}
            if self.timeout_seconds is not None:
                kwargs["timeout_seconds"] = self.timeout_seconds
            return int(self.processor(db, **kwargs) or 0)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="rehab-memory-maintenance-worker",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        stop_event = self._stop_event
        self._stop_event = None
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.process_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("memory maintenance worker iteration failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
