"""Concurrency stress test for the SQLite WAL connection layer.

Reproduces the baseline flake: many writer threads hammer approval_requests /
audit_trail / ledger while a poller thread reads the same pending approval row
in a tight loop (the §6 hold pattern). The spend gate fails OPEN if that poll
raises "database is locked", so the bar is ZERO OperationalError escaping any
thread.

Each thread lazily creates its own thread-local connection on first use — that
is the exact path that used to throw (per-thread re-run of PRAGMA journal_mode=WAL
ignoring busy_timeout). See db._init_db_file.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import db


def _collect(errors, lock, fn):
    try:
        fn()
    except sqlite3.OperationalError as exc:  # the failure we must never see
        with lock:
            errors.append(("OperationalError", str(exc)))
    except Exception as exc:  # noqa: BLE001 — surface anything else too
        with lock:
            errors.append((type(exc).__name__, str(exc)))


def test_no_locked_errors_under_write_and_poll_load(tmp_hermes_home):
    db.init_db()
    db.register_job("jX", "ccX")

    # A long-lived pending approval the poller hammers (the held spend).
    held = db.create_approval("jX", "ccX", 5.0, level="manager")

    duration_s = 3.0
    n_writers = 8
    stop = threading.Event()
    errors: list = []
    err_lock = threading.Lock()
    counters = {"writes": 0, "polls": 0}
    cnt_lock = threading.Lock()

    def poller():
        def loop():
            local_polls = 0
            while not stop.is_set():
                # Both read paths the spend gate / dashboard use.
                db.get_pending_approvals()
                status = db.get_approval_status(held)
                db.read_approval(held)
                assert status is not None
                local_polls += 1
            with cnt_lock:
                counters["polls"] += local_polls
        _collect(errors, err_lock, loop)

    def writer(idx: int):
        def loop():
            local_writes = 0
            i = 0
            while not stop.is_set():
                # Hammer every wrapped write path concurrently.
                db.append_audit(f"writer{idx}", "stress_event", {"i": i})
                aid = db.create_approval(f"j{idx}", "ccX", 1.0 + i * 0.01)
                db.decide_approval(aid, "approved", f"human{idx}", "ok")
                db.append_fact(f"j{idx}", "external_spend", 0.01)
                i += 1
                local_writes += 4
            with cnt_lock:
                counters["writes"] += local_writes
        _collect(errors, err_lock, loop)

    threads = [threading.Thread(target=poller)]
    threads += [threading.Thread(target=writer, args=(k,)) for k in range(n_writers)]
    for t in threads:
        t.start()
    time.sleep(duration_s)
    stop.set()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "a stress thread hung"

    # The headline assertion: nothing locked-out, on any thread.
    assert errors == [], f"errors escaped under concurrent load: {errors[:5]}"

    # Prove the load was real, not a no-op.
    assert counters["writes"] > 100, counters
    assert counters["polls"] > 100, counters
    # And the held row is still pending (poller never mutated it).
    assert db.get_approval_status(held) == "pending"


def test_poll_read_path_never_blocks_during_sustained_writes(tmp_hermes_home):
    """Tighter focus: the approval poll READ must stay lock-free while a single
    writer commits continuously (busy_timeout must cover it; no error allowed)."""
    db.init_db()
    db.register_job("jP", "ccP")
    held = db.create_approval("jP", "ccP", 9.0, level="finance")

    stop = threading.Event()
    errors: list = []
    err_lock = threading.Lock()

    def writer():
        def loop():
            i = 0
            while not stop.is_set():
                db.append_fact("jP", "external_spend", 0.02)
                db.append_audit("w", "tick", {"i": i})
                i += 1
        _collect(errors, err_lock, loop)

    w = threading.Thread(target=writer)
    w.start()
    try:
        # 400 tight reads while writes stream — the spend-gate poll.
        for _ in range(400):
            assert db.get_approval_status(held) == "pending"
    finally:
        stop.set()
        w.join(timeout=10.0)
    assert errors == [], f"writer hit a lock error: {errors[:5]}"
