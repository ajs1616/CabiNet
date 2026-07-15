# SAS — CabiNet's SAS bridge stack

This tree is the host side of CabiNet's SAS leg: a small Raspberry Pi "SMIB"
(a used **3B+** is the recommended board — built-in Ethernet) sits on the
machine's SAS serial port, runs `sas_host.py`, and bridges the machine to the
CabiNet host over Ethernet. Live-proven daily on a WMS BB2 — meters, AFT credit
transfers both directions, cross-machine TITO, legacy bonusing, and handpay
reset all run on real iron.

## What is SAS?

SAS (Slot Accounting System) is the serial protocol most slot machines from
the mid-90s onward speak: RS-232-level signaling at 19200 baud, 8 data bits,
1 stop bit, with a 9-bit "wakeup" address scheme done via mark/space parity.
The host polls; the machine answers. It carries accounting/meters, cashless
transfers (AFT), TITO ticket validation, bonusing, and handpay handling.

## Layout (everything here ships and is tested)

```
SAS/
├── sas_host.py            # THE entry point: poller loop + hub bridge
├── core/                  # validated protocol stack
│   ├── sas_protocol.py    #   framing + CRC-16/Kermit (LSB-first, live-proven)
│   ├── sas_poller.py      #   the poll loop / long-poll scheduler
│   ├── sas_meters.py      #   meter reads (0x1A credits = display authority)
│   ├── sas_tito_host.py   #   TITO host: secure enhanced validation, 0x70/0x71
│   ├── sas_ticket_store.py#   ticket ledger client (hub is the authority)
│   ├── hub_ticket_client.py#  hub REST client for the shared TITO ledger
│   ├── sas_bonus.py       #   legacy bonusing (4-byte-BCD wire max)
│   ├── sas_handpay_reset.py#  remote handpay reset (to credits)
│   ├── sas_machine_info.py#   machine identity / game info reads
│   └── sas_exceptions.py  #   real-time exception (event) decoding
├── modules/aft/           # AFT: host-side transfer state machine (0x72–0x74)
├── transport/serial/      # PL011 serial with 9-bit wakeup + RX-wedge watchdog
├── tests/                 # `pytest SAS/` — the regression gate, no hardware needed
├── tools/sas_bench_poll.py# bare-wire bench probe
├── ARCHITECTURE.md        # how the pieces fit
└── BENCH_AFT_CASHOUT.md   # bench runbook for machine→wallet cash-out
```

Dependencies: `pyserial`, `crcmod`, `loguru` (`pip install -r requirements.txt`).
Everything else in CabiNet's core is stdlib-only; this tree is the exception
because it touches real serial hardware.

## Terminology note — "enhanced validation"

The live host runs SAS **secure enhanced validation** mode: a one-time 0x4C
validation-ID seed answers the EGM's 0x3F tilt, the EGM then self-mints its
16-digit validation numbers, and the host captures tickets via 0x4D into the
hub ledger. The system-validation responder (0x57 → 0x58, host-minted) is the
dormant, toggleable fallback. Mode matrix: `COMPATIBILITY.md` § "SAS
validation modes".

## Running it

Normally you don't run this by hand — `deploy/zero2w_sas_setup.sh` (works on
any PL011 Pi despite the name) installs it as the `casinonet-sas` unit on the
SMIB Pi (see `deploy/SMIB_FRESH_IMAGE.md`).
Manually:

```bash
python3 sas_host.py /dev/ttyAMA0 --address 1 --hub auto
```

`--hub auto` = zero-config: the SMIB derives the hub URL from its default
gateway (the CabiNet host is the DHCP server on the slot segment).

## Hardware

- Pi 3B+ recommended (built-in Ethernet); any Pi with a PL011 UART works
  (a Zero 2 W needs a USB-Ethernet HAT on the wired-only floor)
- RS-232 level shifter (a $5 "MAX3232 RS232 to TTL" board)
- SAS harness: TX, RX, GND to the machine's SAS port, 19200 baud
- The PL011 requirement is real: 9-bit wakeup needs per-byte parity switching

## Testing

```bash
pytest SAS/ -q        # full gate, mock serial, no hardware
```

Bench-with-real-iron runbooks: `BENCH_AFT_CASHOUT.md` here and
`deploy/SMIB_FRESH_IMAGE.md` for first-poll bring-up.

## Legal notice

This software is for **personal use only** with gaming equipment you legally
own. Not for commercial gaming operations. Check local laws regarding gaming
devices.
