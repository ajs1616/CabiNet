"""
SAS machine identity — the polls the fleet registry keys on.

Sources (Montana DOJ SAS Implementation Guide v1.5.0):
  0x1F  §4.4.13  gaming machine ID and information (game/paytable/denom/RTP)
  0x54  §4.4.24  SAS version ID + gaming machine serial number
  0x7B  §4.4.26  extended validation status (carries the asset number)
  0x51  §4.5.2   total number of games implemented
  0x55  §4.5.5   selected game number
  0x56  §4.5.6   enabled game numbers
  0x53  §4.5.4   game N configuration (per-game version of 0x1F)

Known guide ambiguities, flagged rather than guessed:
  * 0x55/0x56 game numbers: the guide's text says "ASCII format" but its own
    example frames show 00-bytes, and every other game-number field in the
    guide (0x52/0x53/0xA0 polls) is 2-byte BCD. We decode BCD.
    TODO(bench): confirm BCD vs ASCII against a real multi-game machine; if a
    machine answers 0x3X 0x3X bytes, switch to ASCII decode.
  * 0x7B asset number: 4 bytes, byte order unspecified ("gaming machine
    asset number or house ID") — exposed as raw bytes.
    TODO(bench): confirm byte order against the machine's own asset-number
    config screen before treating it as an integer key.
  * 0x1F denomination code (byte 8): "binary format" — the code->coin-value
    table is in the full SAS spec (not the guide), so the raw code is exposed
    un-translated. TODO(bench): add the cited denom table from the real SAS
    spec; do NOT guess it.
  * RTP field: 4 ASCII digits, "no decimal point, it is implied" — the guide
    does not say where. theoretical_rtp_pct assumes XX.XX (9250 -> 92.50%),
    the only reading that yields sane percentages; the raw string is kept.
    TODO(bench): confirm against a game with a known par sheet.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .sas_protocol import SASProtocol
from .sas_meters import (
    SAS_POLL_FLOOR, bcd_to_int, int_to_bcd, _crc_frame, build_meter_poll,
)

__all__ = [
    "GameConfig", "SASVersionInfo", "ExtendedValidationStatus", "MachineInfo",
    "parse_machine_id", "parse_game_config", "parse_sas_version",
    "parse_selected_game_number", "parse_enabled_game_numbers",
    "parse_extended_validation_status", "build_extended_validation_status_poll",
    "read_machine_info",
]

_PROTO = SASProtocol()


def _ascii(data: bytes) -> str:
    """Decode an ASCII field, replacing junk visibly, stripping NUL/space
    padding."""
    return data.decode("ascii", errors="replace").strip("\x00 ")


@dataclass(frozen=True)
class GameConfig:
    """§4.4.13 (0x1F, machine-level: game_number=None) and §4.5.4 (0x53,
    per-game). Identical layouts apart from 0x53's leading game number."""
    game_number: Optional[int]           # None for the 0x1F machine-level poll
    game_id: str                         # 2 ASCII chars
    additional_game_id: str              # 3 ASCII chars ('0' padded if unused)
    denomination_code: int               # RAW code — see module TODO(bench)
    max_bet: int
    progressive_group: int
    game_options_raw: bytes              # 2 bytes binary, order unspecified
    paytable_id: str                     # 6 ASCII chars
    rtp_raw: str                         # 4 ASCII digits, implied decimal

    @property
    def theoretical_rtp_pct(self) -> Optional[float]:
        """Assumes implied XX.XX decimal — see module TODO(bench)."""
        try:
            return int(self.rtp_raw) / 100.0
        except ValueError:
            return None


def _parse_config_fields(game_number: Optional[int], d: bytes) -> GameConfig:
    """Shared 18-byte field block of 0x1F and 0x53 (post-game-number)."""
    return GameConfig(
        game_number=game_number,
        game_id=_ascii(d[0:2]),
        additional_game_id=_ascii(d[2:5]),
        denomination_code=d[5],
        max_bet=d[6],
        progressive_group=d[7],
        game_options_raw=d[8:10],
        paytable_id=_ascii(d[10:16]),
        rtp_raw=_ascii(d[16:20]),
    )


