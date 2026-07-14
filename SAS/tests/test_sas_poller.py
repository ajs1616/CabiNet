"""
Tests for the SAS poller engine (SMIB core loop) using a scripted mock
machine. No hardware, no real time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller, MultiMachinePoller, exception_name
from transport.serial.sas_serial import MockSASSerialPort


class ScriptedMachine:
    """A pretend SAS machine with an exception FIFO and a credits meter, that
    models the real IMPLIED-ACK semantics: an exception stays 'pending' and is
    RE-SENT on a repeated general poll (implied NACK) until the host ACKs it
    with a long poll to this machine OR a poll to a different address. Without
    this fidelity the tests would pass against the wrong drain logic — the
    project's historical failure mode."""

    def __init__(self, address=1, credits=4200, require_sync=False):
        self.address = address
        self.fifo = []           # queued exception codes
        self.credits = credits
        self.proto = SASProtocol()
        self.pending = None      # exception sent but not yet ACKed
        self.require_sync = require_sync   # ignore self-polls until synced
        self.synced = not require_sync
        self.general_polls = 0   # total general polls to THIS address

    def queue(self, *codes):
        self.fifo.extend(codes)

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""           # not addressed with wake-up bit
        is_general = len(frame) == 1
        polled = (frame[0] & 0x7F) if is_general else frame[0]

        if is_general and polled == self.address:
            self.general_polls += 1
            if self.require_sync and not self.synced:
                return b""       # §3.3: ignore self-polls until we see another
            if self.pending is not None:
                return bytes([self.pending])      # implied NACK -> re-send
            self.pending = self.fifo.pop(0) if self.fifo else 0x00
            if self.pending == 0x00:
                self.pending = None
                return b"\x00"
            return bytes([self.pending])

        # any other poll (different address, incl. addr 0, or a long poll to
        # us) ACKs our pending exception and re-syncs us
        self.synced = True
        if self.pending is not None:
            self.pending = None
        if not is_general and frame[0] == self.address and frame[1] == 0x1A:
            body = bytes([self.address, 0x1A]) + self.proto._int_to_bcd(self.credits, 4)
            return body + sas_crc(body).to_bytes(2, "little")
        return b""               # poll to addr 0, or unsupported -> silence


def manual_clock():
    """A controllable monotonic clock."""
    t = {"now": 0.0}
    def clock():
        return t["now"]
    clock.advance = lambda dt: t.__setitem__("now", t["now"] + dt)
    return clock


