"""
Event-driven verdict return (2026-07-22): the report thread must fire a
fresh command result the INSTANT the poll thread deposits it, not wait out
the flat REPORT_SEC. These tests pin the wake mechanism directly (no full
main() spin-up): record_result appends AND wakes, the clear-before-snapshot
loop never loses a verdict, and an idle reporter still times out at the
normal cadence (dormancy law — nothing fires without real pending work).
"""

import collections
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sas_host


class _FakePollerState:
    online = True


class _FakePoller:
    state = _FakePollerState()


class _FakeStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"tickets": {}}


def _reporter():
    return sas_host.HubReporter(
        "http://127.0.0.1:0", "smib-test", "/dev/null", 1,
        _FakePoller(), {}, collections.deque(maxlen=20), _FakeStore())


def test_record_result_appends_and_wakes():
    r = _reporter()
    assert not r.result_ready.is_set(), "fresh reporter must not be armed"
    r.record_result({"id": "c1", "ok": True})
    assert list(r.command_results)[0]["id"] == "c1", "result deposited"
    assert r.result_ready.is_set(), "depositing a result wakes the reporter"


def test_wait_wakes_early_on_a_fresh_result():
    """The report thread's wait(REPORT_SEC) must return the instant a result
    lands, not at the full timeout — the whole point of the fix."""
    r = _reporter()
    r.result_ready.clear()
    delay = 0.05

    def deposit_later():
        time.sleep(delay)
        r.record_result({"id": "c2", "ok": True})

    threading.Thread(target=deposit_later, daemon=True).start()
    t0 = time.monotonic()
    woke = r.result_ready.wait(sas_host.REPORT_SEC)   # the real report-loop wait
    elapsed = time.monotonic() - t0
    assert woke is True, "wait returned on the event, not a timeout"
    assert elapsed < sas_host.REPORT_SEC / 2, (
        f"woke early ({elapsed:.3f}s), not at the {sas_host.REPORT_SEC}s tick")


def test_idle_reporter_still_times_out_at_cadence():
    """No pending result => the reporter falls through at REPORT_SEC (normal
    cadence). Proves the fix never busy-loops and the dormancy law holds."""
    r = _reporter()
    r.result_ready.clear()
    t0 = time.monotonic()
    woke = r.result_ready.wait(0.20)   # stand-in short cadence
    elapsed = time.monotonic() - t0
    assert woke is False, "idle wait must time out, not wake"
    assert 0.18 <= elapsed <= 0.45, f"timed out near the cadence ({elapsed:.3f}s)"


def test_clear_before_snapshot_never_loses_a_verdict():
    """A result already reported clears its stale set; a result deposited
    AFTER the clear re-arms and cuts the next wait short. Mirrors the
    run()-loop ordering so a verdict landing during the POST is never
    stranded until the next full tick."""
    r = _reporter()
    r.record_result({"id": "reported", "ok": True})   # rides this snapshot
    r.result_ready.clear()                             # clear-before-snapshot
    assert not r.result_ready.is_set()
    r.record_result({"id": "landed-after-clear", "ok": True})
    assert r.result_ready.is_set(), "a post-clear verdict re-arms the wake"
    assert r.result_ready.wait(0.05) is True, "and cuts the wait short"
