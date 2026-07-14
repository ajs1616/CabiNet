"""
SAS meter suite — builders + parsers for the long polls the Montana DOJ SAS
Implementation Guide v1.5.0 documents (§4.3 ROM signature, §4.4 meters &
general, §4.5 multi-game, §4.6 ticket validation).

Every wire layout in this module is cited to a numbered guide section; the
citations are in each function's docstring. Where the guide is silent (byte
order of "binary format" multi-byte fields, meter-size tables that live in
the full SAS spec's Appendix C) the gap is marked TODO(bench) rather than
guessed — see the individual notes.

Framing recap (guide §2, and core/sas_protocol.py):
  * type R read polls are exactly [addr][cmd] with NO CRC;
  * data-bearing polls and all multi-byte responses end with the 16-bit
    CRC-16/Kermit transmitted LSB first;
  * every guide response layout counts Byte 1 = address, Byte 2 = command —
    the `data` arguments to the parsers here are the bytes BETWEEN the
    command byte and the CRC (i.e. SASPacket.data).

Units: 1 credit = 1 cent in the guide's jurisdiction (§4.2); CasinoNet keeps
meters in raw credit units and leaves cents/dollars to the caller.
"""

import struct
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .sas_protocol import SASProtocol, sas_crc

__all__ = [
    "bcd_to_int", "int_to_bcd", "SAS_POLL_FLOOR",
    "SINGLE_METER_COMMANDS", "TYPE_R_POLLS", "RESPONSE_DATA_PARSERS",
    "MeterSnapshot", "GameMeters", "LastBill", "EnabledFeatures",
    "ValidationMeters", "CashoutTicketInfo", "ValidationRecord",
    "build_meter_poll", "build_rom_signature_poll",
    "build_game_meters_poll", "build_game_config_poll",
    "build_selected_meters_poll_2f", "build_selected_meters_poll_6f",
    "build_validation_meters_poll", "build_enhanced_validation_info_poll",
    "build_set_validation_id_poll", "build_receive_date_time_poll",
    "parse_single_meter", "parse_meters_10_15", "parse_meters_11_15",
    "parse_games_since", "parse_bill_meters", "parse_last_bill",
    "parse_date_time", "parse_enabled_features", "parse_total_games",
    "parse_game_meters", "parse_selected_meters_6f", "parse_selected_meters_2f",
    "parse_rom_signature", "parse_validation_meters",
    "parse_cashout_ticket_info", "parse_enhanced_validation_info",
    "decode_response", "sweep_snapshot",
    "SEND_PENDING_CASHOUT", "RECEIVE_VALIDATION_NUMBER",
    "PendingCashout", "build_send_pending_cashout_poll",
    "parse_pending_cashout", "build_receive_validation_number_poll",
]


# SAS forbids polling a single machine faster than once per 200 ms (the
# 40 ms rate is RTE/ticketing-only) — the safe cadence for unknown game
# firmware. Defined here so the multi-poll sweeps below can pace themselves;
# sas_poller re-exports it as its loop interval.
SAS_POLL_FLOOR = 0.200


# --------------------------------------------------------------------------
# BCD helpers (public module-level versions of SASProtocol's private ones)
# --------------------------------------------------------------------------

def bcd_to_int(data: bytes) -> Optional[int]:
    """Big-endian packed BCD -> int. Returns None on a non-BCD nibble
    (matches SASProtocol._bcd_to_int semantics — a corrupt meter must not
    poison the cache with a wrong number)."""
    result = 0
    for byte in data:
        hi, lo = byte >> 4, byte & 0x0F
        if hi > 9 or lo > 9:
            return None
        result = result * 100 + hi * 10 + lo
    return result


def int_to_bcd(value: int, length: int) -> bytes:
    """int -> big-endian packed BCD of `length` bytes (2 digits per byte)."""
    if value < 0 or value >= 10 ** (length * 2):
        raise ValueError(f"{value} does not fit in {length} BCD bytes")
    digits = f"{value:0{length * 2}d}"
    # pack two decimal digits per byte: "12" -> 0x12
    return bytes(int(digits[i:i + 2], 16) for i in range(0, len(digits), 2))


def _bcd2(byte: int) -> int:
    """One BCD byte -> 0..99, raising on non-BCD (for date/time fields where
    silently returning None would hide corruption)."""
    hi, lo = byte >> 4, byte & 0x0F
    if hi > 9 or lo > 9:
        raise ValueError(f"non-BCD byte 0x{byte:02X}")
    return hi * 10 + lo


