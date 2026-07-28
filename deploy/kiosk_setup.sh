#!/usr/bin/env bash
# kiosk_setup.sh — turn the Pi's DSI touchscreen into the CabiNet kiosk.
#
# Run ON the Pi, as root, from the repo checkout:
#   sudo ./deploy/kiosk_setup.sh          # from inside your CabiNet checkout
#
# It installs the units as YOUR login (sudo's $SUDO_USER), not as the
# `owner` placeholder they ship with. Override with CABINET_USER=<login>.
#
# What it does (idempotent — safe to re-run after every unit-file tweak):
#   1. apt-get install cage        (Wayland kiosk compositor; chromium is
#                                   already on the Pi)
#   2. install cabinet-kiosk.service   -> tty1: cage + chromium --kiosk
#                                           http://127.0.0.1:8081/home?kiosk=1
#      install cabinet-console.service -> tty2: the curses debug cockpit
#                                           (it lived on tty1 before)
#   3. disable getty@tty1 (the kiosk owns VT1; Conflicts= also enforces it)
#   4. enable + (re)start both units
#
# Day-to-day: Ctrl+Alt+F2 on an attached keyboard (or `sudo chvt 2`) shows
# the debug console; Ctrl+Alt+F1 / `sudo chvt 1` returns to the kiosk.
#
# REVERT (back to the pre-kiosk layout, console on tty1):
#   sudo systemctl disable --now cabinet-kiosk.service
#   sudo systemctl enable getty@tty1.service
#   # then either keep the console on tty2, or restore the old tty1 unit:
#   #   edit /etc/systemd/system/cabinet-console.service back to tty1
#   #   (Conflicts=getty@tty1.service, TTYPath=/dev/tty1, and re-add
#   #    ExecStartPre=+/usr/bin/chvt 1), then:
#   sudo systemctl daemon-reload
#   sudo systemctl restart cabinet-console.service

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "kiosk_setup: run me with sudo (systemd + apt need root)" >&2
    exit 1
fi

# WHOSE Pi is this? The shipped units carry a PLACEHOLDER account (`owner`) and
# something has to replace it with the real one — companion_setup.sh and
# zero2w_sas_setup.sh have always done this rewrite; this script never did, so
# both units landed verbatim and systemd answered `status=217/USER — Failed to
# determine user credentials`, restart-looping forever on every Pi whose login
# was not literally "owner". Nobody's login is "owner".
#
# `$HOME` is NO HELP here: we are under sudo, so it is /root. $SUDO_USER is the
# human who typed the command, which is exactly who the kiosk should run as.
RUSER="${CABINET_USER:-${SUDO_USER:-$(id -un)}}"
# `if !`, not a bare assignment: getent exits 2 for an unknown account, and
# under `set -euo pipefail` that kills the script THERE — silently, with the
# helpful message below never reaching the operator.
if ! RHOME="$(getent passwd "$RUSER" | cut -d: -f6)"; then
    RHOME=""
fi
if [[ -z "$RHOME" ]]; then
    echo "kiosk_setup: no such account '$RUSER'. Set CABINET_USER=<login> and" >&2
    echo "  re-run, e.g.  sudo CABINET_USER=pi $0" >&2
    exit 1
fi
if [[ "$RUSER" == "root" ]]; then
    echo "kiosk_setup: refusing to run the kiosk as root. Run it with sudo from" >&2
    echo "  your normal login, or set CABINET_USER=<login>." >&2
    exit 1
fi

REPO="${REPO:-$RHOME/CabiNet}"
UNIT_SRC="$REPO/deploy"
UNIT_DST="/etc/systemd/system"
UNITS=(cabinet-kiosk.service cabinet-console.service)
echo "==> kiosk will run as $RUSER (home $RHOME, repo $REPO)"
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
    echo "  adjust ExecStart in deploy/cabinet-kiosk.service to match." >&2
    exit 1
fi

echo "==> installing systemd units (as $RUSER)"
for u in "${UNITS[@]}"; do
    # Same substitution the companion and satellite setups do: the placeholder
    # user/home in the shipped unit become this box's real ones.
    sed -e "s|^User=.*|User=$RUSER|" \
        -e "s|^Group=.*|Group=$RUSER|" \
        -e "s|/home/owner|$RHOME|g" \
        "$UNIT_SRC/$u" > "$UNIT_DST/$u"
    chmod 644 "$UNIT_DST/$u"
    echo "    $UNIT_DST/$u  (User=$RUSER)"
done
systemctl daemon-reload

# Prove it before enabling anything: a unit that cannot resolve its user starts
# and dies in a loop, and the failure is only visible in journalctl.
for u in "${UNITS[@]}"; do
    want="User=$RUSER"
    if ! grep -qx "$want" "$UNIT_DST/$u"; then
        echo "kiosk_setup: $UNIT_DST/$u did not get '$want' — refusing to" >&2
        echo "  enable a unit that will fail with 217/USER." >&2
        exit 1
    fi
done

echo "==> freeing VT1 for the kiosk (getty@tty1 off)"
systemctl disable --now getty@tty1.service || true

echo "==> enabling + starting the kiosk and the tty2 console"
systemctl enable "${UNITS[@]}"
# restart (not start) so a re-run picks up edited unit files / moves the
# console off tty1 on the first run
systemctl restart cabinet-console.service
systemctl restart cabinet-kiosk.service

echo
echo "kiosk_setup: done."
echo "  kiosk   : tty1 — http://127.0.0.1:8081/home?kiosk=1 (needs cabinet-g2s up)"
echo "  console : tty2 — Ctrl+Alt+F2 / 'sudo chvt 2' (back with Ctrl+Alt+F1)"
echo "  status  : systemctl status cabinet-kiosk cabinet-console"
echo "  revert  : see the comment block at the top of this script"
