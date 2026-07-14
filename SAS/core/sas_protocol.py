"""
SAS Protocol Core Implementation
Handles SAS protocol parsing, validation, and command processing

Wire facts (Montana DOJ SAS Implementation Guide v1.5.0 — the supplemental
guide at the repo root; full authority is the IGT SAS spec):
  * 19,200 baud; 1 start bit, 8 data bits, a WAKE-UP (9th) bit, 1 stop bit.
    The wake-up bit is set ONLY on the first byte of a host message (the
    address byte) and cleared for the rest; machines respond with it cleared.
    On Linux, emulate with mark/space parity (CMSPAR): MARK parity for the
    address byte, SPACE for the remainder — see transport/serial.
  * Frame = [address][command][data...][crc-lo][crc-hi]. There is NO sync
    byte in SAS — the first byte is the address byte. (A previous version of
    this module prepended/stripped a fictitious 0x01 "sync byte", which
    corrupted any frame from a machine at address 1.)
  * Two-byte polls (e.g. `01 1A`) carry NO CRC. Data-bearing polls and all
    multi-byte responses end with the 16-bit CRC.
  * The general poll is a single byte: 0x80 | address (wake-up bit set at the
    serial layer); the machine answers with one exception-code byte, no CRC.
  * A machine has 20 ms to start its response before the host may move on.
"""

import struct
import enum
from typing import Dict, Tuple, Optional, List, Union
from dataclasses import dataclass
from datetime import datetime
import crcmod
from loguru import logger

# SAS CRC: CRC-16/Kermit — reflected CCITT, poly 0x1021 (reversed 0x8408),
# init 0x0000, no final XOR, transmitted LSB FIRST on the wire.
# Canonical check value: sas_crc(b"123456789") == 0x2189.
# (The previous rev=False/MSB-first form was the XMODEM variant — wrong for
# SAS; a real machine silently ignores frames with a bad CRC per the
# unsupported-command rule, so this bug looked like "machine not responding".)
# Final adjudication: validate against a live capture from a real WMS machine
# on the bench and record the result in COMPATIBILITY.md.
sas_crc = crcmod.mkCrcFun(0x11021, rev=True, initCrc=0x0000, xorOut=0x0000)

# Link constants (Montana guide p.6)
SAS_BAUD = 19200
SAS_RESPONSE_TIMEOUT_MS = 20
SAS_MIN_ADDRESS = 1
SAS_MAX_ADDRESS = 127
GENERAL_POLL_FLAG = 0x80