# --------------------------------------------------------------------------
# Poll builders
# --------------------------------------------------------------------------

# System-validation ticket-out (cash-out) long polls. NOT in the Montana
# guide on disk — cross-verified 2026-07-08 against saspy, ArduinoTITO, the
# SAS 6.02 §15.8 spec text and the Montana Implementation Guide (see the
# memory reference reference_casinonet_sas_system_validation). 0x57 "Send
# Pending Cashout Information" is a Type-R read (bare [addr][0x57], no
# outbound CRC); 0x58 "Receive Validation Number" is a data-bearing Type-S
# poll (WITH CRC).
SEND_PENDING_CASHOUT = 0x57
RECEIVE_VALIDATION_NUMBER = 0x58

# Guide poll examples that are exactly '01 <cmd>' — i.e. type R, no data,
# no CRC: §4.4.1-.24 (except the data-bearing 21/6F/7B/7F), §4.4.27-.28,
# §4.5.2/.5/.6, §4.6.3. 0x57 (Send Pending Cashout Information) is likewise a
# bare read (cross-verified, see above) — added so build_meter_poll and the
# poller treat it as CRC-less.
TYPE_R_POLLS = frozenset({
    0x0F, 0x10, 0x11, 0x12, 0x13, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
    0x1E, 0x1F, 0x20, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x46,
    0x48, 0x51, 0x54, 0x55, 0x56, 0x3D, 0x57, 0x7E, 0xA0,
})

_PROTO = SASProtocol()          # stateless; safe to share


def _crc_frame(body: bytes) -> bytes:
    return body + struct.pack("<H", sas_crc(body))


def build_meter_poll(address: int, command: int) -> bytes:
    """Two-byte type R read poll [addr][cmd], NO CRC — valid only for the
    commands the guide shows as bare '01 <cmd>' polls (TYPE_R_POLLS)."""
    if command not in TYPE_R_POLLS:
        raise ValueError(
            f"0x{command:02X} is not a guide-documented type R poll; "
            "use its dedicated builder (it carries data + CRC)")
    return _PROTO.build_short_poll(address, command)


def build_rom_signature_poll(address: int, seed: bytes) -> bytes:
    """§4.3.1 (0x21): [addr][21][seed0][seed1][crc]. `seed` is passed as the
    2 raw wire bytes because the guide says only "binary format".

    DELIVERY IS ASYNCHRONOUS — do NOT expect transact(this poll) to return
    the signature. Guide §4.3 (p.11): "The result of the computation is sent
    to the host in response to the next general poll command after the
    completion of the computation", and §3 (p.8): the pending result is
    transmitted *instead of an event exception* in response to a general
    poll, and once the next poll implies the ACK "the VGM shall erase the
    ROM signature verification result". So the result arrives as a full
    [addr][21][sig][sig][crc] frame answering a LATER general poll —
    SASPoller captures it (state.poll_results[0x21] / on_poll_result) before
    its next poll ACKs-and-erases it.

    BYTE ORDER (resolved 2026-07-07 against the SAS 6.01 spec §2.2.3): data
    "exchanged in the binary format are sent least significant byte first."
    The ROM signature seed and the returned 16-bit signature are binary (not
    BCD), so a numeric seed must be packed LOW-BYTE-FIRST — e.g. seed 0x1234
    -> b"\\x34\\x12" — and the returned signature is parsed the same way
    before any numeric compare. This function takes the 2 wire bytes already
    ordered; callers converting an int seed use int.to_bytes(2, "little").
    (The Montana guide was silent here; the full spec is not — the old
    "NOT specified" note was stale.)
    """
    if len(seed) != 2:
        raise ValueError("ROM signature seed must be exactly 2 bytes")
    return _crc_frame(bytes([address, 0x21]) + seed)


def build_game_meters_poll(address: int, game_number: int) -> bytes:
    """§4.5.3 (0x52): [addr][52][game# 2-byte BCD][crc]."""
    return _crc_frame(bytes([address, 0x52]) + int_to_bcd(game_number, 2))


def build_game_config_poll(address: int, game_number: int) -> bytes:
    """§4.5.4 (0x53): [addr][53][game# 2-byte BCD][crc]."""
    return _crc_frame(bytes([address, 0x53]) + int_to_bcd(game_number, 2))


