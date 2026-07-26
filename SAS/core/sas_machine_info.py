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

Added 2026-07-25 from the full SAS 6.02 spec (section/table citations are in
each function's docstring):
  0xB1  §16.3    current PLAYER denomination        (Table 16.3)
  0xB2  §16.4    enabled PLAYER denominations       (Table 16.4)
  0xB5  §7.23    extended game info — game/paytable NAMES, max bet, SAS
                 progressive group + level bitmap   (Tables 7.23a/b)
None of those three has ever been on a wire from this project. They are
implemented, unit-tested, and explicitly UNPROVEN.

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
    spec; do NOT guess it. The accounting denom is CONFIGURATION (set up at
    RAM clear), not machine identity — and a MULTI-denom setup is REQUIRED to
    account at its minimum denom, in practice $0.01 (AJ 2026-07-22), so every
    multi-denom cabinet reads code 0x01; other codes come from single-denom /
    higher-minimum configs (forum bundles). The HARD mechanical meters may
    run at the HIGHEST denom on most titles — never reconcile these wire
    meters against the hard meters. Bench-confirmed rows so far:
      code 0x01 = $0.01  (WMS BB2, multi-denom accounting min, live 2026-07-22)
  * RTP field: 4 ASCII digits, "no decimal point, it is implied" — the guide
    does not say where. theoretical_rtp_pct assumes XX.XX (9250 -> 92.50%),
    the only reading that yields sane percentages; the raw string is kept.
    TODO(bench): confirm against a game with a known par sheet.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .sas_protocol import SASProtocol, DENOM_CODE_CENTS
from .sas_meters import (
    SAS_POLL_FLOOR, bcd_to_int, int_to_bcd, _crc_frame, build_meter_poll,
)

__all__ = [
    "GameConfig", "SASVersionInfo", "ExtendedValidationStatus", "MachineInfo",
    "ExtendedGameInfo",
    "parse_machine_id", "parse_game_config", "parse_sas_version",
    "parse_selected_game_number", "parse_enabled_game_numbers",
    "parse_extended_validation_status", "build_extended_validation_status_poll",
    "parse_current_player_denom", "parse_enabled_player_denoms",
    "build_extended_game_info_poll", "parse_extended_game_info",
    "denom_code_cents",
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


# ---------------------------------------------------------------------------
# SAS 6.02 §16.3 / §16.4 — the PLAYER denominations.
#
# ⚠️ THESE ARE NOT THE 0x1F DENOMINATION. Keep them separate forever:
#   * 0x1F byte 8 = the ACCOUNTING denomination. Per §16 the base accounting
#     denomination is reported to the host via long polls 1F and 53, and it is
#     the unit EVERY credit meter is reported in. It is fixed at RAM clear, and
#     enabling/disabling play denominations does not move it.
#   * B1 = the denomination the PLAYER currently has selected.
#   * B2 = the set of denominations currently AVAILABLE to the player.
# On the BB2 (2026-07-25) the accounting denom reads code 01 = 1c and 1c is
# also an enabled player denom, so the two coincide — which is exactly how a
# conflation hides for months. GameConfig.denomination_code stays the
# accounting one; these two functions own the player ones.
# ---------------------------------------------------------------------------

def denom_code_cents(code: Optional[int]):
    """Table C-4 code -> cents (float below a cent). None for an unknown or
    absent code — never a guess, and never 0 (a 0 would read as free play)."""
    if code is None:
        return None
    return DENOM_CODE_CENTS.get(code)


def parse_current_player_denom(data: bytes) -> int:
    """§16.3 Table 16.3, LP B1
    "Send Current Player Denomination" — type R, so the poll is a bare
    [addr][B1] with no CRC and the response is [addr][B1][denom][CRC] =>
    exactly ONE data byte. English 6.02, verbatim:

        "Current player denomination | 1 binary | 01-3F | Binary number
         representing the player denomination currently selected
         (see Table C-4 in Appendix C)"

    Returns the RAW Table C-4 code; use denom_code_cents() to translate.
    NEVER answered by a machine without multi-denom extensions — §16.3's last
    line, verbatim: "If a gaming machine does not support multi-denom
    extensions, it ignores this poll." So the caller must report silence as
    "unanswered", not "unsupported": §4.4 "If a gaming
    machine receives a long poll it does not support, it must ignore the long
    poll and not NACK it" makes the two indistinguishable on the wire.
    NOT WIRE-PROVEN on any machine as of 2026-07-25 — zero B1 frames have ever
    been sent from this project. Label results accordingly."""
    if len(data) != 1:
        raise ValueError(f"B1 current player denom: expected 1 data byte, "
                         f"got {len(data)}")
    code = data[0]
    if not 0x01 <= code <= 0x3F:
        raise ValueError(f"B1 denom code 0x{code:02X} outside Table 16.3's "
                         "stated 01-3F range")
    return code


def parse_enabled_player_denoms(data: bytes) -> List[int]:
    """§16.4 Table 16.4, LP B2
    "Send Enabled Player Denominations" — type R poll, response
    [addr][B2][len][count][denom]×count[CRC], so data = [len][count][codes...].
    English 6.02, verbatim:

        "Length | 1 binary | 01-80 | Total length of the bytes following, not
         including CRC"
        "Number of denominations | 1 binary | 00-7F | Number of player
         denominations currently enabled"
        "Player denomination | X binary | 01-3F | Binary number representing
         the denomination, times the number of player denominations enabled
         (see Table C-4 in Appendix C)"

    Returns the RAW Table C-4 codes in wire order.

    ⭐ WHY THIS MATTERS: it is the machine's OWN authoritative answer to "which
    denominations can the player pick right now". Everything else we have is
    inference — a 31-poll B0+56 sweep. It is the only safe basis for the house
    rule that at least one denomination must remain active, and exception 0x3C
    names B2 explicitly among the polls whose answers an operator config change
    invalidates: "Options changed by operator ... any option that affects the
    response to long polls 1F, 53, 54, 56, A0, B2, B3, B4 or B5".

    A count of 0 is legal per the table (00-7F) and is reported as an empty
    list — do NOT silently upgrade that to "unknown"; a machine claiming zero
    enabled player denominations is a finding worth surfacing.
    NOT WIRE-PROVEN as of 2026-07-25 — no B2 frame has ever left this host."""
    if len(data) < 2:
        raise ValueError("B2 response too short")
    declared, count = data[0], data[1]
    if declared != len(data) - 1:
        raise ValueError(f"B2 length byte {declared} != {len(data) - 1}")
    codes = list(data[2:2 + count])
    if len(codes) != count:
        raise ValueError(f"B2 count {count} != payload of {len(data) - 2} "
                         "bytes")
    return codes


# ---------------------------------------------------------------------------
# SAS 6.02 §7.23 — LP B5 extended game info
# ---------------------------------------------------------------------------

def build_extended_game_info_poll(address: int, game_number: int = 0) -> bytes:
    """§7.23 Table 7.23a, LP B5 —
    type M, so the game number is MANDATORY and the frame carries a CRC:

        [addr][B5][game# 2 BCD 0000-9999][CRC]

    game 0000 = the gaming machine as a whole. English 6.02, verbatim: "When the game number is zero, all games supported
    by the gaming machine are considered in the response to long poll B5,
    whether they are currently enabled or not." — i.e. B5 at game 0000 sees
    DISABLED games too, which no other poll we have does.

    ⚠️ 2026-07-25: a B0-wrapped B5 was sent from this project with the game
    number MISSING and drew Table 16.1c error 04. That frame was malformed by
    us; the machine's answer says nothing about B5 support. core.sas_protocol
    .MULTIDENOM_BASE_MIN_DATA now refuses to build it. Bare B5 has never been
    sent at all."""
    if isinstance(game_number, bool) or not isinstance(game_number, int) \
            or not 0 <= game_number <= 9999:
        raise ValueError("B5 game number must be 0..9999 (0 = the gaming "
                         "machine)")
    return _crc_frame(bytes([address, 0xB5]) + int_to_bcd(game_number, 2))


@dataclass(frozen=True)
class ExtendedGameInfo:
    """§7.23 Table 7.23b.
    game_name / paytable_name are the ONLY human-readable game and paytable
    names anywhere in SAS — the SAS-side analog of the IGT game-name DB, and
    the only way a SAS-only cabinet can label itself in the UI. Both are
    explicitly OPTIONAL ("Optional ASCII name of game n or game family" /
    "Optional ASCII name of paytable or collection of paytables"), so an empty
    string is a legal answer, not a parse failure."""
    game_number: Optional[int]
    max_bet: Optional[int]              # 2-byte BCD, in GAME CREDIT units
    progressive_group: int
    progressive_levels_raw: bytes       # 4 binary, lsb = level 1
    progressive_levels: List[int]       # 1-based SAS level numbers set
    game_name: str
    paytable_name: str
    wager_categories: Optional[int]     # >0 => LP B4 supported for this table


def parse_extended_game_info(data: bytes) -> ExtendedGameInfo:
    """§7.23 Table 7.23b: [len][game# 2 BCD][max bet 2 BCD][prog group 1]
    [prog levels 4][game-name len 1][game name ASCII][paytable-name len 1]
    [paytable name ASCII][wager categories 2 BCD].

    Trailing bytes beyond the defined fields are IGNORED, per §2.2.3: "si un host recibe una respuesta válida de longitud
    variable con más datos de los que espera, procesará la parte del mensaje
    que comprende e ignorará los bytes adicionales."

    progressive_levels decodes the 4 binary bytes LSB-first per §2.2.3 ("All
    data exchanged in binary format is sent least significant byte first") with
    Table 7.23b's own "lsb = level 1, msb = level 32, bit set for each SAS
    progressive level enabled". The raw bytes are kept alongside because this has
    never been seen on a wire — if a real machine ever contradicts the order,
    the raw field is the evidence, not a re-derivation."""
    if len(data) < 12:
        raise ValueError(f"B5 response too short: {len(data)} bytes")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"B5 length byte {declared} != {len(data) - 1}")
    game_number = bcd_to_int(data[1:3])
    max_bet = bcd_to_int(data[3:5])
    prog_group = data[5]
    levels_raw = data[6:10]
    mask = int.from_bytes(levels_raw, "little")
    levels = [i + 1 for i in range(32) if mask & (1 << i)]
    i = 10
    name_len = data[i]
    i += 1
    if i + name_len > len(data):
        raise ValueError("B5 game name overruns frame")
    game_name = _ascii(data[i:i + name_len])
    i += name_len
    if i >= len(data):
        raise ValueError("B5 truncated before the paytable-name length")
    pt_len = data[i]
    i += 1
    if i + pt_len > len(data):
        raise ValueError("B5 paytable name overruns frame")
    paytable_name = _ascii(data[i:i + pt_len])
    i += pt_len
    wager_categories = (bcd_to_int(data[i:i + 2])
                        if i + 2 <= len(data) else None)
    return ExtendedGameInfo(
        game_number=game_number,
        max_bet=max_bet,
        progressive_group=prog_group,
        progressive_levels_raw=bytes(levels_raw),
        progressive_levels=levels,
        game_name=game_name,
        paytable_name=paytable_name,
        wager_categories=wager_categories,
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
