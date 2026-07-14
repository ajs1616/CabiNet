"""
SAS two-step handpay reset-to-credits.

PROVENANCE — read this before trusting any byte in this module:

* THE TWO-STEP ORDER IS SETTLED FACT (AJ, 2026-07-01, from his own bench
  experience with real SAS machines — he is more confident on SAS than G2S):
  "SAS can reset to credit but it needs to send the byte prior to reset."
  Step 1 selects the handpay reset method (credit meter); step 2 issues the
  actual reset. A reset issued without the method selection first does not
  pay to the credit meter. This module structurally enforces the order:
  step 2 is unreachable unless step 1 was acked.

* THE POLL NUMBERS / FRAME LAYOUTS ARE TODO(bench). The Montana DOJ SAS
  Implementation Guide v1.5.0 (the only citable SAS document on disk) was
  searched 2026-07-01 and contains NO handpay-reset poll: no 0xA8, no 0x94,
  no reset-method anything. Its only handpay content is exception '3E'
  "hand pay has been validated" (§4.6.2 p.23) and two notes that Montana
  requires validation type 00 "no hand pay lockup" (§4.6.2 / §4.6.4 p.23-24).
  Every constant below is therefore a believed-correct value from the full
  IGT SAS spec's command space (not on disk), carried on AJ-experience
  provenance, and must be verified against a real machine before being
  treated as fact. Record the bench result in COMPATIBILITY.md.

Believed frames (all type S — CRC-bearing even without data, per the
Table 7.4a rule documented in sas_protocol.SASCommandBuilder):

  step 1  [addr][A8][method][crc-lo][crc-hi]   select handpay reset method
          method 0x00 = standard handpay (attendant), 0x01 = credit meter
          believed response: [addr][A8][ack][crc] with ack
            0x00 = method enabled, 0x01 = unable to enable,
            0x02 = not currently in a handpay condition
          (a bare address byte / address|0x80 is also accepted as the
          generic type-S implied ACK/NACK — ack semantics are exactly the
          part the on-disk guide cannot settle: TODO(bench))

  step 2  [addr][94][crc-lo][crc-hi]            remote handpay reset
          believed response: [addr][94][code][crc] with code
            0x00 = reset accepted, 0x01 = unable to reset
          (bare address ACK / address|0x80 NACK likewise accepted)

  confirm the machine reports the reset via a general-poll exception —
          believed code 0x52 "handpay was reset" (0x51 = "handpay is
          pending" is its believed sibling). NEITHER is in the Montana
          guide, and per sas_exceptions.py policy they are deliberately NOT
          added to the guide-cited EXCEPTIONS table; they live here as
          local TODO(bench) constants. Until bench-confirmed, a real 0x52
          still surfaces in the normal poll loop as an UNKNOWN exception —
          honest and visible.

No automatic retries by design: this is an operator-triggered action (fob
tap / bridge command), a silent machine yields a typed TIMEOUT and the
caller may simply re-invoke (re-selecting the method is idempotent). Every
poll is paced at SAS_POLL_FLOOR — the 200 ms per-machine floor.
"""

import enum
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .sas_protocol import SASProtocol
from .sas_meters import SAS_POLL_FLOOR

__all__ = [
    "CMD_SET_HANDPAY_RESET_METHOD", "CMD_REMOTE_HANDPAY_RESET",
    "RESET_METHOD_STANDARD", "RESET_METHOD_CREDIT_METER",
    "METHOD_ACK_ENABLED", "METHOD_ACK_UNABLE", "METHOD_ACK_NO_HANDPAY",
    "RESET_CODE_OK", "RESET_CODE_UNABLE",
    "HANDPAY_PENDING_EXCEPTION", "HANDPAY_RESET_EXCEPTION",
    "CONFIRM_EXCEPTIONS",
    "HandpayResetOutcome", "HandpayResetResult",
    "build_reset_method_poll", "build_handpay_reset_poll",
    "reset_handpay_to_credits",
]

# --------------------------------------------------------------------------
# Believed constants — ALL TODO(bench), AJ-experience provenance (2026-07-01).
# NOT cited by the on-disk Montana guide (searched: nothing).
# --------------------------------------------------------------------------

CMD_SET_HANDPAY_RESET_METHOD = 0xA8   # TODO(bench): believed long poll
CMD_REMOTE_HANDPAY_RESET = 0x94       # TODO(bench): believed long poll

RESET_METHOD_STANDARD = 0x00          # attendant/standard handpay
RESET_METHOD_CREDIT_METER = 0x01      # pay the lockup to the credit meter

# Believed 0xA8 response ack codes (TODO(bench))
METHOD_ACK_ENABLED = 0x00
METHOD_ACK_UNABLE = 0x01
METHOD_ACK_NO_HANDPAY = 0x02