def build_selected_meters_poll_2f(address: int, game_number: int,
                                  meter_codes: List[int]) -> bytes:
    """§4.5.1 (0x2F): [addr][2F][len][game# 2-byte BCD][code]...[crc].
    Meter codes are ONE byte each here (unlike 0x6F's two); max 10.
    Montana requires only codes 0x00 (coin in) and 0x01 (coin out)."""
    if not 1 <= len(meter_codes) <= 10:
        raise ValueError("2F takes 1..10 meter codes")
    body = bytes([address, 0x2F, 2 + len(meter_codes)])
    body += int_to_bcd(game_number, 2) + bytes(meter_codes)
    return _crc_frame(body)


def build_selected_meters_poll_6f(address: int, game_number: int,
                                  meter_codes: List[int]) -> bytes:
    """§4.4.25 (0x6F): [addr][6F][len][game# 2-byte BCD][code lo][code hi]...
    [crc]. Meter codes are TWO bytes each; max 11; game 0000 = whole machine.

    TODO(bench): the guide says only "meter code ... binary format" for the
    2-byte code; we transmit it LSB-first to match SAS's convention for
    binary multi-byte fields — the CRC, the only capture-validated multi-byte
    binary field in this repo, goes LSB-first. (The guide's table 4.1 renders
    codes as hex text, e.g. "0024" for Total Drop; that is notation, not wire
    order.) parse_selected_meters_6f uses the same order, so build/parse
    round-trip regardless — interop still needs the order confirmed against
    the full SAS spec / a live machine.
    """
    if not 1 <= len(meter_codes) <= 11:
        raise ValueError("6F takes 1..11 meter codes")
    body = bytes([address, 0x6F, 2 + 2 * len(meter_codes)])
    body += int_to_bcd(game_number, 2)
    for code in meter_codes:
        body += struct.pack("<H", code)
    return _crc_frame(body)


def build_validation_meters_poll(address: int,
                                 validation_type: int = 0x00) -> bytes:
    """§4.6.4 (0x50): [addr][50][type][crc]. Type 0x00 (cashable ticket from
    cash out or win) is the only type Montana requires."""
    return _crc_frame(bytes([address, 0x50, validation_type]))


def build_enhanced_validation_info_poll(address: int,
                                        function_code: int = 0x00) -> bytes:
    """§4.6.2 (0x4D): [addr][4D][function][crc]. Function codes: 00 = read
    current validation info, 01-1F = buffer index n, FF = look-ahead."""
    if not (function_code == 0xFF or 0x00 <= function_code <= 0x1F):
        raise ValueError("4D function code must be 00-1F or FF")
    return _crc_frame(bytes([address, 0x4D, function_code]))


def build_set_validation_id_poll(address: int, validation_id: bytes,
                                 sequence: bytes) -> bytes:
    """§4.6.1 (0x4C): [addr][4C][id 3B][seq 3B][crc]. Both fields are passed
    as raw wire bytes ("binary format") — set validation_id to b'\\x00'*3 to
    READ the current ID/sequence instead of setting them.

    TODO(bench): byte order of the 3-byte binary fields is not specified in
    the guide; keep them as opaque bytes until the full SAS spec / a live
    machine settles it.
    """
    if len(validation_id) != 3 or len(sequence) != 3:
        raise ValueError("validation ID and sequence are 3 bytes each")
    return _crc_frame(bytes([address, 0x4C]) + validation_id + sequence)


def build_receive_date_time_poll(address: int, when: datetime) -> bytes:
    """§4.4.29 (0x7F, host->VGM set date/time): [addr][7F][MMDDYYYY 4-byte
    BCD][HHMMSS 3-byte BCD][crc]. The VGM's response is its bare address
    byte (parse with SASProtocol.parse_response(last_poll='transfer'))."""
    body = bytes([address, 0x7F])
    body += int_to_bcd(when.month, 1) + int_to_bcd(when.day, 1)
    body += int_to_bcd(when.year, 2)
    body += int_to_bcd(when.hour, 1) + int_to_bcd(when.minute, 1)
    body += int_to_bcd(when.second, 1)
    return _crc_frame(body)


def build_send_pending_cashout_poll(address: int) -> bytes:
    """0x57 "Send Pending Cashout Information" — the host's real-time read of
    the cash-out the EGM is waiting to have validated (cross-verified, see the
    SEND_PENDING_CASHOUT note above). Type R: bare [addr][0x57], NO outbound
    CRC. The EGM answers [addr][0x57][cashout type 1B][amount 5B BCD][crc]."""
    return bytes([address, SEND_PENDING_CASHOUT])


