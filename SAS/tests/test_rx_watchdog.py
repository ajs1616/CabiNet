"""
Tests for the RX-silence wedge watchdog (task #21, sas_host.RxWedgeWatchdog).

Context: a BB2 power-cycle deafens the Zero's PL011 RX while TX keeps
flowing — the machine answers every poll unheard until the serial port is
closed+reopened. Live-diagnosed 2026-07-10 (it even manufactured a false
"one protocol at a time" architecture conclusion before it was caught). The
watchdog reopens the port automatically after a bounded stretch of poll
silence; ANY valid answer resets it. Time is injected (monotonic seconds),
so these are fully deterministic — no sleeps, no real serial.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sas_host import (RxWedgeWatchdog, WEDGE_REOPEN_SEC, WEDGE_SLOW_AFTER,
                      WEDGE_SLOW_SEC)


class FakePort:
    """Minimal port stand-in: counts reopens, optionally raises."""
    def __init__(self, raise_times=0):
        self.reopens = 0
        self._raise_times = raise_times

    def reopen(self):
        self.reopens += 1
        if self._raise_times > 0:
            self._raise_times -= 1
            raise OSError("device busy")


class FakeState:
    """Stands in for SASPoller.MachineState — only .answers is read."""
    def __init__(self):
        self.answers = 0


def make(**kw):
    port, state = FakePort(kw.pop("raise_times", 0)), FakeState()
    return RxWedgeWatchdog(port, state, **kw), port, state


# -- arming -----------------------------------------------------------------

def test_first_tick_arms_never_insta_fires():
    wd, port, _ = make()
    # even a huge 'now' on the very first call must not reopen (nothing to
    # measure silence against yet)
    assert wd.tick(10_000.0) is False
    assert port.reopens == 0


# -- healthy link never reopens --------------------------------------------

def test_healthy_link_never_reopens():
    wd, port, state = make()
    t = 0.0
    wd.tick(t)                       # arm
    for _ in range(500):             # ~100s of polls at 0.2s
        t += 0.2
        state.answers += 1           # machine answered this poll
        assert wd.tick(t) is False
    assert port.reopens == 0
    assert wd.dry_reopens == 0


# -- the wedge: silence -> reopen at the fast cadence -----------------------

def test_wedge_reopens_after_silence():
    wd, port, state = make()
    wd.tick(0.0)                     # arm at t=0, answers frozen hereafter
    # just under the threshold: no reopen
    assert wd.tick(WEDGE_REOPEN_SEC - 0.01) is False
    assert port.reopens == 0
    # cross it: exactly one reopen
    assert wd.tick(WEDGE_REOPEN_SEC + 0.01) is True
    assert port.reopens == 1
    assert wd.dry_reopens == 1


def test_wedge_keeps_reopening_each_cadence():
    wd, port, state = make()
    t = 0.0
    wd.tick(t)
    for i in range(1, 6):
        t += WEDGE_REOPEN_SEC + 0.01
        assert wd.tick(t) is True
        assert port.reopens == i
    assert wd.dry_reopens == 5


# -- recovery: an answer after reopens resets everything --------------------

def test_answer_after_reopen_resets():
    wd, port, state = make()
    t = 0.0
    wd.tick(t)
    t += WEDGE_REOPEN_SEC + 0.01
    wd.tick(t)                       # reopen #1
    assert wd.dry_reopens == 1
    # machine heard again on the next poll
    t += 0.2
    state.answers += 1
    assert wd.tick(t) is False
    assert wd.dry_reopens == 0       # dry counter cleared
    # and the fast clock restarts: no immediate second reopen
    t += WEDGE_REOPEN_SEC - 0.5
    assert wd.tick(t) is False
    assert port.reopens == 1


# -- park: bump() suppresses reopen during intentional silence --------------

def test_bump_suppresses_reopen_while_parked():
    wd, port, state = make()
    wd.tick(0.0)
    # simulate a long parked stretch: bump() every 0.2s, answers frozen
    t = 0.0
    for _ in range(500):
        t += 0.2
        wd.bump(t)                   # parked = intentional silence
    assert port.reopens == 0
    # on resume, silence clock starts from the last bump, not the machine's
    # last answer — still needs a full cadence before firing
    assert wd.tick(t + WEDGE_REOPEN_SEC - 0.01) is False
    assert wd.tick(t + WEDGE_REOPEN_SEC + 0.01) is True
    assert port.reopens == 1


# -- backoff: after slow_after dry reopens, cadence slows --------------------

def test_backoff_to_slow_cadence():
    wd, port, state = make()
    t = 0.0
    wd.tick(t)
    # burn through the fast phase
    for _ in range(WEDGE_SLOW_AFTER):
        t += WEDGE_REOPEN_SEC + 0.01
        wd.tick(t)
    assert wd.dry_reopens == WEDGE_SLOW_AFTER
    assert port.reopens == WEDGE_SLOW_AFTER
    # now the fast cadence must NOT trigger another reopen
    t += WEDGE_REOPEN_SEC + 0.01
    assert wd.tick(t) is False
    assert port.reopens == WEDGE_SLOW_AFTER
    # only after the SLOW cadence does it reopen again
    t += (WEDGE_SLOW_SEC - WEDGE_REOPEN_SEC)
    assert wd.tick(t) is True
    assert port.reopens == WEDGE_SLOW_AFTER + 1


def test_recovery_from_slow_phase_restores_fast():
    wd, port, state = make()
    t = 0.0
    wd.tick(t)
    for _ in range(WEDGE_SLOW_AFTER + 1):
        t += WEDGE_SLOW_SEC + 0.01   # generous so both phases fire
        wd.tick(t)
    assert wd.dry_reopens >= WEDGE_SLOW_AFTER
    # machine comes back
    t += 0.2
    state.answers += 1
    wd.tick(t)
    assert wd.dry_reopens == 0
    # a fresh wedge now heals at the FAST cadence again
    assert wd.tick(t + WEDGE_REOPEN_SEC + 0.01) is True


# -- resilience: a reopen that raises is swallowed, still counts -------------

def test_reopen_exception_is_swallowed():
    wd, port, state = make(raise_times=1)
    t = 0.0
    wd.tick(t)
    t += WEDGE_REOPEN_SEC + 0.01
    # must not propagate the OSError
    assert wd.tick(t) is True
    assert port.reopens == 1
    assert wd.dry_reopens == 1
    # and it keeps trying on the next cadence (device came back)
    t += WEDGE_REOPEN_SEC + 0.01
    assert wd.tick(t) is True
    assert port.reopens == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
