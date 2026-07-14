#!/usr/bin/env bash
# kiosk_setup.sh — turn the Pi's DSI touchscreen into the CasinoNet kiosk.
#
# Run ON the Pi, as root, from the repo checkout:
#   sudo /home/aj/CasinoNet/deploy/kiosk_setup.sh
#
# What it does (idempotent — safe to re-run after every unit-file tweak):
#   1. apt-get install cage        (Wayland kiosk compositor; chromium is
#                                   already on the Pi)
#   2. install casinonet-kiosk.service   -> tty1: cage + chromium --kiosk
#                                           http://127.0.0.1:8081/home?kiosk=1
#      install casinonet-console.service -> tty2: the curses debug cockpit
#                                           (it lived on tty1 before)
#   3. disable getty@tty1 (the kiosk owns VT1; Conflicts= also enforces it)
#   4. enable + (re)start both units
#
# Day-to-day: Ctrl+Alt+F2 on an attached keyboard (or `sudo chvt 2`) shows
# the debug console; Ctrl+Alt+F1 / `sudo chvt 1` returns to the kiosk.
#
# REVERT (back to the pre-kiosk layout, console on tty1):
#   sudo systemctl disable --now casinonet-kiosk.service
#   sudo systemctl enable getty@tty1.service
#   # then either keep the console on tty2, or restore the old tty1 unit:
#   #   edit /etc/systemd/system/casinonet-console.service back to tty1
#   #   (Conflicts=getty@tty1.service, TTYPath=/dev/tty1, and re-add
#   #    ExecStartPre=+/usr/bin/chvt 1), then:
#   sudo systemctl daemon-reload
#   sudo systemctl restart casinonet-console.service

set -euo pipefail

REPO="${REPO:-/home/aj/CasinoNet}"
UNIT_SRC="$REPO/deploy"
UNIT_DST="/etc/systemd/system"
UNITS=(casinonet-kiosk.service casinonet-console.service)

if [[ $EUID -ne 0 ]]; then
    echo "kiosk_setup: run me with sudo (systemd + apt need root)" >&2
    exit 1
fi
for u in "${UNITS[@]}"; do
    if [[ ! -f "$UNIT_SRC/$u" ]]; then
        echo "kiosk_setup: $UNIT_SRC/$u not found — is the repo at $REPO?" >&2
        exit 1
    fi
done

echo "==> installing cage (Wayland kiosk compositor)"
apt-get install -y cage

# chromium ships preinstalled; fail loudly with a hint if it ever isn't.
if ! command -v chromium >/dev/null 2>&1; then
    echo "kiosk_setup: /usr/bin/chromium not found." >&2
    echo "  Debian 13:        sudo apt-get install -y chromium" >&2
    echo "  Raspberry Pi OS:  binary may be 'chromium-browser' — install it and" >&2
    echo "  adjust ExecStart in deploy/casinonet-kiosk.service to match." >&2
    exit 1
fi

echo "==> installing systemd units"
for u in "${UNITS[@]}"; do
    install -m 644 "$UNIT_SRC/$u" "$UNIT_DST/$u"
    echo "    $UNIT_DST/$u"
done
systemctl daemon-reload

echo "==> freeing VT1 for the kiosk (getty@tty1 off)"
systemctl disable --now getty@tty1.service || true

echo "==> enabling + starting the kiosk and the tty2 console"
systemctl enable "${UNITS[@]}"
# restart (not start) so a re-run picks up edited unit files / moves the
# console off tty1 on the first run
systemctl restart casinonet-console.service
systemctl restart casinonet-kiosk.service

echo
echo "kiosk_setup: done."
echo "  kiosk   : tty1 — http://127.0.0.1:8081/home?kiosk=1 (needs casinonet-g2s up)"
echo "  console : tty2 — Ctrl+Alt+F2 / 'sudo chvt 2' (back with Ctrl+Alt+F1)"
echo "  status  : systemctl status casinonet-kiosk casinonet-console"
echo "  revert  : see the comment block at the top of this script"