def build_receive_validation_number_poll(address: int, validation_id: int,
                                         payload8: bytes) -> bytes:
    """0x58 "Receive Validation Number" — the host's answer to a system
    validation request. Type S, WITH CRC, 13 bytes:
    [addr][0x58][validation ID 1B][8-byte payload][crc-lo][crc-hi].

    validation_id: 0x01 = APPROVE (print the ticket) / 0x00 = REJECT (drop to
    handpay). payload8 is the 8 validation-data bytes and MUST be exactly 8
    bytes — SAS 6.02 §15.8: the packed-BCD validation number for the
    spec/saspy 'bcd_number' style, or the ArduinoTITO [cashout type][00][00]
    [5B amount] echo. Caller builds the 8 bytes (see sas_tito_host); this
    builder is style-agnostic and only guarantees framing + CRC."""
    if len(payload8) != 8:
        raise ValueError(f"validation payload must be exactly 8 bytes, "
                         f"got {len(payload8)}")
    body = bytes([address, RECEIVE_VALIDATION_NUMBER, validation_id]) + payload8
    return _crc_frame(body)


# --------------------------------------------------------------------------
# Response parsers — each takes SASPacket.data (bytes between cmd and CRC)
# --------------------------------------------------------------------------

# Single 4-byte-BCD meters: code -> snake_case label.
# §4.4.2-.8 (10-17), §4.4.11 (1A), §4.4.14 (20), §4.4.15-.21 (31-37),
# §4.4.22 (46). 31-37 count BILLS; the rest count credits (= cents, §4.2).
SINGLE_METER_COMMANDS: Dict[int, str] = {
    0x10: "cancelled_credits",
    0x11: "coin_in",
    0x12: "coin_out",
    0x13: "total_drop",
    0x15: "games_played",
    0x16: "games_won",
    0x17: "games_lost",
    0x1A: "current_credits",
    0x20: "dollar_value_of_bills",
    0x31: "bills_1",
    0x32: "bills_2",
    0x33: "bills_5",
    0x34: "bills_10",
    0x35: "bills_20",
    0x36: "bills_50",
    0x37: "bills_100",
    0x46: "credit_amount_of_bills",
}

# 0x1E bill meters (§4.4.12): six 4-byte BCD counts, in this denomination
# order (cents). NOTE the $2 bill has a single-meter poll (0x32) but is NOT
# in the 1E composite — that's the guide's layout, not an omission here.
_BILL_METER_ORDER_CENTS = (100, 500, 1000, 2000, 5000, 10000)


def _require_len(data: bytes, n: int, what: str) -> None:
    if len(data) != n:
        raise ValueError(f"{what}: expected {n} data bytes, got {len(data)}")


def parse_single_meter(data: bytes) -> Optional[int]:
    """A lone 4-byte BCD meter (see SINGLE_METER_COMMANDS)."""
    _require_len(data, 4, "single meter")
    return bcd_to_int(data)


def parse_meters_10_15(data: bytes) -> Dict[str, Optional[int]]:
    """§4.4.1 (0x0F): cancelled, coin in, coin out, drop, jackpot (always 0
    in MT), games played — six 4-byte BCD fields."""
    _require_len(data, 24, "0F composite")
    labels = ("cancelled_credits", "coin_in", "coin_out", "total_drop",
              "jackpot", "games_played")
    return {lbl: bcd_to_int(data[i * 4:i * 4 + 4])
            for i, lbl in enumerate(labels)}


def parse_meters_11_15(data: bytes) -> Dict[str, Optional[int]]:
    """§4.4.10 (0x19): coin in, coin out, drop, jackpot, games played."""
    _require_len(data, 20, "19 composite")
    labels = ("coin_in", "coin_out", "total_drop", "jackpot", "games_played")
    return {lbl: bcd_to_int(data[i * 4:i * 4 + 4])
            for i, lbl in enumerate(labels)}


