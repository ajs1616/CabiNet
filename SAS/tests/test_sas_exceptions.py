"""
Tests for the typed exception-code table (core/sas_exceptions.py) and its
wiring into the poller's event path.

Authority: Montana DOJ SAS Implementation Guide v1.5.0 §3.1 (p.9) for the
code list, §4.6.2 for 0x3E. The table must contain EXACTLY the cited codes —
a fabricated code here would be a lie about the wire.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_exceptions import (
    BILL_DENOM_CENTS, EXCEPTIONS, EXCEPTION_NAMES, ExceptionCategory,
    SASExceptionInfo, exception_name, lookup,
)
from core.sas_poller import SASPoller
from transport.serial.sas_serial import MockSASSerialPort


# The guide §3.1 table, transcribed code-for-code (plus 3E from §4.6.2).
GUIDE_31_CODES = {
    0x00, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
    0x1B, 0x1C, 0x1D, 0x1E, 0x27, 0x28, 0x29, 0x2B, 0x3C, 0x3D, 0x3F,
    0x47, 0x48, 0x49, 0x4A, 0x60, 0x61, 0x70, 0x7A, 0x86, 0x8C,
}


class TestTableCompleteness:
    def test_exactly_the_cited_codes(self):
        """Every §3.1 code present; nothing invented beyond §3.1 + 0x3E +
        0x1F (spec §12.6 'no activity and waiting for player' — live-proven
        streaming from the BB2 in attract mode, 2026-07-08)."""
        assert set(EXCEPTIONS) == GUIDE_31_CODES | {0x3E, 0x1F}

    def test_no_fabricated_handpay_or_big_bill_codes(self):
        """The real-SAS handpay-pending and $50/$100-bill codes are NOT in
        the guide, so they must NOT be in the table (TODO(bench) markers in
        the module document the gap)."""
        for uncited in (0x51, 0x52, 0x4B, 0x4C):
            assert uncited not in EXCEPTIONS

    def test_backcompat_name_map_matches(self):
        assert EXCEPTION_NAMES[0x8C] == "game selected"
        assert EXCEPTION_NAMES[0x11] == "slot door opened"
        assert set(EXCEPTION_NAMES) == set(EXCEPTIONS)


class TestCategoriesAndEvents:
    def test_door_codes(self):
        for code in (0x11, 0x13, 0x15, 0x19, 0x1D):
            info = lookup(code)
            assert info.category is ExceptionCategory.DOOR
            assert info.event == "door_open"
        for code in (0x12, 0x14, 0x16, 0x1A, 0x1E):
            info = lookup(code)
            assert info.category is ExceptionCategory.DOOR
            assert info.event == "door_closed"

    def test_power_codes(self):
        assert lookup(0x17).event == "power_on"
        assert lookup(0x18).event == "power_lost"
        assert lookup(0x18).category is ExceptionCategory.POWER

    def test_bill_denominations_guide_cited(self):
        """§3.1 documents the denomination for 47/48/49/4A only."""
        assert BILL_DENOM_CENTS == {0x47: 100, 0x48: 500,
                                    0x49: 1000, 0x4A: 2000}
        info = lookup(0x49)
        assert info.event == "bill_inserted"
        assert info.denom_cents == 1000
        # bill_rejected carries NO denomination (guide doesn't give one)
        assert lookup(0x2B).denom_cents is None

    def test_handpay_validated_from_4_6_2(self):
        info = lookup(0x3E)
        assert info.category is ExceptionCategory.HANDPAY
        assert info.event == "handpay_validated"

    def test_ticket_and_game_codes(self):
        assert lookup(0x3D).event == "ticket_printed"
        assert lookup(0x3F).category is ExceptionCategory.TICKET
        assert lookup(0x8C).event == "game_selected"
        assert lookup(0x86).category is ExceptionCategory.GAME

    def test_unknown_code_is_visible_not_fatal(self):
        info = lookup(0xE9)
        assert info.category is ExceptionCategory.UNKNOWN
        assert info.event == "unknown_exception"
        assert info.code == 0xE9
        assert exception_name(0xE9) == "exception 0xE9"


class _OneShotMachine:
    """Minimal implied-ACK machine: serves each queued exception once (re-
    sends until ACKed by a poll to another address), then 0x00."""

    def __init__(self, address, codes):
        self.address = address
        self.fifo = list(codes)
        self.pending = None

    def __call__(self, frame, wakeup):
        is_general = len(frame) == 1
        polled = (frame[0] & 0x7F) if is_general else frame[0]
        if is_general and polled == self.address:
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            return bytes([self.pending]) if self.pending else b"\x00"
        self.pending = None          # any other poll = implied ACK
        return b""


class TestPollerTypedEvents:
    def test_typed_callback_receives_category_and_denom(self):
        machine = _OneShotMachine(1, [0x11, 0x48, 0x8C])
        port = MockSASSerialPort(machine)
        typed = []
        legacy = []
        poller = SASPoller(
            port, address=1,
            on_event=lambda a, c, n: legacy.append((a, c, n)),
            on_typed_event=lambda a, i: typed.append((a, i)))
        poller.poll_once()

        assert [i.event for _, i in typed] == \
            ["door_open", "bill_inserted", "game_selected"]
        assert typed[1][1].denom_cents == 500          # $5 bill
        assert all(isinstance(i, SASExceptionInfo) for _, i in typed)
        # legacy callback still fires in lockstep with identical codes
        assert [c for _, c, _ in legacy] == [0x11, 0x48, 0x8C]
        assert legacy[0][2] == "slot door opened"

    def test_typed_callback_isolated_like_legacy(self):
        machine = _OneShotMachine(1, [0x12])
        port = MockSASSerialPort(machine)

        def boom(*a):
            raise RuntimeError("typed cb blew up")

        poller = SASPoller(port, address=1, on_typed_event=boom)
        poller.poll_once()               # must not raise
        assert poller.state.callback_errors >= 1

    def test_unknown_code_flows_through_typed_path(self):
        machine = _OneShotMachine(1, [0xE9])
        port = MockSASSerialPort(machine)
        typed = []
        SASPoller(port, address=1,
                  on_typed_event=lambda a, i: typed.append(i)).poll_once()
        assert typed[0].category is ExceptionCategory.UNKNOWN
        assert typed[0].code == 0xE9
