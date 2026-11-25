#!/usr/bin/env bash
# hub_firstboot.sh — turn a freshly flashed hub card into a running hub.
# Runs unattended from cabinet-hub-firstboot.service; nobody types anything.
#
# The image carries a CabiNet checkout in the first account's home and this
# script. On first boot it waits for the account to exist (Raspberry Pi OS
# renames its placeholder "pi" account to whatever name the card was given,
# and the home folder moves with it), then runs the same installer a person
# would: deploy/hub_setup.sh on the wired port, and deploy/kiosk_setup.sh when
# a screen is plugged in.
#
# EXIT STATUS IS THE RETRY CONTRACT: nonzero until the hub is up, so the
# service tries again every 30 s (account not made yet, no wired port yet).
# Once it succeeds it leaves a marker and never runs again; hub_setup.sh
# remains the repair tool after that.
set -uo pipefail

ROOT="${CABINET_FIRSTBOOT_ROOT:-}"          # test seam, nothing else
MARK="$ROOT/var/lib/cabinet/hub-firstboot.done"
LOG="$ROOT/var/log/cabinet-hub-firstboot.log"
say() { printf 'cabinet-hub-firstboot: %s\n' "$*"; }

[[ -e "$MARK" ]] && exit 0

# Raspberry Pi OS keeps its system log in memory: after a power-off there is
# nothing left to say why a hub did not come up. Everything this script and
# the installers print is also kept in a plain file on the card, flushed at
# the end of every try, so the card itself can be read on another computer.
exec > >(tee -a "$LOG") 2>&1
trap 'sync' EXIT
say "---- try at $(date -u +%FT%TZ), up $(cut -d. -f1 /proc/uptime)s ----"

# ...and the person in front of the hub's screen sees a login prompt and
# nothing else while this works. Tell them, on that screen.
screen() { { printf '\r\n  CabiNet: %s\r\n' "$*" > /dev/tty1; } 2>/dev/null || true; }

# The operator is uid 1000 — the one account every card has, whatever it was
# named. Until first-boot setup has given it a real login it is the "pi"
# placeholder with no shell: not ready, try again.
ENT="$(getent passwd 1000 || true)"
RUSER="$(cut -d: -f1 <<<"$ENT")"; RHOME="$(cut -d: -f6 <<<"$ENT")"
RSHELL="$(cut -d: -f7 <<<"$ENT")"
if [[ -z "$RUSER" || "$RSHELL" == */nologin || "$RSHELL" == */false ]]; then
    say "the hub's account is not set up yet — waiting"; exit 1
fi
REPO="$RHOME/CabiNet"
if [[ ! -x "$REPO/deploy/hub_setup.sh" ]]; then
    say "no CabiNet folder in $RHOME yet — waiting"; exit 1
fi

# The slot-side port: the first real wired port (a Pi has exactly one).
NIC=""
for d in /sys/class/net/*; do
    n="$(basename "$d")"
    [[ -e "$d/device" && ! -d "$d/wireless" ]] || continue
    [[ "$(cat "$d/type" 2>/dev/null)" == "1" ]] || continue
    NIC="$n"; break
done
if [[ -z "$NIC" ]]; then
    say "no wired Ethernet port found — waiting"; exit 1
fi

say "setting up the hub for '$RUSER' on $NIC"
screen "setting up this hub. This takes a couple of minutes; the screen changes by itself when it is ready."
if ! CABINET_USER="$RUSER" "$REPO/deploy/hub_setup.sh" --nic "$NIC" --yes; then
    say "hub setup did not finish — will try again"
    screen "the hub is not up yet. Trying again in half a minute."
    exit 1
fi

# A screen plugged into the hub becomes the touch kiosk. No screen: skip it;
# `sudo ./deploy/kiosk_setup.sh` adds one later.
SCREEN=0
for s in /sys/class/drm/*/status; do
    [[ -r "$s" && "$(cat "$s")" == "connected" ]] && SCREEN=1
done
if [[ $SCREEN -eq 1 ]]; then
    say "a screen is connected — setting up the kiosk"
    CABINET_USER="$RUSER" REPO="$REPO" "$REPO/deploy/kiosk_setup.sh" \
        || say "kiosk setup did not finish (the hub itself is up)"
fi

mkdir -p "$(dirname "$MARK")" && date -u +%FT%TZ > "$MARK"
say "done — the hub is at http://$(hostname).local:8081"
