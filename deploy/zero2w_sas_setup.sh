#!/usr/bin/env bash
# zero2w_sas_setup.sh — turn a freshly-flashed Raspberry Pi into a SAS SMIB.
#
# DESPITE THE NAME this works on ANY Pi-family board with a PL011 UART —
# Zero 2 W, Zero W, 3A+, 3B/3B+, 4B (all use the SAME disable-bt recipe;
# the name stays for muscle memory). Board menu + full fresh-image runbook:
# deploy/SMIB_FRESH_IMAGE.md.
#
# Run FROM THE DEV BOX against a booted, network-joined Pi (flash with
# Raspberry Pi Imager: set hostname, your user, SSH — same recipe as
# the host box; boards with Ethernet may just be plugged in). Idempotent —
# safe to re-run.
#
#   deploy/zero2w_sas_setup.sh aj@smib-bb2.local [-i ~/.ssh/casinonet] \
#       [--port /dev/ttyAMA0] [--address 1] [--hub auto|http://192.168.50.2:8081]
#
# TWO GOLDEN IMAGES (2026-07-13): a satellite is either a G2S COMPANION (Pi +
# PN532 RFID only — deploy/companion_setup.sh) or a SAS SMIB (Pi + PN532 + a
# MAX232 level shifter + this SAS stack). Both self-configure from ONE identical
# image of their kind: the SAS smibId now defaults to smib-<pi-serial-tail>
# (like the companion's id), so two identical SAS cards don't collide their
# floor legs — no --smib-id needed. A SAS SMIB's RFID reader binds to its own
# cabinet AUTOMATICALLY (co-location: the reader and this daemon report from the
# same IP), so it never needs assigning in the UI; only pure-G2S readers do.
# (The existing smib-bb2 keeps its hand-set name — pin --smib-id there before
# any redeploy so its sasLink / nickname / AFT registration don't churn.)
#
# --hub defaults to 'auto' (v11 zero-config): the daemon derives the hub from
# the DHCP default gateway — the host IS the gateway + DHCP server on the
# wired slot segment, so no hardcoded IP is needed. Satellites are WIRED-ONLY
# (the Wi-Fi path is retired — it dropped/delayed the PULL-only report leg):
# plug the Zero's USB-Eth HAT into the slot-segment switch (the host serves
# DHCP + G2S there) and it comes up on .50.x, one local hop from the host.
# Pass --hub http://127.0.0.1:8081 for a host-co-located SAS leg.
#
# What it does on the SMIB:
#   1. UART for SAS — THE PI TRAP (every BT-equipped model): the good PL011
#      UART is claimed by Bluetooth; the GPIO14/15 fallback is the
#      mini-UART, which has NO PARITY support and can never speak SAS
#      (9-bit wakeup = mark/space parity per byte). Fix: dtoverlay=
#      disable-bt (+ enable_uart=1) hands the PL011 to the header as
#      /dev/ttyAMA0; hciuart disabled. Identical on Zero W/Zero 2 W/3A+/
#      3B+/4B (a cabinet SMIB has no use for BT; if you ever need BT on a
#      Pi 4, dtoverlay=uart3 exposes a SPARE PL011 on GPIO4/5 instead —
#      then pass --port /dev/ttyAMA1). Also strips console=serial0 from
#      cmdline.txt and masks the serial getty — with BT off, serial0 IS
#      ttyAMA0 and a console there would eat the SAS line.
#   2. ~/venvs/casinonet venv with pyserial/crcmod/loguru/pytest.
#   3. rsyncs the SAS tree (excludes /data — the ticket-money ledger never
#      travels; same GR-04 rule as the G2S voucher store).
#   4. Installs + enables casinonet-sas.service (port/address from args).
#   5. Reboots (config.txt changes) and verifies /dev/ttyAMA0 + service.
#
# After it finishes: wire the RS232 level shifter to the BB2E's SAS port
# (TX->RX, RX->TX, GND), set SAS address (default 1) + enable the SAS
# channel in the WMS operator menu. First contact:
#   sudo systemctl stop casinonet-sas       # the service holds the port
#   ~/venvs/casinonet/bin/python tools/sas_bench_poll.py /dev/ttyAMA0 --credits
#   sudo systemctl start casinonet-sas      # then watch: journalctl -u casinonet-sas -f
# A CRC-BAD answer is GOOD news (machine alive; capture it for
# COMPATIBILITY.md — it settles Kermit-vs-XMODEM for the WMS fleet).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="" ; SSHKEY="" ; PORT="/dev/ttyAMA0" ; ADDRESS="1"
HUB="auto"   # v11 zero-config: derive the hub from the default gateway (see header); override with --hub
SMIB_ID=""   # empty = the daemon's default (smib-<pi-serial-tail>); PIN an existing hostname-named
             # SMIB with its CURRENT name so a redeploy doesn't rename its floor leg (see header).

while [ $# -gt 0 ]; do
    case "$1" in
        -i) SSHKEY="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --address) ADDRESS="$2"; shift 2 ;;
        --hub) HUB="$2"; shift 2 ;;
        --smib-id) SMIB_ID="$2"; shift 2 ;;
        *) if [ -z "$HOST" ]; then HOST="$1"; shift; else
               echo "unknown arg: $1" >&2; exit 2; fi ;;
    esac
