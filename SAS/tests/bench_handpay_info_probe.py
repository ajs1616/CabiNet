#!/usr/bin/env python3
"""
bench_handpay_info_probe.py — ONE live pass on the BB2 (addr 1) to test AJ's
0x51 hypothesis (2026-07-08):

    The endless exception 0x51 stream is NOT a live handpay latch — the
    machine is playable and was keyed off long ago. It is the machine
    re-reporting, as STATUS, that it holds handpay INFORMATION the host has
    never collected. Per SAS §11 the host collects it with long poll 0x1B
    "Send Handpay Information"; the re-report should stop once read (and a
    0x1E-style ack cycle is NOT believed required — the read is the ack).

This probe, run with casinonet-sas STOPPED (it owns the port):
  1. opens the REAL transport (9-bit wakeup, the b7f8677 timing fix),
  2. general-polls a few times — expect the familiar 0x51 chirps,
  3. sends LP 0x1B as a bare 2-byte R poll (the bench-proven no-CRC family:
     0x1A/0x1F/0x54 all answer this way on this BB2) and prints the RAW
     response hex — the layout gets adjudicated later, capture comes first,
  4. general-polls again and reports whether the 0x51 stream stopped,
  5. if a CRC'd variant is needed (silence on the bare form), retries 0x1B
     as [addr][1B][CRC] once.

Run on the Zero:
  sudo systemctl stop casinonet-sas
  ~/venvs/casinonet/bin/python tests/bench_handpay_info_probe.py
  sudo systemctl restart casinonet-sas
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sas_protocol import SASProtocol                     # noqa: E402
from transport.serial.sas_serial import SASSerialPort         # noqa: E402

PORT = "/dev/ttyAMA0"
ADDR = 1
CMD_SEND_HANDPAY_INFO = 0x1B
PACE = 0.2


def hexs(b):
    return b.hex() if b else "(silence)"


def main():
    proto = SASProtocol()
    port = SASSerialPort(PORT)
    print(f"port {PORT} open — probing addr {ADDR}")

    def gp_burst(label, n=5):
        codes = []
        for _ in range(n):
            # general poll = single byte 0x80|addr; answer = ONE exception
            r = port.transact(bytes([0x80 | ADDR]), max_bytes=1)
            codes.append(r.hex() if r else "--")
            time.sleep(PACE)
        print(f"  {label}: {' '.join(codes)}")
        return codes

    # -- 1: baseline — expect 0x51 chirps ---------------------------------
    print("— baseline general polls")
    before = gp_burst("before")

    # -- 2: DRAIN LOOP — first probe run (2026-07-08) proved the machine
    # holds a QUEUE of unread handpay records: one 0x1B read produced a real
    # 24-byte record, the machine acked with exception 0x52 ("handpay was
    # reset"), then resumed 0x51 for the NEXT record. Read until the 0x51
    # chirping dies (cap 25 — a year of key-offs, not unbounded).
    records = []
    for i in range(25):
        time.sleep(PACE)
        resp = port.transact(bytes([ADDR, CMD_SEND_HANDPAY_INFO]))
        print(f"  0x1B read #{i + 1}: {hexs(resp)}")
        if resp:
            records.append(resp.hex())
        else:
            break
        # let the machine cycle its 51/52 reporting between reads
        codes = gp_burst(f"  polls after #{i + 1}", n=6)
        if "51" not in codes:
            print("  — no 0x51 in the window; queue looks drained")
            break

    # -- 4: final check ----------------------------------------------------
    print("— final general polls")
    time.sleep(PACE)
    after = gp_burst("after", n=10)
    print()
    print(f"captured {len(records)} raw record(s) for adjudication:")
    for r in records:
        print(f"  {r}")

    still = sum(1 for c in after if c == "51")
    was = sum(1 for c in before if c == "51")
    print()
    print(f"RESULT: 0x51 in {was}/{len(before)} polls before, "
          f"{still}/{len(after)} after.")
    if was and not still:
        print("HYPOTHESIS CONFIRMED: the 0x1B read drained the pending "
              "handpay record — 0x51 was 'info awaiting pickup', not a "
              "live latch.")
    elif resp:
        print("Info captured but the stream persists — the record may need "
              "an explicit ack cycle (adjudicate the raw bytes + spec §11).")
    else:
        print("No 0x1B answer — capture the raw above and adjudicate "
              "framing before concluding anything.")


if __name__ == "__main__":
    main()
