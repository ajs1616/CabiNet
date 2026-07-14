#!/usr/bin/env python3
"""
SAS bench poller — first-contact tool for a real machine.

Runs the host side of a SAS loop against one machine: general polls at a
steady cadence (decoding exception codes), with an optional meter poll mixed
in. Logs every TX/RX byte in hex so the session doubles as a capture for
COMPATIBILITY.md and CRC adjudication.

Usage (on the Pi, machine at default address 1):
    python3 tools/sas_bench_poll.py /dev/ttyUSB0
    python3 tools/sas_bench_poll.py /dev/ttyAMA0 --address 3 --credits
    python3 tools/sas_bench_poll.py --mock          # self-test, no hardware

If the machine never answers ANYTHING:
  1. wake-up bit — cheap USB adapters (CH340) often can't do mark/space
     parity; use the Pi UART + level shifter or an FTDI adapter;
  2. wiring/null-modem; 3. machine address (operator menu); 4. SAS enabled
     on the machine at all (some games need the comm channel switched on).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sas_protocol import SASProtocol, sas_crc, SAS_RESPONSE_TIMEOUT_MS
from transport.serial.sas_serial import MockSASSerialPort

# Exception codes required by the implementation guide (Montana DOJ v1.5.0 p.9)
EXCEPTIONS = {
    0x00: "no activity",
    0x11: "slot door open", 0x12: "slot door closed",
    0x13: "drop door open", 0x14: "drop door closed",
    0x15: "card cage open", 0x16: "card cage closed",
    0x17: "AC power applied", 0x18: "AC power lost",
    0x19: "cashbox door open", 0x1A: "cashbox door closed",
    0x1B: "cashbox removed", 0x1C: "cashbox installed",
    0x1D: "belly door open", 0x1E: "belly door closed",
    0x27: "cashbox full", 0x28: "bill jam",
    0x29: "bill acceptor hardware failure", 0x2B: "bill rejected",
    0x3C: "operator changed configuration", 0x3D: "cash out ticket printed",
    0x3F: "validation ID not configured",
    0x47: "$1 bill accepted", 0x48: "$5 bill accepted",
    0x49: "$10 bill accepted", 0x4A: "$20 bill accepted",
    0x60: "printer comm error", 0x61: "printer paper out",
    0x70: "exception buffer overflow", 0x7A: "game soft meter reset",
    0x86: "game out of service", 0x8C: "game selected",
}


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b) if b else "(silence)"


def make_mock_machine(protocol: SASProtocol, address: int):
    """A pretend BB2E for --mock self-tests."""
    state = {"polls": 0}

    def machine(frame, wakeup):
        if not wakeup[0]:
            return b""  # never addressed without the wake-up bit
        state["polls"] += 1
        if frame == bytes([0x80 | address]):
            # every 5th poll, report a door-closed exception
            return b"\x12" if state["polls"] % 5 == 0 else b"\x00"
        if frame == bytes([address, 0x1A]):
            body = bytes([address, 0x1A]) + b"\x00\x00\x42\x00"  # 4200 credits
            return body + sas_crc(body).to_bytes(2, "little")
        return b""  # unsupported command -> silence, per spec

    return machine


def chirp_listen(port, address):
    """Passively listen for the machine's 'chirp' — a lone address byte every
    ~200 ms after 5 s of host silence. Catching it proves the RX path AND the
    wake-up framing independently of our TX, which separates a wiring/parity
    problem from a TX problem."""
    print("\n[chirp] listening 3 s for the machine's idle chirp (no polling)...")
    seen = bytearray()
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        seen += port.listen(0.5)
    if seen:
        print(f"[chirp] heard {hexs(bytes(seen))}")
        if bytes([address]) in seen or bytes([address | 0x80]) in seen:
            print(f"[chirp] -> contains address {address:#04x}: RX path + "
                  f"wake-up framing are GOOD. A dead poll is then a TX-side "
                  f"problem (parity switch timing / latency timer).")
        else:
            print("[chirp] -> bytes seen but not our address; check address "
                  "setting / wiring.")
    else:
        print("[chirp] silence — likely RX wiring (machine TX -> Pi RX) or "
              "the machine isn't chirping (SAS disabled / wrong port).")


def main():
    ap = argparse.ArgumentParser(description="SAS bench poller")
    ap.add_argument("port", nargs="?", help="serial device, e.g. /dev/ttyUSB0")
    ap.add_argument("--address", type=int, default=1, help="machine address (1-127)")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="poll cadence seconds (default 200ms — the SAS floor; "
                         "do not go below 0.2 for unknown firmware)")
    ap.add_argument("--credits", action="store_true",
                    help="interleave a 0x1A current-credits poll every 10 cycles")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (default: run until Ctrl+C)")
    ap.add_argument("--mock", action="store_true",
                    help="self-test against an in-memory machine")
    args = ap.parse_args()

    if not (1 <= args.address <= 127):
        ap.error(f"--address must be 1..127 (got {args.address}); "
                 "address 0 turns SAS off and is reserved")
    if args.interval < 0.2 and not args.mock:
        print(f"[warn] --interval {args.interval}s is below the 200ms SAS "
              f"floor; a real machine may ignore polls this fast.")

    protocol = SASProtocol()

    if args.mock:
        port = MockSASSerialPort(make_mock_machine(protocol, args.address))
        print("[mock] in-memory machine on address", args.address)
    elif args.port:
        from transport.serial.sas_serial import SASSerialPort
        port = SASSerialPort(args.port)
        ll = "low-latency ON" if port.low_latency else "low-latency N/A"
        print(f"[open] {args.port} @ 19200 8-mark/space-1, {ll}, "
              f"50ms first-byte / 6ms gap read")
    else:
        ap.error("either a serial port or --mock is required")

    general = protocol.build_general_poll(args.address)
    sync_ack = protocol.build_general_poll(0)            # 0x80: sync + impl. ACK
    credits = protocol.build_short_poll(args.address, 0x1A)
    n = answered = 0
    started = time.monotonic()
    # Lead with a sync poll so a freshly-started / desynced machine resets its
    # poll-state counter and will answer (SAS §3.3).
    port.transact(sync_ack)

    try:
        while True:
            n += 1
            want_credits = args.credits and n % 10 == 0
            frame = credits if want_credits else general
            resp = port.transact(frame)
            if resp:
                answered += 1

            if not resp:
                if n % 25 == 1:
                    print(f"[{n:5d}] TX {hexs(frame)}  RX (silence) "
                          f"[{answered}/{n} answered]")
            else:
                # context: a general poll -> exception byte; a 0x1A -> frame
                last = 'transfer' if False else None
                r = protocol.parse_response(resp, last_poll=last,
                                            address=args.address)
                t = r['type']
                if t == 'exception' and not r.get('idle'):
                    code = r['value']
                    print(f"[{n:5d}] TX {hexs(frame)}  RX {hexs(resp)}  <- "
                          f"{EXCEPTIONS.get(code, f'exception {code:02X}?')}")
                    port.transact(sync_ack)          # ACK it (implied-ACK rule)
                elif t == 'busy':
                    print(f"[{n:5d}] TX {hexs(frame)}  RX {hexs(resp)}  <- "
                          f"machine BUSY (will retry)")
                elif t == 'packet':
                    pkt = r['packet']
                    print(f"[{n:5d}] TX {hexs(frame)}  RX {hexs(resp)}  <- CRC OK")
                    if pkt.command == 0x1A:
                        print(f"        current credits: "
                              f"{protocol._bcd_to_int(pkt.data)}")
                elif t == 'crc_error':
                    print(f"[{n:5d}] TX {hexs(frame)}  RX {hexs(resp)}  <- "
                          f"CRC BAD on a {len(resp)}-byte frame (CAPTURE THIS — "
                          f"adjudicates Kermit-vs-XMODEM)")
                elif t == 'short':
                    print(f"[{n:5d}] TX {hexs(frame)}  RX {hexs(resp)}  <- "
                          f"runt {len(resp)} bytes (likely truncated — check "
                          f"latency timer / gap timeout, NOT a CRC problem)")

            if args.duration and time.monotonic() - started >= args.duration:
                break
            if args.mock and n >= 30:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    rate = answered / n * 100 if n else 0
    print(f"\n{n} polls, {answered} answered ({rate:.0f}%).")
    if answered:
        print("Machine is ALIVE on the loop 🎰")
    else:
        print("Dead silence. Checklist (in order):")
        print("  1. latency timer — FTDI must be 1ms (we set low-latency mode;"
              " confirm '[open] ... low-latency ON' above)")
        print("  2. wake-up parity — does the adapter do mark/space? (FTDI yes,"
              " CH340 often no)")
        print("  3. wiring — machine TX -> Pi RX, machine RX -> Pi TX, GND")
        print("  4. address — does --address match the operator-menu setting?")
        print("  5. SAS enabled on the machine / correct port?")
        if not args.mock and args.port:
            chirp_listen(port, args.address)   # free RX-path diagnostic

    if not args.mock and args.port:
        port.close()
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