class TestSASPoller:
    def test_online_on_first_answer(self):
        machine = ScriptedMachine(address=1)
        port = MockSASSerialPort(machine)
        online = []
        poller = SASPoller(port, address=1, on_online=lambda a: online.append(a))

        assert poller.state.online is False
        poller.poll_once()
        assert poller.state.online is True
        assert online == [1]

    def test_exception_dispatched_and_named(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12, 0x8C)  # slot door closed, game selected
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append((a, c, n)))

        poller.poll_once()  # pops 0x12, ACK, drains 0x8C, ACK, then 0x00
        codes = [c for _, c, _ in events]
        assert codes == [0x12, 0x8C]   # each dispatched exactly ONCE
        assert exception_name(0x8C) == "game selected"

    def test_busy_nack_chirp_not_dispatched(self):
        # addr|0x80 (Table 7.4b busy/NACK). Live-seen 2026-07-10 on the WMS
        # BB2 at addr 1: a ~1 s run of 0x81 answers was dispatched as 21
        # phantom "exception 0x81" events on the floor UI. The poller must
        # count the chirps (state.nacks), never dispatch, never FIFO-drain —
        # and a chirping machine is an ANSWERING machine (stays online).
        machine = ScriptedMachine(address=1)
        machine.queue(0x81, 0x81, 0x81)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        for _ in range(3):
            poller.poll_once()
        assert events == []
        assert poller.state.nacks == 3
        assert poller.state.online is True

    def test_nack_byte_is_address_relative(self):
        # The guard is addr|0x80, not a blanket 0x81: from a machine at
        # address 2 the byte 0x81 is NOT its NACK (0x82 would be) and must
        # still dispatch as a real exception.
        machine = ScriptedMachine(address=2)
        machine.queue(0x81)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=2,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        poller.poll_once()
        assert [c for _, c, _ in events] == [0x81]
        assert poller.state.nacks == 0

    def test_implied_ack_no_duplicate_events(self):
        """Against a machine that re-sends until ACKed (real semantics), the
        ACK-aware drain must dispatch each exception exactly once — a naive
        re-poll drain would loop on 0x12 forever."""
        machine = ScriptedMachine(address=1)
        machine.queue(0x11, 0x17, 0x8C)
        port = MockSASSerialPort(machine)
        events = []
        SASPoller(port, address=1,
                  on_event=lambda a, c, n: events.append(c)).poll_once()
        assert [c for c in events] == [0x11, 0x17, 0x8C]

    def test_sync_poll_before_first_real_poll(self):
        """A machine that enforces §3.3 (ignores self-polls until it sees a
        poll to another address) still gets read, because the poller leads
        with an address-0 poll."""
        machine = ScriptedMachine(address=1, require_sync=True)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert poller.state.online is True   # the address-0 poll synced it
        assert 0x12 in events
        # and an address-0 (0x80) general poll was actually sent
        assert any(f == b"\x80" for f, _ in port.sent_frames)

    def test_callback_exception_does_not_kill_loop(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        def boom(*a):
            raise RuntimeError("bad callback")
        poller = SASPoller(port, address=1, on_event=boom, on_online=boom)
        poller.poll_once()                   # must not raise
        assert poller.state.callback_errors >= 1
        assert poller.state.online is True

    def test_meter_sweep_reads_credits(self):
        machine = ScriptedMachine(address=1, credits=1234)
        port = MockSASSerialPort(machine)
        meters = []
        clock = manual_clock()
        poller = SASPoller(port, address=1, meter_interval=5.0, clock=clock,
                           on_meter=lambda a, c, v: meters.append((c, v)))

        poller.poll_once()                      # first sweep happens at t=0
        assert poller.state.meters.get("current_credits") == 1234
        assert (0x1A, 1234) in meters

    def test_meter_interval_throttles(self):
        machine = ScriptedMachine(address=1, credits=500)
        port = MockSASSerialPort(machine)
        meters = []
        clock = manual_clock()
        poller = SASPoller(port, address=1, meter_interval=5.0, clock=clock,
                           on_meter=lambda a, c, v: meters.append(v))

        poller.poll_once()           # sweep at t=0
        clock.advance(1.0)
        poller.poll_once()           # too soon, no sweep
        assert len(meters) == 1
        clock.advance(5.0)
        machine.credits = 999
        poller.poll_once()           # now due
        assert meters[-1] == 999

    def test_offline_after_consecutive_misses(self):
        machine = ScriptedMachine(address=1)
        port = MockSASSerialPort(machine)
        offline = []
        poller = SASPoller(port, address=1, offline_after=3,
                           on_offline=lambda a: offline.append(a))

        poller.poll_once()                       # online
        assert poller.state.online
        # Now make the machine deaf (wrong address responds to nobody)
        machine.address = 99
        for _ in range(3):
            poller.poll_once()
        assert poller.state.online is False
        assert offline == [1]

    def test_rom_signature_result_via_general_poll(self):
        """Guide §4.3 (p.11) / §3 (p.8): a pending 0x21 ROM signature result
        answers a LATER general poll as a full '[addr][21][sig][sig][crc]'
        frame, sent INSTEAD of an exception code — and the VGM erases it as
        soon as the next poll implies the ACK. The poller must capture it as
        a long-poll result, not dispatch resp[0] (the address byte) as a
        phantom exception 0x01."""
        body = bytes([1, 0x21, 0xAB, 0xCD])
        pending = [body + sas_crc(body).to_bytes(2, "little")]

        def machine(frame, wakeup):
            if not wakeup[0]:
                return b""
            if len(frame) == 1 and (frame[0] & 0x7F) == 1:
                return pending.pop(0) if pending else b"\x00"
            return b""                    # addr-0 polls, long polls: silence

        port = MockSASSerialPort(machine)
        events, results = [], []
        poller = SASPoller(port, address=1, meters=[],
                           on_event=lambda a, c, n: events.append(c),
                           on_poll_result=lambda a, c, v:
                               results.append((a, c, v)))
        code = poller.poll_once()
        assert results == [(1, 0x21, b"\xAB\xCD")]
        assert poller.state.poll_results[0x21] == b"\xAB\xCD"
        assert events == []               # no phantom exception dispatched
        assert code == 0x00               # nothing came off the event FIFO
        assert poller.state.online is True

    def test_corrupt_general_poll_frame_is_dropped(self):
        """A multi-byte general-poll answer whose CRC fails must be dropped,
        not recorded as a result or dispatched as an exception."""
        good = bytes([1, 0x21, 0xAB, 0xCD])
        bad = good + b"\x00\x00"          # wrong CRC
        pending = [bad]

        def machine(frame, wakeup):
            if len(frame) == 1 and (frame[0] & 0x7F) == 1:
                return pending.pop(0) if pending else b"\x00"
            return b""

        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1, meters=[],
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert poller.state.poll_results == {}
        assert events == []

    def test_run_bounded(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        poller.run(interval=0, max_polls=10, sleep=lambda _: None)
        assert poller.state.polls >= 10
        assert poller.state.answers >= 10


class TestMultiMachine:
    def test_round_robin_two_machines(self):
        # Two machines share one transport; the mock routes by address.
        m1 = ScriptedMachine(address=1)
        m2 = ScriptedMachine(address=2)
        m2.queue(0x17)  # AC power applied on machine 2

        def router(frame, wakeup):
            r1 = m1(frame, wakeup)
            return r1 if r1 else m2(frame, wakeup)

        port = MockSASSerialPort(router)
        events = []
        mp = MultiMachinePoller(port, [1, 2],
                                on_event=lambda a, c, n: events.append((a, c)))
        mp.poll_round()
        assert mp.states[1].online and mp.states[2].online
        assert (2, 0x17) in events
