#!/usr/bin/env python3
"""
bench_longpoll_probe.py -- ONE live diagnostic pass on the WMS BB2 (SAS addr 1)
to decisively reveal why multi-byte long polls get zero response while the
1-byte general poll answers.

Ground truth from tonight's bench:
  * RX path is perfect (40 clean 0x01 chirps captured passively).
  * General poll 0x81 got real answers (0x00 no-activity, 0x6C AFT-request).
  * EVERY multi-byte long poll got ZERO response under every parity scheme.

Two competing explanations this run separates:
  (A) Frame CONTENT: type-R read polls (0x1A/0x1F/0x54) must be a BARE 2-byte
      [addr,cmd] frame with NO CRC. Appending a CRC turns them into a 4-byte
      frame the machine silently discards -> would fail under ALL parity
      schemes (matches "no-crc works, crc'd fails").
  (B) Frame TIMING/FRAMING: the mid-frame mark/space parity switch inserts a
      >5ms inter-byte gap -> multi-byte frames dropped. saspy (commands real
      machines) avoids it: plain 8N1, ONE write of the whole frame.

This script opens plain 8N1 (like saspy) and sends each frame as a single
write, so a PASS here indicts our transport's parity switch, and the
0x1A no-crc-vs-crc split indicts (or clears) the type-R CRC rule.

CRC byte order: adjudicated LSB-first (spec Section 5 + our sas_protocol.py;
saspy's PyCRC byte-swaps then appends "high-first", producing the identical
LSB-true-first wire bytes). We send LSB-first by default; a helper builds the
MSB-first variant so a skeptic can A/B it.

Run on the Pi Zero:  python3 bench_longpoll_probe.py
Reads/writes /dev/ttyAMA0 only. Nothing else touched.
"""

import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial required: pip install pyserial")

PORT = "/dev/ttyAMA0"
BAUD = 19200
ADDR = 0x01
ATTEMPTS = 6                # each poll is tried 6x -> pass/fail (N/6)
FIRST_BYTE_TIMEOUT = 0.080  # machine has 20ms to START; be lenient
GAP_TIMEOUT = 0.006         # SAS >5ms inter-byte gap == end of frame
IDLE_GAP = 0.300            # >=250ms between polls; swallow the 0x01 chirps


