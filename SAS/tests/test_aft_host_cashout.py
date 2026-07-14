"""
Tests for the SAS machine->wallet RETURN leg (glass destub v3, corrected
2026-07-12): the EGM_TO_HOST (0x80) host-cashout transfer + the READ-ONLY
0x74 host-cashout GATE PROBE, against the same scripted mock AFT machine the
rest of the AFT suite uses.

CONFIRMED model (AJ 2026-07-12): the machine is menu-set to "soft cash-out to
host, fail to ticket" and raises exception 0x6A on the CASH-OUT button; the
ARM is purely HUB-SIDE (sas_host.cashout_state) — NOT a wire write. The prior
build shipped a WRONG arm here: an LP 0x74 REQUEST/CANCEL game-LOCK that
locked the cabinet and auto-expired in ~5 s. Dropped. Load-bearing guards:

  * the EGM_TO_HOST transfer type is accepted + written at byte 5 of the 0x72;
  * a LOCKLESS EGM->host transfer completes with NO 0x74 lock dance;
  * set_host_cashout(True) does a READ-ONLY 0x74/FF interrogate (the gate
    probe — NO game-lock REQUEST/CANCEL, never a second serial writer);
  * set_host_cashout(False) touches NO wire at all (disarm is hub-side);
  * the machine-initiated 0x6A/0x6B host-cashout requests surface via
    AFTHost.on_cashout_request (the flag the between-polls handler reads).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_poller import SASPoller
from modules.aft.aft_handler import (
    AFT_CMD_LOCK_STATUS, AFTHost, AFTOutcome, AFTTransferRequest,
    LOCK_CODE_CANCEL, LOCK_CODE_INTERROGATE, LOCK_CODE_REQUEST,
    TRANSFER_CODE_FULL_ONLY, TRANSFER_FLAGS_NONE,
    TRANSFER_TYPE_EGM_TO_HOST, build_aft_transfer_poll,
)
from transport.serial.sas_serial import MockSASSerialPort

# Reuse the proven scripted machine + fixtures from the main AFT suite.
from test_aft_handler import AFTMachine, ASSET, KEY, NO_SLEEP, POS


def _locks(port):
    return [f for f, _ in port.sent_frames
            if len(f) > 1 and f[1] == AFT_CMD_LOCK_STATUS]


class TestEgmToHostReturn:
    """The machine->wallet return = a LOCKLESS EGM->host 0x72 (0x80)."""

    def test_transfer_type_written_at_byte_5(self):
        req = AFTTransferRequest(transaction_id="COTEST01",
                                 transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                                 cashable_cents=500)
        frame = build_aft_transfer_poll(1, req, ASSET, KEY)
        # [addr][72][len][transfer code][txn index=00][TRANSFER TYPE]...
        assert frame[5] == TRANSFER_TYPE_EGM_TO_HOST == 0x80

    def test_lockless_egm_to_host_completes_without_lock(self):
        machine = AFTMachine(registered=True, transfer_mode="immediate")
        machine.credited_cents = 500                 # $5 on the machine
        port = MockSASSerialPort(machine)
        host = AFTHost(SASPoller(port, address=1), ASSET, KEY, POS,
                       sleep=NO_SLEEP)
        req = AFTTransferRequest(transaction_id="COTEST02",
                                 transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                                 cashable_cents=500,
                                 transfer_code=TRANSFER_CODE_FULL_ONLY,
                                 transfer_flags=TRANSFER_FLAGS_NONE)
        res = host.transfer(req, require_lock=False)
        assert res.ok
        assert res.outcome is AFTOutcome.COMPLETED
        assert res.final is not None and res.final.cashable_cents == 500
        # THE point: a lockless return never issues a 0x74 lock request.
        assert _locks(port) == []


class TestHostCashoutArm:
    """set_host_cashout is a READ-ONLY gate probe — the real arm is hub-side.
    The prior build's game-LOCK (LP 0x74 REQUEST/CANCEL) is GONE (2026-07-12)."""

    def test_arm_does_a_readonly_interrogate_no_game_lock(self):
        machine = AFTMachine(registered=True)
        machine.credited_cents = 300
        port = MockSASSerialPort(machine)
        host = AFTHost(SASPoller(port, address=1), ASSET, KEY, POS,
                       sleep=NO_SLEEP)
        ls = host.set_host_cashout(True)
        probes = _locks(port)
        assert probes, "an LP 0x74 gate probe went out"
        # [addr][74][lock code][...] — READ-ONLY 0xFF interrogate, NOT a game
        # lock: it must never be a REQUEST (locks the cabinet) or CANCEL.
        assert probes[-1][2] == LOCK_CODE_INTERROGATE == 0xFF
        assert all(f[2] not in (LOCK_CODE_REQUEST, LOCK_CODE_CANCEL)
                   for f in probes)
        assert ls is not None                        # read-back status parsed

    def test_disarm_touches_no_wire(self):
        # Disarm is PURELY hub-side (clear the flag) — the CONFIRMED model
        # sends nothing on the wire; an unanswered 0x6A already tickets.
        machine = AFTMachine(registered=True)
        port = MockSASSerialPort(machine)
        host = AFTHost(SASPoller(port, address=1), ASSET, KEY, POS,
                       sleep=NO_SLEEP)
        ls = host.set_host_cashout(False)
        assert ls is None
        assert port.sent_frames == []                # not a single frame left

    def test_arm_is_a_single_reader_no_transfer_sent(self):
        # Arming must NEVER move money — only the read-only 0x74 probe leaves,
        # never a 0x72 transfer.
        machine = AFTMachine(registered=True)
        port = MockSASSerialPort(machine)
        host = AFTHost(SASPoller(port, address=1), ASSET, KEY, POS,
                       sleep=NO_SLEEP)
        host.set_host_cashout(True)
        transfers = [f for f, _ in port.sent_frames
                     if len(f) > 1 and f[1] == 0x72]
        assert transfers == []


class TestCashoutRequestSurface:
    """The machine's 0x6A/0x6B cash-out requests reach on_cashout_request —
    the flag the sas_host between-polls handler reads to answer EGM->host."""

    def test_6a_and_6b_surface(self):
        for code in (0x6A, 0x6B):
            machine = AFTMachine()
            machine.queue(code)
            poller = SASPoller(MockSASSerialPort(machine), address=1)
            seen = []
            AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP,
                    on_cashout_request=lambda a, c: seen.append((a, c)))
            poller.poll_once()
            assert seen == [(1, code)]

    def test_unrelated_exception_does_not_surface(self):
        machine = AFTMachine()
        machine.queue(0x11)                          # slot door — not a cashout
        poller = SASPoller(MockSASSerialPort(machine), address=1)
        seen = []
        AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP,
                on_cashout_request=lambda a, c: seen.append((a, c)))
        poller.poll_once()
        assert seen == []