done
[ -n "$HOST" ] || { echo "usage: $0 aj@<zero-host> [-i key] [--port dev] [--address n] [--hub url] [--smib-id name]" >&2; echo "  (--smib-id: PIN an existing hostname-named SMIB, e.g. --smib-id smib-bb2, so a redeploy keeps its leg name)" >&2; exit 2; }

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)
[ -n "$SSHKEY" ] && SSH+=(-i "$SSHKEY")
RSYNC_SSH="${SSH[*]}"

say() { printf '\n=== %s\n' "$*"; }

say "checking SSH + passwordless sudo on $HOST"
"${SSH[@]}" "$HOST" 'sudo -n true' || {
    echo "need passwordless sudo on the Zero first (one-time, run there):" >&2
    echo "  echo \"\$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/010-\$USER-nopasswd" >&2
    exit 1; }

# The shipped unit carries placeholder user/home values — rewrite them for
# whoever this Zero actually runs as (applied in the unit-install sed below).
RUSER=$("${SSH[@]}" "$HOST" 'id -un')
RHOME=$("${SSH[@]}" "$HOST" 'echo "$HOME"')

say "UART: PL011 to the header, console off"
"${SSH[@]}" "$HOST" '
set -e
MODEL=$(tr -d "\0" < /proc/device-tree/model 2>/dev/null || echo unknown)
echo "board: $MODEL"
case "$MODEL" in
  *"Zero 2"*|*"Zero W"*|*"Pi 3"*|*"Pi 4"*) : ;;   # the proven PL011 recipe
  *"Pi 5"*) echo "NOTE: Pi 5 — UARTs live on the RP1; disable-bt still frees ttyAMA0 on current firmware, but verify the port after reboot" ;;
  *) echo "WARNING: unrecognized board ($MODEL) — PL011 assumptions unverified; check /dev/ttyAMA0 exists after reboot" ;;
esac
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt
sudo cp "$CFG" "$CFG.bak-sas" 2>/dev/null || true
grep -q "^dtoverlay=disable-bt" "$CFG" || printf "\n# SAS SMIB: PL011 -> GPIO14/15 (mini-UART has no parity, cannot do SAS)\nenable_uart=1\ndtoverlay=disable-bt\n" | sudo tee -a "$CFG" >/dev/null
sudo cp "$CMD" "$CMD.bak-sas" 2>/dev/null || true
sudo sed -i "s/console=serial0,[0-9]* //; s/console=ttyAMA0,[0-9]* //" "$CMD"
sudo systemctl disable --now hciuart 2>/dev/null || true
sudo systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true
sudo usermod -aG dialout "$USER"
echo "uart config done"'

say "python venv + deps"
"${SSH[@]}" "$HOST" '
set -e
sudo apt-get -qq update && sudo apt-get -qq install -y python3-venv rsync >/dev/null
python3 -m venv ~/venvs/casinonet 2>/dev/null || true
~/venvs/casinonet/bin/pip -q install pyserial crcmod loguru pytest
~/venvs/casinonet/bin/python -c "import serial, crcmod, loguru; print(\"deps OK\")"'

say "rsync SAS tree (data excluded)"
rsync -a -e "$RSYNC_SSH" --exclude '/data' --exclude '__pycache__' \
      --exclude '.pytest_cache' "$REPO/SAS/" "$HOST:CasinoNet/SAS/"

say "gate: pytest SAS/ on the Zero"
"${SSH[@]}" "$HOST" 'cd ~/CasinoNet && ~/venvs/casinonet/bin/python -m pytest SAS/ -q 2>&1 | tail -1'

say "install casinonet-sas.service (port=$PORT address=$ADDRESS, hub=$HUB${SMIB_ID:+, smib-id=$SMIB_ID})"
# Append --smib-id ONLY when pinning an existing name; otherwise the daemon's
# default (smib-<pi-serial-tail>) stands so a golden image self-names per board.
sed -e "s|/dev/ttyAMA0|$PORT|" -e "s|--address 1|--address $ADDRESS|" \
    -e "s|--hub http://127.0.0.1:8081|--hub $HUB${SMIB_ID:+ --smib-id $SMIB_ID}|" \
    -e "s|^User=.*|User=$RUSER|" -e "s|^Group=.*|Group=$RUSER|" \
    -e "s|/home/aj|$RHOME|g" \
    "$REPO/deploy/casinonet-sas.service" | \
    "${SSH[@]}" "$HOST" 'sudo tee /etc/systemd/system/casinonet-sas.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable casinonet-sas >/dev/null 2>&1
echo "service installed + enabled"'

say "rebooting the Zero (config.txt changes)"
"${SSH[@]}" "$HOST" 'sudo reboot' || true
sleep 40
for i in $(seq 1 20); do
    "${SSH[@]}" "$HOST" 'echo up' 2>/dev/null && break
    sleep 10
done

say "verify"
"${SSH[@]}" "$HOST" "ls -la $PORT; systemctl is-active casinonet-sas; journalctl -u casinonet-sas --no-pager | tail -3"

say "DONE — wire the level shifter to the BB2E SAS port, set address $ADDRESS"
echo "first contact: sudo systemctl stop casinonet-sas, then"
echo "  cd ~/CasinoNet/SAS && ~/venvs/casinonet/bin/python tools/sas_bench_poll.py $PORT --credits"
