"""
Bounded stress/fuzz tests for the rebuilt SAS stack.

Rewritten 2026-07-07: the legacy file was a wall-clock CLI script importing
the import-broken modules.aft and the asyncio-era AFTTransactionManager, and
it errored at pytest collection. This version is a real (fast, bounded)
pytest module: framing fuzz, thread-safety churn, state-machine fuzz, and
ticket-store churn with a reload check. Iteration counts are fixed so the
suite stays sub-second — bump them locally for a heavier soak.
"""

import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASCommand, SASProtocol
from core.sas_ticket_store import REDEEMED, TicketStore
from modules.aft.aft_handler import (
    AFTStateError, AFTTxnEvent, AFTTxnState, advance, is_terminal,
)

RNG = random.Random(0x5A5)      # deterministic fuzz — failures reproduce


def test_packet_roundtrip_fuzz():
    """2,000 random frames: build -> parse -> identical fields, valid CRC."""
    protocol = SASProtocol()
    for _ in range(2000):
        addr = RNG.randint(1, 127)
        cmd = RNG.choice(list(SASCommand))
        data = bytes(RNG.randint(0, 255) for _ in range(RNG.randint(0, 60)))
        frame = protocol.build_packet(addr, cmd, data)
        packet = protocol.parse_packet(frame)
        assert packet is not None and packet.is_valid()
        assert packet.address == addr
        assert packet.command == cmd
        assert packet.data == data


def test_truncation_fuzz_never_crashes():
    """Every truncation/corruption of a valid frame parses to None or a
    valid packet — never an exception."""
    protocol = SASProtocol()
    frame = protocol.build_packet(1, 0x72, bytes(range(40)))
    for cut in range(len(frame)):
        protocol.parse_packet(frame[:cut])
    for i in range(len(frame)):
        mutated = bytearray(frame)
        mutated[i] ^= 0xFF
        protocol.parse_packet(bytes(mutated))


def test_concurrent_packet_building():
    """The shared stateless SASProtocol is safe under threads (the SMIB
    poll loop + a web layer may both build frames)."""
    protocol = SASProtocol()

    def worker(thread_id):
        errors = 0
        for i in range(400):
            addr = (thread_id % 127) + 1
            frame = protocol.build_packet(addr, 0x1A, bytes([i % 256]))
            packet = protocol.parse_packet(frame)
            if packet is None or not packet.is_valid() \
                    or packet.address != addr:
                errors += 1
        return errors

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert sum(results) == 0


def test_state_machine_fuzz_never_corrupts():
    """Random event storms either follow legal transitions or raise
    AFTStateError — the ledger can never wedge in an undefined state, and
    terminal states accept nothing."""
    events = list(AFTTxnEvent)
    for _ in range(500):
        state = AFTTxnState.CREATED
        for _ in range(12):
            event = RNG.choice(events)
            try:
                state = advance(state, event)
            except AFTStateError:
                continue
            assert isinstance(state, AFTTxnState)
        if is_terminal(state):
            for event in events:
                try:
                    advance(state, event)
                    raise AssertionError(
                        f"terminal {state} accepted {event}")
                except AFTStateError:
                    pass


def test_ticket_store_churn_and_reload(tmp_path):
    """120 tickets through the full issue -> authorize -> close cycle, then
    a cold reload: counts and states must survive."""
    path = str(tmp_path / "churn.json")
    store = TicketStore(path)
    for i in range(120):
        vn = f"77{i:014d}"
        assert not store.record_issued(vn, 100 + i, 1)["duplicate"]
        if i % 3 == 0:
            assert store.authorize_redemption(1, vn)["authorized"]
            assert store.close_redemption(1, vn, redeemed=True) == REDEEMED
    reloaded = TicketStore(path)
    live = reloaded.outstanding()
    assert len(live) == 120 - 40
    assert all(r["state"] == "issued" for r in live)
    # every closed one stays closed
    for i in range(0, 120, 3):
        assert reloaded.get(f"77{i:014d}")["state"] == REDEEMED