class SASCommand(enum.IntEnum):
    """SAS long-poll command codes.

    The meter/info block (0x0F-0x56) was rebuilt 2026-06-12 strictly from the
    Montana DOJ SAS Implementation Guide v1.5.0 §4.3-4.6 (the prior values were
    systematically shifted — e.g. 0x11 was labeled 'Total Drop' when the guide
    defines it as Coin In). Codes marked [guide] are cited to that document;
    [SAS] are standard SAS codes the guide doesn't define (control/AFT region,
    kept for the existing handlers — verify against the full IGT spec / a bench
    capture before relying on them).

    NOTE on namespaces: a byte like 0x11 is a METER here but a door EXCEPTION in
    a general-poll response (see sas_exceptions.EXCEPTIONS). Different tables;
    don't conflate."""

    # --- control / no-data type S polls (0x00-0x07) [SAS] -----------------
    POLL = 0x00                      # general poll sentinel (not a long poll)
    SHUTDOWN = 0x01
    STARTUP = 0x02
    SOUND_OFF = 0x03
    SOUND_ON = 0x04
    REEL_SPIN = 0x05
    ENABLE_BILL_ACCEPTOR = 0x06
    DISABLE_BILL_ACCEPTOR = 0x07

    # --- meters & machine info (type R/M reads) [guide §4.3-4.5] ----------
    SEND_METERS_10_15 = 0x0F         # composite: cancelled/coinin/coinout/drop/jp/played
    CANCELLED_CREDITS = 0x10         # jurisdictional cancelled credit meter
    COIN_IN = 0x11
    COIN_OUT = 0x12
    TOTAL_DROP = 0x13
    GAMES_PLAYED = 0x15
    GAMES_WON = 0x16
    GAMES_LOST = 0x17
    GAMES_SINCE_POWERUP_DOORCLOSE = 0x18
    SEND_METERS_11_15 = 0x19         # composite: coinin/coinout/drop/jp/played
    CURRENT_CREDITS = 0x1A
    BILL_METERS = 0x1E               # # of each bill denom accepted
    GAMING_MACHINE_ID = 0x1F         # game ID + info (ASCII; was wrongly 0x4F)
    DOLLAR_VALUE_OF_BILLS = 0x20
    ROM_SIGNATURE = 0x21
    BILLS_IN_1 = 0x31                # $1 bills in meter
    BILLS_IN_2 = 0x32                # $2
    BILLS_IN_5 = 0x33
    BILLS_IN_10 = 0x34
    BILLS_IN_20 = 0x35
    BILLS_IN_50 = 0x36
    BILLS_IN_100 = 0x37
    CREDIT_AMOUNT_OF_BILLS = 0x46
    LAST_ACCEPTED_BILL_INFO = 0x48
    SAS_VERSION_AND_SERIAL = 0x54
    SEND_SELECTED_METERS_FOR_GAME = 0x6F
    EXTENDED_VALIDATION_STATUS = 0x7B
    SEND_DATE_TIME = 0x7E
    RECEIVE_DATE_TIME = 0x7F         # host->VGM set; response is bare address
    SEND_SELECTED_METERS_GAME_N = 0x2F  # [guide §4.5.1] type M, 1-byte codes
    SEND_TOTAL_GAMES_IMPLEMENTED = 0x51
    SEND_GAME_N_METERS = 0x52
    SEND_GAME_N_CONFIG = 0x53
    SEND_SELECTED_GAME_NUMBER = 0x55
    SEND_ENABLED_GAME_NUMBERS = 0x56
    SEND_ENABLED_FEATURES = 0xA0     # [guide §4.4.28] (was wrongly 0x1E)

    # --- validation / TITO [guide §4.6] -----------------------------------
    SET_ENHANCED_VALIDATION_ID = 0x4C
    SEND_ENHANCED_VALIDATION_INFO = 0x4D
    SEND_CASH_OUT_TICKET_INFO = 0x3D
    SEND_VALIDATION_METERS = 0x50

    # --- AFT (type S/M) [SAS — not in the Montana guide] ------------------
    # VERIFY_ON_BENCH — reconciled 2026-07-07 during the AFT rebuild. The
    # previous block here was SHIFTED one code (0x72=register, 0x75=transfer
    # funds — the same systematic-shift failure mode as the pre-2026-06-12
    # meter table), which is where the old aft_handler's "docstring says
    # 0x74, code sends 0x75" conflict came from. Believed-correct mapping
    # from the full SAS spec's AFT chapter (not on disk):
    #   0x72 = AFT transfer funds (initiate / cancel / interrogate via the
    #          transfer-code byte),
    #   0x73 = AFT register gaming machine,
    #   0x74 = AFT game lock and status request (this doubles as the
    #          "interrogate" poll).
    #   0x75 = Set AFT Receipt Data (spec §8.11.1) — host->EGM config,
    #   0x76 = Set Custom AFT Ticket Data (spec §8.12) — host->EGM config;
    #          both REAL (spec TOC + change-log confirm) but not implemented
    #          yet — see modules/aft/aft_handler.py header. Host cashout is
    #          EGM-initiated via exceptions 0x6A/0x6B, not a poll.
    # modules/aft/aft_handler.py owns the detailed frame layouts and status
    # codes; adjudicate against a live AFT-capable machine and record in
    # COMPATIBILITY.md.
    AFT_TRANSFER_FUNDS = 0x72
    AFT_REGISTER_GAMING_MACHINE = 0x73
    AFT_GAME_LOCK_AND_STATUS = 0x74

@dataclass
class SASPacket:
    """Represents a parsed SAS packet"""
    address: int
    command: int
    data: bytes
    crc: int
    raw: bytes
    
    def is_valid(self) -> bool:
        """Validate CRC"""
        # Calculate CRC on address + command + data
        calc_data = bytes([self.address, self.command]) + self.data
        calculated_crc = sas_crc(calc_data)
        return calculated_crc == self.crc
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'address': f"{self.address:02X}",
            'command': f"{self.command:02X}",
            'command_name': SASCommand(self.command).name if self.command in SASCommand._value2member_map_ else 'UNKNOWN',
            'data': self.data.hex().upper(),
            'crc': f"{self.crc:04X}",
            'valid': self.is_valid(),
            'raw': self.raw.hex().upper()
        }