# --------------------------------------------------------------------------
# Kermit CRC (poly 0x1021, refin/refout=true, init 0, xorout 0).
# Canonical check: crc(b"123456789") == 0x2189.
# --------------------------------------------------------------------------
def kermit(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


assert kermit(b"123456789") == 0x2189, "Kermit CRC self-check failed"


def frame_crc_lsb(body: bytes) -> bytes:
    """CONCLUDED order: append CRC least-significant byte first (spec-correct,
    == our sas_protocol.build_packet / aft_handler._crc_frame)."""
    return body + kermit(body).to_bytes(2, "little")


def frame_crc_msb(body: bytes) -> bytes:
    """OTHER order, for A/B only. Do NOT ship this."""
    return body + kermit(body).to_bytes(2, "big")


def frame_no_crc(body: bytes) -> bytes:
    """Type-R read poll: bare [addr,cmd], no CRC."""
    return body


# --------------------------------------------------------------------------
# Transport: plain 8N1, ONE write per frame (the saspy method).
# --------------------------------------------------------------------------
def open_port() -> "serial.Serial":
    return serial.Serial(
        port=PORT,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,   # plain 8N1 -- no mid-frame parity switch
        stopbits=serial.STOPBITS_ONE,
        timeout=FIRST_BYTE_TIMEOUT,
    )


def transact(ser: "serial.Serial", frame: bytes) -> bytes:
    """Send the WHOLE frame in one write(); wait up to FIRST_BYTE_TIMEOUT for
    byte 1, then read byte-by-byte until a >GAP_TIMEOUT silence."""
    ser.reset_input_buffer()          # drop any queued 0x01 chirp
    ser.write(frame)
    ser.flush()
    ser.timeout = FIRST_BYTE_TIMEOUT
    first = ser.read(1)
    if not first:
        return b""
    out = bytearray(first)
    ser.timeout = GAP_TIMEOUT
    while len(out) < 256:
        b = ser.read(1)
        if not b:
            break
        out += b
    ser.timeout = FIRST_BYTE_TIMEOUT
    return bytes(out)


def run_poll(ser, label, frame) -> None:
    """Fire `frame` ATTEMPTS times; a non-empty read counts as a response.
    A lone echo of our own address (chirp) does not count."""
    hits = 0
    samples = []
    for _ in range(ATTEMPTS):
        time.sleep(IDLE_GAP)
        resp = transact(ser, frame)
        # A bare 0x01 is the idle chirp, not an answer to our poll.
        real = bool(resp) and not (len(resp) == 1 and resp[0] == ADDR)
        if real:
            hits += 1
            samples.append(resp.hex().upper())
    verdict = "PASS" if hits else "fail"
    sample = ("  e.g. " + samples[0]) if samples else ""
    print(f"  [{verdict} {hits}/{ATTEMPTS}] {label:<34} "
          f"tx={frame.hex().upper()}{sample}")


def main() -> None:
    print(f"Opening {PORT} @ {BAUD} 8N1 (plain, single-write, saspy-style)\n")
    ser = open_port()
    try:
        # (a) SANITY: general poll, single byte 0x80|addr, NO CRC.
        print("(a) general poll [sanity -- known-good 1-byte path]")
        run_poll(ser, "general poll 0x81", bytes([0x80 | ADDR]))

        # (b) current credits 0x1A -- BOTH ways, the decisive split.
        print("\n(b) current credits 0x1A  (type-R: spec says NO crc)")
        run_poll(ser, "0x1A NO crc  [spec-correct]",
                 frame_no_crc(bytes([ADDR, 0x1A])))
        run_poll(ser, "0x1A WITH crc (LSB) [our old bug?]",
                 frame_crc_lsb(bytes([ADDR, 0x1A])))

        # (c) SAS version 0x54 with crc, LSB order.
        print("\n(c) SAS version 0x54")
        run_poll(ser, "0x54 WITH crc (LSB)",
                 frame_crc_lsb(bytes([ADDR, 0x54])))
        run_poll(ser, "0x54 NO crc  [if type-R]",
                 frame_no_crc(bytes([ADDR, 0x54])))

        # (d) AFT register interrogate 0x73/0x01/0xFF -- CRC required.
        print("\n(d) AFT register interrogate 0x73 (crc REQUIRED)")
        run_poll(ser, "0x73 01 FF WITH crc (LSB)",
                 frame_crc_lsb(bytes([ADDR, 0x73, 0x01, 0xFF])))
        # A/B the byte order once, in case the BB2 disagrees with spec.
        run_poll(ser, "0x73 01 FF WITH crc (MSB) [A/B]",
                 frame_crc_msb(bytes([ADDR, 0x73, 0x01, 0xFF])))

        # Reference: the exact register frame for asset 1000 (not sent here --
        # registering is a state change; interrogate above is read-only).
        body = (bytes([ADDR, 0x73, 0x1D, 0x01])
                + (1000).to_bytes(4, "little")   # asset 1000, binary LE
                + bytes([0x01] * 20)             # 20-byte reg key
                + bytes([0x00, 0x00, 0x00, 0x01]))  # POS ID = 1
        print("\nREGISTER FRAME (asset 1000, LSB crc, reference only):")
        print("  " + frame_crc_lsb(body).hex().upper())
    finally:
        ser.close()

    print("\nHow to read this run:")
    print("  * (a) PASS confirms the link/8N1 path is alive this session.")
    print("  * 0x1A NO-crc PASS but WITH-crc fail  => the type-R CRC rule is")
    print("    the bug: stop appending CRC to read polls.")
    print("  * Both 0x1A variants PASS where our old stack failed => our")
    print("    transport's mid-frame parity switch was the culprit (this")
    print("    script never switches parity).")
    print("  * 0x73 PASS on one CRC order, fail on the other => the BB2 fixes")
    print("    the CRC byte order; keep the winning one.")


if __name__ == "__main__":
    main()