def parse_games_since(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """§4.4.9 (0x18): (games since power-up, games since door close) — two
    2-byte BCD fields."""
    _require_len(data, 4, "0x18 games-since")
    return bcd_to_int(data[0:2]), bcd_to_int(data[2:4])


def parse_bill_meters(data: bytes) -> Dict[int, Optional[int]]:
    """§4.4.12 (0x1E): counts of $1/$5/$10/$20/$50/$100 bills accepted.
    Returns {denomination_cents: count}."""
    _require_len(data, 24, "1E bill meters")
    return {denom: bcd_to_int(data[i * 4:i * 4 + 4])
            for i, denom in enumerate(_BILL_METER_ORDER_CENTS)}


@dataclass(frozen=True)
class LastBill:
    """§4.4.23 (0x48). country_code/denom_code are raw BCD-decoded codes —
    the code->currency/denomination tables live in the full SAS spec, not
    the guide. TODO(bench): map denom_code to cents once cited."""
    country_code: Optional[int]
    denom_code: Optional[int]
    count: Optional[int]


def parse_last_bill(data: bytes) -> LastBill:
    """§4.4.23 (0x48): country code (1 BCD), bill denom code (1 BCD),
    count of accepted bills of this type (4-byte BCD)."""
    _require_len(data, 6, "48 last bill")
    return LastBill(bcd_to_int(data[0:1]), bcd_to_int(data[1:2]),
                    bcd_to_int(data[2:6]))


def parse_date_time(data: bytes) -> datetime:
    """§4.4.27 (0x7E): date MMDDYYYY (4-byte BCD) + time HHMMSS 24-hour
    (3-byte BCD). Raises ValueError on non-BCD or out-of-range fields."""
    _require_len(data, 7, "7E date/time")
    month, day = _bcd2(data[0]), _bcd2(data[1])
    year = _bcd2(data[2]) * 100 + _bcd2(data[3])
    hour, minute, second = _bcd2(data[4]), _bcd2(data[5]), _bcd2(data[6])
    return datetime(year, month, day, hour, minute, second)


@dataclass(frozen=True)
class EnabledFeatures:
    """§4.4.28 (0xA0) feature bits, decoded per the guide's Table 4.5.36.

    TODO(bench): the guide labels the two feature-code bytes 'LSB' and 'MSB'
    but does not state their wire order; we take data byte 3 (the first
    feature byte on the wire) as the LSB because the table lists LSB first.
    Confirm against the full SAS spec / a live machine.
    """
    game_number: Optional[int]
    raw: bytes
    jackpot_multiplier: bool
    bonus_awards: bool
    tournament: bool
    validation_style: str            # standard | system | enhanced | reserved
    voucher_redemption: bool
    meter_model: int                 # 0-3, table's MSB bits 0-1
    vouchers_to_drop: bool
    extended_meters: bool
    component_authentication: bool
    aft: bool
    multi_denom_extensions: bool


_VALIDATION_STYLES = ("standard", "system", "enhanced", "reserved")


def parse_enabled_features(data: bytes) -> EnabledFeatures:
    """§4.4.28 (0xA0): game# (2-byte BCD, 0000 = whole machine), feature
    codes (2 bytes, Table 4.5.36), 4 reserved bytes."""
    _require_len(data, 8, "A0 enabled features")
    lsb, msb = data[2], data[3]      # see TODO(bench) on EnabledFeatures
    return EnabledFeatures(
        game_number=bcd_to_int(data[0:2]),
        raw=data[2:4],
        jackpot_multiplier=bool(lsb & 0x01),
        bonus_awards=bool(lsb & 0x04),
        tournament=bool(lsb & 0x08),
        validation_style=_VALIDATION_STYLES[(lsb >> 5) & 0x03],
        voucher_redemption=bool(lsb & 0x80),
        meter_model=msb & 0x03,
        vouchers_to_drop=bool(msb & 0x04),
        extended_meters=bool(msb & 0x08),
        component_authentication=bool(msb & 0x10),
        aft=bool(msb & 0x40),
        multi_denom_extensions=bool(msb & 0x80),
    )


def parse_total_games(data: bytes) -> Optional[int]:
    """§4.5.2 (0x51): number of games implemented, 2-byte BCD."""
    _require_len(data, 2, "51 total games")
    return bcd_to_int(data)


@dataclass(frozen=True)
class GameMeters:
    """§4.5.3 (0x52) per-game meters. jackpot is always zero in MT."""
    game_number: Optional[int]
    coin_in: Optional[int]
    coin_out: Optional[int]
    jackpot: Optional[int]
    games_played: Optional[int]


def parse_game_meters(data: bytes) -> GameMeters:
    """§4.5.3 (0x52): game# (2 BCD) + coin in/coin out/jackpot/games played
    (4-byte BCD each)."""
    _require_len(data, 18, "52 game meters")
    return GameMeters(
        game_number=bcd_to_int(data[0:2]),
        coin_in=bcd_to_int(data[2:6]),
        coin_out=bcd_to_int(data[6:10]),
        jackpot=bcd_to_int(data[10:14]),
        games_played=bcd_to_int(data[14:18]),
    )


def parse_selected_meters_6f(data: bytes) -> Dict[str, object]:
    """§4.4.25 (0x6F) response: [len][game# 2 BCD] then repeating
    [meter code 2B][size 1B][value size-byte BCD]. Self-describing thanks to
    the per-meter size byte. Returns {'game_number': n,
    'meters': {code: value}}. Meter-code byte order: see
    build_selected_meters_poll_6f's TODO(bench)."""
    if len(data) < 3:
        raise ValueError("6F response too short")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"6F length byte {declared} != {len(data) - 1}")
    game_number = bcd_to_int(data[1:3])
    meters: Dict[int, Optional[int]] = {}
    i = 3
    while i < len(data):
        if i + 3 > len(data):
            raise ValueError("6F truncated meter entry")
        code = struct.unpack("<H", data[i:i + 2])[0]
        size = data[i + 2]
        i += 3
        if i + size > len(data):
            raise ValueError("6F meter value overruns frame")
        meters[code] = bcd_to_int(data[i:i + size])
        i += size
    return {"game_number": game_number, "meters": meters}