class SASProtocol:
    """Core SAS Protocol Handler"""

    def __init__(self):
        self.broadcast_address = 0x00

    def parse_packet(self, raw_data: bytes) -> Optional[SASPacket]:
        """Parse a CRC-bearing SAS frame: [addr][cmd][data...][crc-lo][crc-hi].

        For single-byte responses (general-poll exception codes, lone-address
        ACKs) use parse_response() instead.
        """
        if len(raw_data) < 4:  # addr + cmd + 2 CRC bytes
            return None

        try:
            address = raw_data[0]
            command = raw_data[1]
            data = raw_data[2:-2]
            crc = struct.unpack('<H', raw_data[-2:])[0]  # LSB first on the wire

            packet = SASPacket(
                address=address,
                command=command,
                data=data,
                crc=crc,
                raw=raw_data
            )

            # Validate CRC before returning
            if not packet.is_valid():
                logger.warning(f"Invalid CRC in packet: expected {packet.crc:04X}, calculated {sas_crc(bytes([address, command]) + data):04X}")
                return None

            return packet

        except Exception as e:
            logger.error(f"Error parsing SAS packet: {e}")
            return None

    def parse_response(self, raw_data: bytes,
                       last_poll: Optional[str] = None,
                       address: Optional[int] = None) -> Optional[Dict]:
        """Parse a machine response. A lone byte's meaning is CONTEXTUAL in
        SAS — it cannot be classified by value (most exception codes are
        <= 0x7F, and a type-S NACK is address|0x80). Pass last_poll to
        disambiguate:

          last_poll='general' : 1 byte = exception code (0x00 = no activity)
          last_poll='transfer': 1 byte = bare address ACK, address|0x80 NACK
          (any)               : 2 bytes [addr][0x00] = machine BUSY (no CRC)
                                4+ bytes = CRC-bearing frame

        Returns a dict with 'type' in {exception, ack, nack, busy, packet,
        crc_error, short} or None for empty input.
        """
        if not raw_data:
            return None

        # 2-byte busy response: [addr][0x00], no CRC (machine can't answer yet)
        if len(raw_data) == 2 and raw_data[1] == 0x00:
            return {'type': 'busy', 'address': raw_data[0], 'raw': raw_data}

        if len(raw_data) == 1:
            v = raw_data[0]
            if last_poll == 'transfer':
                if address is not None and v == (address | 0x80):
                    return {'type': 'nack', 'value': v}
                return {'type': 'ack', 'value': v}
            # default / general poll: a lone byte is an exception code
            return {'type': 'exception', 'value': v,
                    'idle': v == 0x00}

        if len(raw_data) < 4:
            return {'type': 'short', 'raw': raw_data}

        packet = self.parse_packet(raw_data)
        if packet is None:
            # genuine 4+ byte frame whose CRC did not validate — THIS is the
            # only case worth flagging as CRC adjudication evidence
            return {'type': 'crc_error', 'raw': raw_data}
        return {'type': 'packet', 'packet': packet}

    def build_packet(self, address: int, command: int, data: bytes = b'',
                     include_crc: bool = True) -> bytes:
        """Build a SAS frame: [addr][cmd][data...] + CRC (LSB first).

        Two-byte polls (no data) are sent WITHOUT a CRC per the SAS spec —
        pass include_crc=False or use build_short_poll() for those.
        The wake-up (9th) bit on the address byte is a SERIAL-LAYER concern
        (mark parity on byte 0) — not represented in these bytes.
        """
        packet = bytes([address, command]) + data
        if include_crc:
            packet += struct.pack('<H', sas_crc(packet))  # LSB first
        return packet

    def build_short_poll(self, address: int, command: int) -> bytes:
        """Two-byte long poll (e.g. 0x1A current credits): no CRC."""
        return bytes([address, command])

    def build_general_poll(self, address: int) -> bytes:
        """General poll: single byte 0x80|address, answered by one
        exception-code byte (0x00 = no activity)."""
        return bytes([GENERAL_POLL_FLAG | address])

    def build_poll(self, address: int) -> bytes:
        """Build a general poll (kept for API compatibility)."""
        return self.build_general_poll(address)

    def build_meter_request(self, address: int, meter_code: int) -> bytes:
        """Build a meter request: two-byte poll, no CRC."""
        return self.build_short_poll(address, meter_code)
    
    def parse_meter_response(self, packet: SASPacket) -> Optional[int]:
        """Parse meter value from response"""
        if not packet.is_valid():
            return None
            
        # Meter responses are typically 4-byte BCD values
        if len(packet.data) == 4:
            return self._bcd_to_int(packet.data)
        return None
    
    def _bcd_to_int(self, bcd_bytes: bytes) -> int:
        """Convert BCD bytes to integer"""
        result = 0
        for byte in bcd_bytes:
            # Validate BCD digits
            high = (byte >> 4)
            low = (byte & 0x0F)
            if high > 9 or low > 9:
                logger.warning(f"Invalid BCD byte: {byte:02X}")
                return None
            result = result * 100 + (high * 10) + low
        return result
    
    def _int_to_bcd(self, value: int, length: int = 4) -> bytes:
        """Convert a non-negative integer to packed BCD, MSB-first, in
        `length` bytes (spec §2.2.3: BCD data are sent most-significant byte
        first). RAISES if the value needs more than 2*length digits — a
        silent overflow here would truncate a MONEY amount on the wire
        (e.g. a $10,000.00 = 1000000-cent transfer packed into 5 bytes would
        drop the high digits and authorize a different amount). Adjudication
        2026-07-07 flagged the missing guard."""
        if value < 0:
            raise ValueError(f"BCD value must be non-negative: {value}")
        if value >= 10 ** (2 * length):
            raise ValueError(
                f"BCD value {value} does not fit in {length} bytes "
                f"({2 * length} digits) — would truncate on the wire")
        bcd = []
        for _ in range(length):
            low = value % 10
            value //= 10
            high = value % 10
            value //= 10
            bcd.append((high << 4) | low)
        return bytes(reversed(bcd))

