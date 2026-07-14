"""
SAS legacy bonusing (spec §13) — the 0x8A "initiate legacy bonus pay" poll.

LIVE-PROVEN on the WMS BB2 2026-07-08 (the first host->machine money movement
in this project): frame `01 8A 00 00 00 10 00 E8 26` credited 10 credits
($0.10 at the machine's 1c denomination), the machine ACKed with its bare
address byte, and the 0x1A current-credits meter read back 0x10 BCD. No AFT
registration is required for legacy bonusing (§13 is independent of §8).

Layout (Type S; §13.3 is outside our on-disk spec excerpt — field order
adjudicated against marcrdavis/ArduinoTITO-PlayerTracking's LegacyBonus(),
a read-only oracle proven on real WMS iron, then live-proven here):

    [address][8A][amount 4 BCD, MSB first, in CREDITS][tax status][CRC lo hi]

Tax status byte (believed values from the full spec; only 0x00 bench-proven):
    00 = deductible (standard)   01 = non-deductible   02 = wager match

ACK/NACK per spec §2.2.2.2 (Type S that carries no data request): the
machine ACKs by transmitting its bare address byte, NACKs with address|0x80,
or stays silent if the frame never arrived intact.

UNITS: CREDITS of the machine's accounting denomination, NOT cents — a
10-credit bonus on a 1c machine is $0.10. Callers doing money math must
convert at the protocol edge (hub stores millicents; see hub_store).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sas_protocol import sas_crc
from core.sas_meters import int_to_bcd

__all__ = [
    "LEGACY_BONUS_CMD", "TAX_DEDUCTIBLE", "TAX_NON_DEDUCTIBLE",
    "TAX_WAGER_MATCH", "MAX_BONUS_CREDITS",
    "build_legacy_bonus_poll", "classify_bonus_reply",
]

LEGACY_BONUS_CMD = 0x8A

TAX_DEDUCTIBLE = 0x00      # bench-proven
TAX_NON_DEDUCTIBLE = 0x01  # VERIFY_ON_BENCH
TAX_WAGER_MATCH = 0x02     # VERIFY_ON_BENCH

# Hard cap on a single award = the 4-byte-BCD WIRE MAX (99,999,999 credits) —
# protocol physics, not policy. A home game room awards whatever it likes
# (collector de-cage, 2026-07-15; the old 10,000-credit "$100" value was a
# casino marketing knob). Fat-finger protection is the UI's confirm tap, not
# an artificial ceiling.
MAX_BONUS_CREDITS = 99_999_999


def build_legacy_bonus_poll(address: int, credits: int,
                            tax_status: int = TAX_DEDUCTIBLE) -> bytes:
    """Build the 0x8A initiate-legacy-bonus frame (CRC included)."""
    if not 1 <= address <= 0x7F:
        raise ValueError(f"address {address} out of range 1..0x7F")
    if not isinstance(credits, int) or isinstance(credits, bool):
        raise ValueError("credits must be an int")
    if not 1 <= credits <= MAX_BONUS_CREDITS:
        raise ValueError(
            f"credits {credits} out of range 1..{MAX_BONUS_CREDITS}")
    if tax_status not in (TAX_DEDUCTIBLE, TAX_NON_DEDUCTIBLE,
                          TAX_WAGER_MATCH):
        raise ValueError(f"unknown tax status 0x{tax_status:02X}")
    body = (bytes([address, LEGACY_BONUS_CMD])
            + int_to_bcd(credits, 4)
            + bytes([tax_status]))
    return body + sas_crc(body).to_bytes(2, "little")


def classify_bonus_reply(reply: bytes, address: int) -> str:
    """Machine's reply to 0x8A -> 'ack' | 'nack' | 'busy' | 'silence' |
    'unexpected'. Per §2.2.2.2 (ACK = bare address, NACK = address|0x80)
    and §4.1 (busy = address + 0x00 command code)."""
    if not reply:
        return "silence"
    if reply == bytes([address]):
        return "ack"
    if reply == bytes([address | 0x80]):
        return "nack"
    if reply == bytes([address, 0x00]):
        return "busy"
    return "unexpected"