def parse_machine_id(data: bytes) -> GameConfig:
    """§4.4.13 (0x1F): game ID (2 ASCII), additional ID (3 ASCII), denom
    code (1B), max bet (1B), progressive group (1B), game options (2B),
    paytable ID (6 ASCII), theoretical RTP (4 ASCII) = 20 data bytes."""
    if len(data) != 20:
        raise ValueError(f"1F machine ID: expected 20 data bytes, got {len(data)}")
    return _parse_config_fields(None, data)


def parse_game_config(data: bytes) -> GameConfig:
    """§4.5.4 (0x53): game number (2-byte BCD) + the same 20-byte block as
    0x1F = 22 data bytes."""
    if len(data) != 22:
        raise ValueError(f"53 game config: expected 22 data bytes, got {len(data)}")
    return _parse_config_fields(bcd_to_int(data[0:2]), data[2:22])


@dataclass(frozen=True)
class SASVersionInfo:
    """§4.4.24 (0x54). sas_version is the 3 ASCII digits as sent (e.g.
    '602' = SAS 6.02); serial_number is variable-length ASCII."""
    sas_version: str
    serial_number: str


def parse_sas_version(data: bytes) -> SASVersionInfo:
    """§4.4.24 (0x54): length (1B binary), SAS version (3 ASCII),
    serial number (variable ASCII)."""
    if len(data) < 4:
        raise ValueError("54 response too short")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"54 length byte {declared} != {len(data) - 1}")
    return SASVersionInfo(_ascii(data[1:4]), _ascii(data[4:]))


def parse_selected_game_number(data: bytes) -> Optional[int]:
    """§4.5.5 (0x55): selected game number, 0 if none selected. Decoded as
    2-byte BCD — see the module docstring's BCD-vs-ASCII TODO(bench)."""
    if len(data) != 2:
        raise ValueError(f"55: expected 2 data bytes, got {len(data)}")
    return bcd_to_int(data)


def parse_enabled_game_numbers(data: bytes) -> List[Optional[int]]:
    """§4.5.6 (0x56): length (1B), number of games (1B binary), then 2-byte
    game numbers. Decoded as BCD — see the BCD-vs-ASCII TODO(bench)."""
    if len(data) < 2:
        raise ValueError("56 response too short")
    declared, count = data[0], data[1]
    if declared != len(data) - 1:
        raise ValueError(f"56 length byte {declared} != {len(data) - 1}")
    if len(data) - 2 != 2 * count:
        raise ValueError(f"56 game count {count} != payload of {len(data) - 2} bytes")
    return [bcd_to_int(data[i:i + 2]) for i in range(2, len(data), 2)]


def build_extended_validation_status_poll(address: int) -> bytes:
    """§4.4.26 (0x7B) poll, read-only per the Montana guidance: control mask
    0000, status bits 0000, both expiration fields 0000 ("should always be
    set to 0000") — so this never reconfigures the machine, it only reads
    the asset number / status / expirations back."""
    body = bytes([address, 0x7B, 0x08]) + b"\x00" * 8
    return _crc_frame(body)


@dataclass(frozen=True)
class ExtendedValidationStatus:
    """§4.4.26 (0x7B) response. asset_number_raw: 4 bytes, order unspecified
    (module TODO(bench)); status_bits_raw: 2 bytes; expirations decoded as
    4-digit BCD days (9999 = never) — the guide's 0000/9999 examples read as
    decimal, TODO(bench) confirm BCD vs binary on the wire."""
    asset_number_raw: bytes
    status_bits_raw: bytes
    cashable_expiration_days: Optional[int]
    restricted_expiration_days: Optional[int]


def parse_extended_validation_status(data: bytes) -> ExtendedValidationStatus:
    """§4.4.26 (0x7B): length (1B), asset number (4B), status bits (2B),
    cashable-ticket expiration (2B), restricted-ticket expiration (2B)."""
    if len(data) != 11:
        raise ValueError(f"7B: expected 11 data bytes, got {len(data)}")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"7B length byte {declared} != {len(data) - 1}")
    return ExtendedValidationStatus(
        asset_number_raw=data[1:5],
        status_bits_raw=data[5:7],
        cashable_expiration_days=bcd_to_int(data[7:9]),
        restricted_expiration_days=bcd_to_int(data[9:11]),
    )