# Believed 0x94 response codes (TODO(bench))
RESET_CODE_OK = 0x00
RESET_CODE_UNABLE = 0x01

# Believed general-poll exception codes (TODO(bench)); deliberately NOT in
# sas_exceptions.EXCEPTIONS (that table is guide-cited only — see its module
# docstring and test_no_fabricated_handpay_or_big_bill_codes).
HANDPAY_PENDING_EXCEPTION = 0x51
HANDPAY_RESET_EXCEPTION = 0x52
CONFIRM_EXCEPTIONS = frozenset({HANDPAY_RESET_EXCEPTION})

_PROTO = SASProtocol()                # stateless; safe to share


class HandpayResetOutcome(enum.Enum):
    """Typed result of a reset-to-credits attempt."""
    RESET_OK = "reset_ok"             # step 2 acked AND 0x52 confirmation seen
    METHOD_REFUSED = "method_refused"  # machine refused step 1 (or step 2)
    NO_HANDPAY = "no_handpay"         # machine says it is not in a handpay
    TIMEOUT = "timeout"               # silence, or acked-but-unconfirmed


@dataclass(frozen=True)
class HandpayResetResult:
    outcome: HandpayResetOutcome
    step1_acked: bool                 # method selection acknowledged
    step2_sent: bool                  # reset poll actually left the host
    step2_code: Optional[int]         # 0x94 response code byte, if one came
    confirmed_by_exception: bool      # 0x52-family exception observed
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is HandpayResetOutcome.RESET_OK


# --------------------------------------------------------------------------
# Frame builders
# --------------------------------------------------------------------------

def build_reset_method_poll(address: int,
                            method: int = RESET_METHOD_CREDIT_METER) -> bytes:
    """Step 1 — select handpay reset method: [addr][A8][method][crc].
    TODO(bench): poll number and layout are believed, uncited on disk."""
    if method not in (RESET_METHOD_STANDARD, RESET_METHOD_CREDIT_METER):
        raise ValueError(f"unknown handpay reset method 0x{method:02X}")
    return _PROTO.build_packet(address, CMD_SET_HANDPAY_RESET_METHOD,
                               bytes([method]))


def build_handpay_reset_poll(address: int) -> bytes:
    """Step 2 — remote handpay reset: [addr][94][crc]. Type S: CRC even with
    no data. TODO(bench): poll number is believed, uncited on disk."""
    return _PROTO.build_packet(address, CMD_REMOTE_HANDPAY_RESET)


# --------------------------------------------------------------------------
# Response classification
# --------------------------------------------------------------------------

def _classify(resp: bytes, address: int, command: int,
              proto: SASProtocol) -> tuple:
    """Classify a step response -> (kind, code, detail).

    kind: 'ack' | 'refused' | 'no_handpay' | 'silence'
    code: the response's status byte, when the machine sent a full frame.

    Tolerant on the ack SHAPE (full [addr][cmd][code][crc] frame, bare
    address implied ACK, or address|0x80 NACK) because that shape is exactly
    what the bench must settle — but never tolerant on a bad CRC or a frame
    from the wrong address/command: those are treated as silence, not as
    consent to fire a reset."""
    if not resp:
        return "silence", None, "no response"
    if len(resp) == 1:
        v = resp[0]
        if v == address:
            return "ack", None, "bare-address implied ACK"
        if v == (address | 0x80):
            return "refused", None, "address|0x80 NACK"
        return "silence", None, f"unclassifiable lone byte 0x{v:02X}"
    if len(resp) == 2 and resp[1] == 0x00:
        return "silence", None, "machine busy ([addr][00])"
    packet = proto.parse_packet(resp)
    if packet is None or packet.address != address \
            or packet.command != command or len(packet.data) < 1:
        return "silence", None, "corrupt/foreign frame dropped"
    code = packet.data[0]
    if command == CMD_SET_HANDPAY_RESET_METHOD:
        if code == METHOD_ACK_ENABLED:
            return "ack", code, "method enabled (ack 0x00)"
        if code == METHOD_ACK_NO_HANDPAY:
            return "no_handpay", code, "not in a handpay condition (ack 0x02)"
        return "refused", code, f"method refused (ack 0x{code:02X})"
    # CMD_REMOTE_HANDPAY_RESET
    if code == RESET_CODE_OK:
        return "ack", code, "reset accepted (code 0x00)"
    return "refused", code, f"reset refused (code 0x{code:02X})"


# --------------------------------------------------------------------------
# The two-step sequence
# --------------------------------------------------------------------------

