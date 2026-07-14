"""
Tests for the SAS 9-bit wakeup serial transport (mock-based — real-hardware
validation happens on the Pi against a live machine).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from transport.serial.sas_serial import MockSASSerialPort


class TestWakeupFraming:
    def setup_method(self):
        self.protocol = SASProtocol()

    def test_wakeup_set_on_first_byte_only(self):
        """The 9th bit must be set on byte 0 and clear on all others."""
        port = MockSASSerialPort()
        frame = self.protocol.build_packet(0x01, 0x21, b"\x00\x00")
        port.send_frame(frame)

        sent, wakeup = port.sent_frames[0]
        assert sent == frame
        assert wakeup[0] is True
        assert all(w is False for w in wakeup[1:])

    def test_general_poll_is_one_wakeup_byte(self):
        port = MockSASSerialPort()
        port.send_frame(self.protocol.build_general_poll(0x01))

        sent, wakeup = port.sent_frames[0]
        assert sent == b"\x81"
        assert wakeup == [True]

    def test_transact_round_trip(self):
        """A mock machine at address 1 answers a current-credits short poll."""
        protocol = self.protocol

        def machine(frame, wakeup):
            # Machine logic: only respond if addressed with wake-up bit set
            if not wakeup[0]:
                return b""
            if frame == b"\x01\x1A":  # current credits short poll
                body = b"\x01\x1A" + b"\x00\x00\x12\x34"
                return body + sas_crc(body).to_bytes(2, "little")
            return b""

        port = MockSASSerialPort(machine)
        resp = port.transact(protocol.build_short_poll(0x01, 0x1A))

        parsed = protocol.parse_packet(resp)
        assert parsed is not None
        assert parsed.address == 0x01
        assert parsed.command == 0x1A
        assert protocol._bcd_to_int(parsed.data) == 1234

    def test_machine_ignores_frame_without_wakeup(self):
        """A frame whose first byte lacks the wake-up bit gets silence —
        this is exactly what the old (parity-less) implementation caused."""
        def machine(frame, wakeup):
            return b"\x00" if wakeup[0] else b""

        port = MockSASSerialPort(machine)
        # Simulate the old bug: pretend no wakeup flag on any byte
        port.sent_frames.append((b"\x81", [False]))
        assert machine(b"\x81", [False]) == b""
        # And the correct path answers
        assert port.transact(b"\x81") == b"\x00"
