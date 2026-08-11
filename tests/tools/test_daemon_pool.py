"""Tests for tools.daemon_pool.DaemonThreadPoolExecutor.

The daemon pool exists so abandoned workers (interrupted/timed-out tool
batches, wedged memory-provider syncs) can never block interpreter exit:
stdlib ThreadPoolExecutor workers are non-daemon AND registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every
worker unconditionally — even after shutdown(wait=False).
"""

import subprocess
import sys
import threading
import time

import pytest
from concurrent.futures.thread import BrokenThreadPool, _threads_queues

from tools.daemon_pool import DaemonThreadPoolExecutor


def test_workers_are_daemon_threads():
    pool = DaemonThreadPoolExecutor(max_workers=2)
    try:
        info = pool.submit(
            lambda: (threading.current_thread().daemon, threading.current_thread())
        ).result(timeout=10)
        is_daemon, worker = info
        assert is_daemon is True
        # Not registered with concurrent.futures' atexit join hook.
        assert worker not in _threads_queues
    finally:
        pool.shutdown(wait=True)


def test_idle_worker_reuse():
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        tid1 = pool.submit(threading.get_ident).result(timeout=10)
        time.sleep(0.05)  # let the worker park on the idle semaphore
        tid2 = pool.submit(threading.get_ident).result(timeout=10)
        assert tid1 == tid2
    finally:
        pool.shutdown(wait=True)


def test_initializer_runs_once_in_each_worker_with_initargs():
    lock = threading.Lock()
    initialized = []
    release = threading.Barrier(4)

    def initializer(marker):
        with lock:
            initialized.append((threading.get_ident(), marker))

    pool = DaemonThreadPoolExecutor(
        max_workers=4,
        initializer=initializer,
        initargs=("ready",),
    )
    try:
        futures = [pool.submit(release.wait) for _ in range(4)]
        assert all(f.result(timeout=10) >= 0 for f in futures)
        assert len(initialized) == 4
        assert len({thread_id for thread_id, _ in initialized}) == 4
        assert {marker for _, marker in initialized} == {"ready"}
        assert len(pool._threads) <= 4
        assert all(thread.daemon for thread in pool._threads)
    finally:
        pool.shutdown(wait=True)


def test_initializer_failure_breaks_pool():
    def broken_initializer():
        raise RuntimeError("initializer boom")

    pool = DaemonThreadPoolExecutor(max_workers=1, initializer=broken_initializer)
    try:
        with pytest.raises(BrokenThreadPool):
            pool.submit(lambda: None).result(timeout=10)
    finally:
        pool.shutdown(wait=True)


def test_wedged_worker_does_not_block_interpreter_exit():
    """A worker stuck in a long sleep must not hold the process open.

    With stdlib ThreadPoolExecutor this subprocess hangs until the sleep
    finishes (the atexit hook joins the worker); with the daemon pool it
    exits as soon as the main thread returns.
    """
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.daemon_pool import DaemonThreadPoolExecutor\n"
        "import time\n"
        "pool = DaemonThreadPoolExecutor(max_workers=1)\n"
        "pool.submit(time.sleep, 120)\n"
        "time.sleep(0.3)\n"
        "pool.shutdown(wait=False)\n"
        "print('main-done', flush=True)\n"
    ) % (str(_repo_root()),)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "main-done" in proc.stdout


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]