@dataclass
class MachineInfo:
    """Aggregated identity for one SAS machine — what the fleet registry
    keys on. None fields = the machine didn't answer that poll (unsupported
    commands legitimately get silence, guide §2.7.4)."""
    address: int
    machine: Optional[GameConfig] = None           # 0x1F
    sas_version: Optional[str] = None              # 0x54
    serial_number: Optional[str] = None            # 0x54
    asset_number_raw: Optional[bytes] = None       # 0x7B
    total_games: Optional[int] = None              # 0x51
    selected_game: Optional[int] = None            # 0x55
    enabled_games: List[Optional[int]] = field(default_factory=list)  # 0x56

    @property
    def registry_key(self) -> str:
        """Stable fleet-registry key: serial number when the machine reports
        one, else paytable+game identity, else the bare SAS address."""
        if self.serial_number:
            return f"sas:{self.serial_number}"
        if self.machine and (self.machine.game_id or self.machine.paytable_id):
            return f"sas:{self.machine.game_id}:{self.machine.paytable_id}"
        return f"sas-addr:{self.address}"


def read_machine_info(transport, address: int,
                      protocol: Optional[SASProtocol] = None,
                      pace: float = SAS_POLL_FLOOR,
                      sleep: Callable[[float], None] = time.sleep
                      ) -> MachineInfo:
    """Sweep the identity polls (1F, 54, 51, 55, 56, 7B) against one machine
    and aggregate a MachineInfo. Tolerant of silence and malformed frames —
    fields just stay None. Works with SASSerialPort or MockSASSerialPort
    (anything with .transact(bytes)->bytes).

    PACING: SAS forbids polling one machine faster than once per 200 ms
    (sas_meters.SAS_POLL_FLOOR), so `sleep(pace)` runs between consecutive
    polls. Because this sweep reads silence as "command unsupported"
    (§2.7.4), over-rate polling would silently misrecord supported polls as
    None fields. Inject a no-op `sleep` in mock tests.
    TODO(bench): lower `pace` only to a bench-measured safe floor."""
    proto = protocol or _PROTO
    info = MachineInfo(address=address)
    asked = [0]                      # polls issued so far (for pacing)

    def ask(frame: bytes, command: int):
        if asked[0] and pace > 0:
            sleep(pace)
        asked[0] += 1
        resp = transport.transact(frame)
        packet = proto.parse_packet(resp) if resp else None
        if packet is None or packet.address != address \
                or packet.command != command:
            return None
        return packet

    pkt = ask(build_meter_poll(address, 0x1F), 0x1F)
    if pkt:
        try:
            info.machine = parse_machine_id(pkt.data)
        except ValueError:
            pass
    pkt = ask(build_meter_poll(address, 0x54), 0x54)
    if pkt:
        try:
            v = parse_sas_version(pkt.data)
            info.sas_version, info.serial_number = v.sas_version, v.serial_number
        except ValueError:
            pass
    pkt = ask(build_meter_poll(address, 0x51), 0x51)
    if pkt and len(pkt.data) == 2:
        info.total_games = bcd_to_int(pkt.data)
    pkt = ask(build_meter_poll(address, 0x55), 0x55)
    if pkt and len(pkt.data) == 2:
        info.selected_game = parse_selected_game_number(pkt.data)
    pkt = ask(build_meter_poll(address, 0x56), 0x56)
    if pkt:
        try:
            info.enabled_games = parse_enabled_game_numbers(pkt.data)
        except ValueError:
            pass
    pkt = ask(build_extended_validation_status_poll(address), 0x7B)
    if pkt:
        try:
            info.asset_number_raw = \
                parse_extended_validation_status(pkt.data).asset_number_raw
        except ValueError:
            pass
    return info