def parse_selected_meters_2f(data: bytes,
                             meter_sizes: Optional[Dict[int, int]] = None
                             ) -> Dict[str, object]:
    """§4.5.1 (0x2F) response: [len][game# 2 BCD] then repeating
    [meter code 1B][value 4-or-5-byte BCD].

    HONEST LIMITATION: unlike 0x6F there is no per-meter size byte, and the
    4-vs-5-byte size table is in the full SAS spec's Appendix C (table C-7),
    which is NOT on disk. So:
      * a single-meter response is parsed by inferring the size from the
        remaining length;
      * a multi-meter response REQUIRES `meter_sizes` ({code: 4 or 5});
        without it this raises ValueError instead of guessing.
    TODO(bench): fill a cited meter-size table from the full SAS spec.
    """
    if len(data) < 3:
        raise ValueError("2F response too short")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"2F length byte {declared} != {len(data) - 1}")
    game_number = bcd_to_int(data[1:3])
    meters: Dict[int, Optional[int]] = {}
    i = 3
    while i < len(data):
        code = data[i]
        i += 1
        remaining = len(data) - i
        if meter_sizes and code in meter_sizes:
            size = meter_sizes[code]
        elif remaining in (4, 5):
            size = remaining         # single/last meter: size is unambiguous
        else:
            raise ValueError(
                f"2F meter 0x{code:02X}: value size unknown (4 or 5 bytes; "
                "spec Appendix C table C-7 needed) — pass meter_sizes")
        if size > remaining:
            raise ValueError("2F meter value overruns frame")
        meters[code] = bcd_to_int(data[i:i + size])
        i += size
    return {"game_number": game_number, "meters": meters}


def parse_rom_signature(data: bytes) -> bytes:
    """§4.3.1 (0x21) result frame: the 2-byte ROM signature, returned RAW.

    NOTE: this frame does NOT answer the 0x21 poll itself — it answers a
    LATER general poll, sent instead of an exception code once the VGM
    finishes computing (§4.3 p.11 / §3 p.8; see build_rom_signature_poll).
    SASPoller._general_poll routes it here via decode_response; anything
    that treats a general-poll response as a single exception byte would
    misread it AND cause the VGM to erase the result on the next poll.

    TODO(bench): byte order unspecified in the guide — compare signatures as
    bytes, not ints, until the order is confirmed."""
    _require_len(data, 2, "21 ROM signature")
    return data


@dataclass(frozen=True)
class ValidationMeters:
    """§4.6.4 (0x50). amount is in cents per the guide."""
    validation_type: int
    count: Optional[int]
    amount_cents: Optional[int]


def parse_validation_meters(data: bytes) -> ValidationMeters:
    """§4.6.4 (0x50): type (1B binary), total validations (4-byte BCD),
    cumulative amount in cents (5-byte BCD)."""
    _require_len(data, 10, "50 validation meters")
    return ValidationMeters(data[0], bcd_to_int(data[1:5]),
                            bcd_to_int(data[5:10]))


@dataclass(frozen=True)
class CashoutTicketInfo:
    """§4.6.3 (0x3D). validation_number is the 8-digit STANDARD validation
    number as a zero-padded string (all zeros if the machine is configured
    for enhanced/system validation, per the guide's note). Decoded with
    .hex(), which for valid packed BCD IS the decimal digit string; a corrupt
    nibble shows as a-f — visible rather than silently misdecoded."""
    validation_number: str
    amount_cents: Optional[int]