def reset_handpay_to_credits(transport, address: int,
                             protocol: Optional[SASProtocol] = None,
                             method: int = RESET_METHOD_CREDIT_METER,
                             pace: float = SAS_POLL_FLOOR,
                             sleep: Callable[[float], None] = time.sleep,
                             confirm_polls: int = 5,
                             on_exception: Optional[Callable[[int], None]] = None,
                             on_frame: Optional[Callable[[bytes], None]] = None
                             ) -> HandpayResetResult:
    """Run the two-step reset-to-credits sequence against one machine.

      1. select reset method (0xA8, method byte) — REQUIRE an ack;
      2. only then issue the remote handpay reset (0x94);
      3. confirm via the believed 'handpay was reset' exception (0x52)
         off the general-poll FIFO.

    THE ORDER IS THE FEATURE (AJ bench fact): step 2 is never sent unless
    step 1 was acked — silence, a NACK, a busy, or a corrupt frame on step 1
    all end the sequence with a typed result and NO reset on the wire.

    Pacing: `sleep(pace)` (default SAS_POLL_FLOOR, the 200 ms per-machine
    floor) runs between consecutive polls. Inject a no-op sleep only in mock
    tests. No automatic retries — see the module docstring.

    on_exception(code): non-confirmation exceptions drained during the
    confirm window (door events etc.) are handed here so a wrapping poller
    can dispatch them normally instead of losing them.
    on_frame(resp): a multi-byte pending long-poll result (guide §3 p.8,
    e.g. a ROM signature) arriving during the confirm window is handed here
    raw — SASPoller routes it through its _handle_long_poll_result.

    Long polls double as the implied ACK for a prior general-poll exception
    (guide §3), so running this inside a poll loop is protocol-safe.
    """
    proto = protocol or _PROTO

    # -- step 1: select the reset method — REQUIRE ack ----------------------
    resp = transport.transact(build_reset_method_poll(address, method))
    kind, code, detail = _classify(resp, address,
                                   CMD_SET_HANDPAY_RESET_METHOD, proto)
    if kind == "no_handpay":
        return HandpayResetResult(HandpayResetOutcome.NO_HANDPAY,
                                  False, False, None, False,
                                  f"step 1: {detail}")
    if kind == "refused":
        return HandpayResetResult(HandpayResetOutcome.METHOD_REFUSED,
                                  False, False, None, False,
                                  f"step 1: {detail}")
    if kind != "ack":
        return HandpayResetResult(HandpayResetOutcome.TIMEOUT,
                                  False, False, None, False,
                                  f"step 1: {detail}; reset NOT sent")

    # -- step 2: the reset — reachable ONLY through the acked branch --------
    sleep(pace)
    resp = transport.transact(build_handpay_reset_poll(address))
    kind, code, detail = _classify(resp, address,
                                   CMD_REMOTE_HANDPAY_RESET, proto)
    if kind == "refused":
        return HandpayResetResult(HandpayResetOutcome.METHOD_REFUSED,
                                  True, True, code, False,
                                  f"step 2: {detail}")
    if kind != "ack":
        return HandpayResetResult(HandpayResetOutcome.TIMEOUT,
                                  True, True, code, False,
                                  f"step 2: {detail}")

    # -- confirm: watch the exception FIFO for the reset report -------------
    confirmed = _await_confirmation(transport, address, proto, pace, sleep,
                                    confirm_polls, on_exception, on_frame)
    if confirmed:
        return HandpayResetResult(HandpayResetOutcome.RESET_OK,
                                  True, True, code, True,
                                  "reset acked and confirmed by exception "
                                  f"0x{HANDPAY_RESET_EXCEPTION:02X}")
    return HandpayResetResult(HandpayResetOutcome.TIMEOUT,
                              True, True, code, False,
                              "step 2 acked but no handpay-reset exception "
                              f"within {confirm_polls} polls — verify meters "
                              "before treating the reset as done")


def _await_confirmation(transport, address, proto, pace, sleep,
                        confirm_polls, on_exception, on_frame) -> bool:
    """General-poll (paced) until the believed 0x52 confirmation, honoring
    implied-ACK semantics: every non-zero exception read here is ACKed with
    an address-0 poll (a poll to a different address) so the machine does
    not re-send it to the main poll loop afterwards."""
    for _ in range(confirm_polls):
        sleep(pace)
        resp = transport.transact(proto.build_general_poll(address))
        if not resp:
            continue
        if len(resp) > 1:
            # pending long-poll result delivered instead of an exception
            # (guide §3 p.8) — hand it up rather than losing it
            if on_frame is not None:
                on_frame(resp)
            continue
        code = resp[0]
        if code == 0x00:
            continue
        # ACK the exception before anything else (implied-ACK rule)
        transport.transact(proto.build_general_poll(0))
        if code in CONFIRM_EXCEPTIONS:
            return True
        if on_exception is not None:
            on_exception(code)
    return False
