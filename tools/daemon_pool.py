"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so:

  - ``_python_exit`` never joins them, and
  - the interpreter's non-daemon thread join at shutdown skips them.

Semantics are otherwise identical (initializer/initargs, work queue,
idle-thread reuse).  Use it for any pool whose work is best-effort or
independently interruptible and must never hold the process open:
concurrent tool execution, background memory sync, catalog fan-out,
subagent timeout wrappers.  Do NOT use it for work that must complete
before exit (durable writes) — those belong on foreground threads with
explicit bounded joins.
"""

from __future__ import annotations

import threading
import weakref
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker

__all__ = ["DaemonThreadPoolExecutor"]

logger = logging.getLogger(__name__)

_WORKER_PARAMETERS = tuple(inspect.signature(_worker).parameters)
_MODERN_WORKER_PARAMETERS = ("executor_reference", "ctx", "work_queue")
_LEGACY_WORKER_PARAMETERS = (
    "executor_reference",
    "work_queue",
    "initializer",
    "initargs",
)


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def _adjust_thread_count(self) -> None:
        # Unknown future CPython worker contracts degrade to the stdlib pool.
        # Losing daemon semantics is safer than breaking every submit() call.
        if _WORKER_PARAMETERS not in (
            _MODERN_WORKER_PARAMETERS,
            _LEGACY_WORKER_PARAMETERS,
        ):
            logger.warning(
                "Unsupported concurrent.futures.thread._worker signature %s; "
                "falling back to standard non-daemon workers",
                _WORKER_PARAMETERS,
            )
            return super()._adjust_thread_count()

        # Mirrors CPython's implementation with two changes: daemon=True and
        # no _threads_queues registration. Python 3.14 replaced the explicit
        # initializer/initargs worker contract with a WorkerContext.
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            if _WORKER_PARAMETERS == _MODERN_WORKER_PARAMETERS:
                worker_args = (
                    weakref.ref(self, weakref_cb),
                    self._create_worker_context(),
                    self._work_queue,
                )
            else:
                worker_args = (
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                )
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=worker_args,
                daemon=True,
            )
            t.start()
            self._threads.add(t)
