# Companion — the RFID tap daemon

Turns PN532 card taps into hub knowledge. Runs on the Zero **beside**
`casinonet-sas` (one Zero = SAS + NFC, proven 2026-07-10). A single thread
polls the reader at ~5Hz, debounces a held card into one tap, and POSTs
`{tapId, uid, at}` to the hub's `/api/companion/report`. Everything smart —
fob lookup, tiers, G2S `setIdValidation` carded sessions, reset-tier SAS
handpay reset — lives in the hub (`G2S/g2s_host.py`); the companion stays a
dumb satellite on purpose.

- `reader.py` — stdlib PN532 I2C driver (verbatim port of the live-proven
  bring-up script; first card read: UID `6CB16F06`, S50 1K) + `MockRfidReader`.
- `companion_host.py` — the daemon: debounce, bounded tap queue (64), ack
  watermark, edge-logged hub reporting.
- `casinonet-companion.service` — systemd unit for the Zero.
- `tests/test_companion.py` — `pytest Companion/ -q` (no hardware needed).

## Wiring recap (PN532 V3 on the SAS Zero)

The Zero's hardware I2C pins are taken by the MAX232 SAS HAT, so the PN532
rides a **software i2c-gpio bus** on spare GPIOs:

| PN532 pin | Zero pin              |
|-----------|-----------------------|
| SDA       | GPIO23 (physical 16)  |
| SCL       | GPIO24 (physical 18)  |
| VCC       | 3.3V (physical 17)    |
| GND       | GND (physical 20)     |

1. Set the board's DIP switches to **I2C** mode (it answers at `0x24`).
2. `/boot/firmware/config.txt`:
   `dtoverlay=i2c-gpio,i2c_gpio_sda=23,i2c_gpio_scl=24,bus=11`
3. Make sure the `i2c-dev` module loads (add `i2c-dev` to `/etc/modules`
   if `/dev/i2c-11` doesn't appear after reboot).
4. Sanity check: `i2cdetect -y 11` shows `0x24`.

Gotchas already paid for: swapping SDA/SCL reads as a dead bus; without
`i2c-dev` there is no `/dev/i2c-11` node at all; the DIP switches left in
HSU/SPI mode make the chip mute (commands never ack — at bring-up OR mid-run
a run of missed ACKs surfaces as reader trouble, so the daemon reports
`readerOk=false` and self-heals/retries rather than crashing or going
silently deaf).

## Deploy

Use `deploy/companion_setup.sh <user>@<zero-host>` from the repo root — it
enables the i2c-gpio overlay, rsyncs this directory, and installs the unit
(see `deploy/SMIB_FRESH_IMAGE.md` for the full fresh-SD runbook). Then:

```sh
journalctl -u casinonet-companion -f     # expect: PN532 ready, then 💳 taps
```

Stdlib-only — the Zero's system `python3` is enough, no venv. With zero-config
onboarding the reader self-IDs to the host and you bind it to a machine from
the web UI (**Players ▸ Readers ▸ Assign**); `--g2s-egm` / `--sas-smib`
flags on the `ExecStart` line are the manual-bind fallback.

No-hardware smoke test: `python3 companion_host.py http://127.0.0.1:8081 --mock`.