def parse_cashout_ticket_info(data: bytes) -> CashoutTicketInfo:
    """§4.6.3 (0x3D): standard validation number (4-byte BCD) + ticket
    amount in cents (5-byte BCD)."""
    _require_len(data, 9, "3D cashout ticket info")
    return CashoutTicketInfo(data[0:4].hex(), bcd_to_int(data[4:9]))


@dataclass(frozen=True)
class PendingCashout:
    """0x57 "Send Pending Cashout Information" response (cross-verified, see
    the SEND_PENDING_CASHOUT note). amount_cents is the cash-out value in
    cents (guide §4.2: 1 credit unit = 1 cent), decoded from the 5-byte
    big-endian BCD amount field; amount_raw keeps those 5 wire bytes so the
    ArduinoTITO 'type_amount' 0x58 payload can echo them verbatim.

    Tuple-unpackable as (cashout_type, amount_cents) for the ticket's stated
    parse_pending_cashout signature; amount_raw is the third element."""
    cashout_type: int
    amount_cents: Optional[int]
    amount_raw: bytes

    def __iter__(self):
        return iter((self.cashout_type, self.amount_cents, self.amount_raw))


def parse_pending_cashout(data: bytes) -> PendingCashout:
    """Parse the 0x57 response's data bytes (between cmd and CRC):
    [cashout type 1B][amount 5B BCD, MSB..LSB] — 6 data bytes. amount is
    decoded as packed BCD cents (a non-BCD nibble yields amount_cents=None,
    which the caller MUST treat as a bad read — never as an approval)."""
    _require_len(data, 6, "57 pending cashout")
    return PendingCashout(cashout_type=data[0],
                          amount_cents=bcd_to_int(data[1:6]),
                          amount_raw=data[1:6])


@dataclass(frozen=True)
class ValidationRecord:
    """§4.6.2 (0x4D) — one enhanced/system validation record.

    validation_number: 16 BCD digits as a zero-padded string (it is an
    identifier, not a quantity). system_id 00 = enhanced (VGM-calculated),
    01-99 = system validation.
    TODO(bench): ticket_number is 2 bytes 'binary format' with unspecified
    byte order — exposed raw; ticket_number_le is a little-endian reading
    provided for convenience and must be bench-confirmed before use as a key.
    """
    validation_type: int
    buffer_index: int
    when: Optional[datetime]
    validation_number: str
    amount_cents: Optional[int]
    ticket_number_raw: bytes
    system_id: Optional[int]

    @property
    def ticket_number_le(self) -> int:
        return int.from_bytes(self.ticket_number_raw, "little")


def parse_enhanced_validation_info(data: bytes) -> ValidationRecord:
    """§4.6.2 (0x4D): type (1B), buffer index (1B), date MMDDYYYY (4 BCD),
    time HHMMSS (3 BCD), validation number (8 BCD), amount in cents (5 BCD),
    sequential ticket number (2B binary), system ID (1 BCD), 6 reserved —
    31 data bytes (the guide's Byte 3 through Byte 33)."""
    _require_len(data, 31, "4D validation record")
    try:
        when: Optional[datetime] = parse_date_time(data[2:9])
    except ValueError:
        when = None                  # empty/zeroed record: date 00/00/0000
    return ValidationRecord(
        validation_type=data[0],
        buffer_index=data[1],
        when=when,
        validation_number=data[9:17].hex(),
        amount_cents=bcd_to_int(data[17:22]),
        ticket_number_raw=data[22:24],
        system_id=bcd_to_int(data[24:25]),
    )


# Machine-identity parsers (0x1F, 0x53, 0x54, 0x55, 0x56, 0x7B) live in
# core/sas_machine_info.py; this dispatch covers the meter/validation set.
RESPONSE_DATA_PARSERS: Dict[int, Callable[[bytes], object]] = {
    **{code: parse_single_meter for code in SINGLE_METER_COMMANDS},
    0x0F: parse_meters_10_15,
    0x18: parse_games_since,
    0x19: parse_meters_11_15,
    0x1E: parse_bill_meters,
    0x21: parse_rom_signature,
    0x2F: parse_selected_meters_2f,
    0x3D: parse_cashout_ticket_info,
    0x48: parse_last_bill,
    0x4D: parse_enhanced_validation_info,
    0x50: parse_validation_meters,
    0x57: parse_pending_cashout,
    0x51: parse_total_games,
    0x52: parse_game_meters,
    0x6F: parse_selected_meters_6f,
    0x7E: parse_date_time,
    0xA0: parse_enabled_features,
}