class SASCommandBuilder:
    """Helper class to build common SAS commands.

    CRC rule (SAS spec Table 7.4a): type R (read-only) long polls are exactly
    [addr][cmd] with NO CRC; type S/M/G polls ALWAYS carry a CRC, even when
    they have no data bytes (so shutdown is a 4-byte '01 01 CRC CRC' frame).
    Sending a CRC on a type R poll makes the machine start answering the
    2-byte poll it recognizes, then collide with the two stray CRC bytes."""

    def __init__(self, protocol: SASProtocol):
        self.protocol = protocol

    # --- type S/M control polls: CRC even with no data --------------------
    def shutdown(self, address: int) -> bytes:
        return self.protocol.build_packet(address, SASCommand.SHUTDOWN)

    def startup(self, address: int) -> bytes:
        return self.protocol.build_packet(address, SASCommand.STARTUP)

    def enable_bill_acceptor(self, address: int) -> bytes:
        return self.protocol.build_packet(address, SASCommand.ENABLE_BILL_ACCEPTOR)

    def disable_bill_acceptor(self, address: int) -> bytes:
        return self.protocol.build_packet(address, SASCommand.DISABLE_BILL_ACCEPTOR)

    def handpay_reset(self, address: int) -> bytes:
        """NOT a single frame — handpay reset-to-credits is a strict TWO-STEP
        sequence (AJ bench fact, 2026-07-01: "send the byte prior to reset"):
        method-select 0xA8 must be ACKED before the 0x94 reset goes out.
        Use core/sas_handpay_reset.reset_handpay_to_credits() (or the
        SASPoller.reset_handpay_to_credits() wrapper) — a one-frame builder
        here could only ever implement the sequence wrong. Poll numbers are
        TODO(bench); the Montana guide does not document any of it."""
        raise NotImplementedError(
            "handpay reset is a two-step sequence (0xA8 method select -> "
            "0x94 reset); use core/sas_handpay_reset.reset_handpay_to_credits")

    # --- type R read polls: NO CRC (2-byte frame) -------------------------
    def request_current_credits(self, address: int) -> bytes:
        return self.protocol.build_short_poll(address, SASCommand.CURRENT_CREDITS)

    def request_machine_id(self, address: int) -> bytes:
        return self.protocol.build_short_poll(address, SASCommand.GAMING_MACHINE_ID)

class MeterGroup:
    """SAS single-meter long-poll codes -> names.

    Rebuilt 2026-06-12 from the Montana DOJ SAS Implementation Guide v1.5.0
    §4.4 (the prior map was shifted: it labeled 0x11 'Total Drop' when the
    guide defines 0x11 = Coin In, etc.). All values are 4-byte BCD credit
    meters unless noted."""

    METERS = {
        0x0F: "Meters 10-15 (composite)",
        0x10: "Cancelled Credits",
        0x11: "Coin In",
        0x12: "Coin Out",
        0x13: "Total Drop",
        0x15: "Games Played",
        0x16: "Games Won",
        0x17: "Games Lost",
        0x18: "Games Since Power-Up / Door Close",
        0x19: "Meters 11-15 (composite)",
        0x1A: "Current Credits",
        0x1E: "Bill Meters",
        0x20: "Dollar Value of Bills",
        0x31: "$1 Bills In",
        0x32: "$2 Bills In",
        0x33: "$5 Bills In",
        0x34: "$10 Bills In",
        0x35: "$20 Bills In",
        0x36: "$50 Bills In",
        0x37: "$100 Bills In",
        0x46: "Credit Amount of All Bills",
    }

    @classmethod
    def get_meter_name(cls, code: int) -> str:
        """Get human-readable meter name"""
        return cls.METERS.get(code, f"Unknown Meter {code:02X}")