def decode_response(command: int, data: bytes):
    """Dispatch a response's data bytes to the right parser, or None if the
    command has no parser here."""
    parser = RESPONSE_DATA_PARSERS.get(command)
    return parser(data) if parser else None


# --------------------------------------------------------------------------
# MeterSnapshot + sweep
# --------------------------------------------------------------------------

@dataclass
class MeterSnapshot:
    """Everything the §4.4 accounting polls report, in credit units
    (1 credit = 1 cent, §4.2). Fields left None = machine didn't answer that
    poll (per §2.7.4 an unsupported poll gets dead silence, which is normal
    — real games implement subsets)."""
    address: int
    taken_at: float = 0.0                       # time.monotonic()
    cancelled_credits: Optional[int] = None     # 0F
    coin_in: Optional[int] = None               # 0F
    coin_out: Optional[int] = None              # 0F
    total_drop: Optional[int] = None            # 0F
    jackpot: Optional[int] = None               # 0F (always 0 in MT)
    games_played: Optional[int] = None          # 0F
    games_won: Optional[int] = None             # 16
    games_lost: Optional[int] = None            # 17
    games_since_power_up: Optional[int] = None  # 18
    games_since_door_close: Optional[int] = None
    current_credits: Optional[int] = None       # 1A
    dollar_value_of_bills: Optional[int] = None  # 20 (in dollars per guide)
    credit_amount_of_bills: Optional[int] = None  # 46 (in credits)
    bills: Dict[int, Optional[int]] = field(default_factory=dict)  # 1E

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


# The polls one snapshot issues: 0F covers six meters in one frame; the rest
# fill in what 0F lacks. All are type R (no CRC on the poll).
SNAPSHOT_POLLS = (0x0F, 0x16, 0x17, 0x18, 0x1A, 0x1E, 0x20, 0x46)


def sweep_snapshot(transport, address: int,
                   protocol: Optional[SASProtocol] = None,
                   clock: Callable[[], float] = None,
                   pace: float = SAS_POLL_FLOOR,
                   sleep: Callable[[float], None] = time.sleep
                   ) -> MeterSnapshot:
    """Issue the SNAPSHOT_POLLS against one machine and return a
    MeterSnapshot. Silence or a bad CRC on any poll leaves those fields None
    (unsupported commands legitimately get no response, §2.7.4).

    Works against any transport with .transact(bytes)->bytes
    (SASSerialPort or MockSASSerialPort). Long polls also serve as the
    implied ACK for a preceding general-poll exception (§2.6 / spec §3), so
    interleaving a sweep with general polls is protocol-safe.

    PACING: SAS forbids polling one machine faster than once per 200 ms
    (SAS_POLL_FLOOR; the 40 ms rate is RTE/ticketing-only), so `sleep(pace)`
    runs between consecutive polls. This matters doubly here: an over-polled
    machine's dropped/busy response is silence, and this sweep by design
    reads silence as "unsupported command" (§2.7.4) — so an over-rate sweep
    would non-deterministically poison the snapshot with phantom Nones.
    Inject a no-op `sleep` in mock tests to keep them fast.
    TODO(bench): if bench timing proves a real BB2E tolerates tighter
    long-poll spacing, lower `pace` to the measured floor — do not remove it.
    """
    proto = protocol or _PROTO
    snap = MeterSnapshot(address=address,
                         taken_at=(clock or time.monotonic)())
    for i, command in enumerate(SNAPSHOT_POLLS):
        if i and pace > 0:
            sleep(pace)
        resp = transport.transact(proto.build_short_poll(address, command))
        packet = proto.parse_packet(resp) if resp else None
        if packet is None or packet.address != address \
                or packet.command != command:
            continue
        try:
            value = decode_response(command, packet.data)
        except ValueError:
            continue                 # malformed frame: skip, don't poison
        if command == 0x0F and isinstance(value, dict):
            for k, v in value.items():
                setattr(snap, k, v)
        elif command == 0x16:
            snap.games_won = value
        elif command == 0x17:
            snap.games_lost = value
        elif command == 0x18:
            snap.games_since_power_up, snap.games_since_door_close = value
        elif command == 0x1A:
            snap.current_credits = value
        elif command == 0x1E and isinstance(value, dict):
            snap.bills = value
        elif command == 0x20:
            snap.dollar_value_of_bills = value
        elif command == 0x46:
            snap.credit_amount_of_bills = value
    return snap
