#!/usr/bin/env bash
# companion_setup.sh — turn a fresh Pi into a CasinoNet Companion: the RFID
# tap daemon for a cabinet that uses its OWN glass for the player UI (the IGT
# AVP) or to add tap-to-identify to any machine.
#
# RFID-ONLY tier — no SAS, no UART, no touchscreen. The Companion is
# stdlib-only (system python3 is enough, no venv). This installs Companion/ +
# casinonet-companion.service, enables I2C for the PN532, and points the daemon
# at the hub's WIRED slot-net IP by default. Idempotent — safe to re-run.
#
#   ZERO-CONFIG (v11) — no bindings needed; assign the machine in the hub UI:
#     deploy/companion_setup.sh aj@smib-avp [-i ~/.ssh/casinonet]
#   MANUAL OVERRIDE — bake a binding into the unit (co-located / advanced):
#     deploy/companion_setup.sh aj@smib-avp.local --g2s-egm IGT_<egmId> \
#       [--companion-id companion-avp] [--hub http://127.0.0.1:8081] \
#       [--hw-i2c] [--sas-smib smib-bb2 --sas-address 1]
#
# WIRED ONLY (the Wi-Fi satellite path is retired): the host serves DHCP +
# G2S on the slot segment (192.168.50.0/24), so plug the Zero's Ethernet/USB
# HAT into that switch and it comes up on .50.x, one local hop from the host
# at 192.168.50.2:8081. --hub overrides (co-located = 127.0.0.1).
#
# What it does on the Pi:
#   1. I2C for the PN532 — the UNIVERSAL software i2c-gpio bus on GPIO23/24
#      (physical 16/18) -> /dev/i2c-11. ONE wiring for the WHOLE FLEET: every
#      Companion, whether it also wears a SAS HAT (BB2) or is RFID-only (AVP),
#      wires the PN532 to the SAME pins (Companion/README.md). The software bus
#      works regardless of whether the hardware I2C pins (GPIO2/3) are occupied,
#      so there is no per-board wiring to remember. `--hw-i2c` opts a board into
#      the hardware bus (GPIO2/3 -> /dev/i2c-1) instead — rarely needed.
#      `i2c-dev` is ensured in /etc/modules so the node appears.
#   2. rsyncs Companion/ (stdlib-only; no venv, no SAS tree).
#   3. Installs + enables casinonet-companion.service with THIS cabinet's
#      bindings (--g2s-egm / --companion-id / --hub / optional --sas-*).
#   4. Reboots ONLY if config.txt changed (I2C overlay), then verifies the
#      I2C bus + the service.
#
# After it finishes: set the PN532 DIP switches to I2C (answers at 0x24), wire
# it per Companion/README.md, tap a fob, and watch the HUB journal
# (journalctl -u casinonet-g2s -f) for `💳 card IN`. A board that isn't wired
# yet is fine — the daemon reports readerOk=false and keeps retrying; the unit
# stays active.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="" ; SSHKEY="" ; HUB=""
G2S_EGM="" ; COMPANION_ID="" ; SAS_SMIB="" ; SAS_ADDRESS="" ; I2C_MODE="gpio"

# ZERO-CONFIG (v11): every binding flag is OPTIONAL. With none passed, the unit
# installs FLAGLESS and the daemon self-configures — id from the Pi serial, hub
# from the default gateway — and the collector binds the machine from the hub
# UI (the machine's ⚙️ Options). Pass flags only for a co-located/manual
# override; a bare `companion_setup.sh aj@host` is the normal path.
while [ $# -gt 0 ]; do
    case "$1" in
        -i) SSHKEY="$2"; shift 2 ;;
        --hub) HUB="$2"; shift 2 ;;
        --g2s-egm) G2S_EGM="$2"; shift 2 ;;
        --companion-id) COMPANION_ID="$2"; shift 2 ;;
        --sas-smib) SAS_SMIB="$2"; shift 2 ;;
        --sas-address) SAS_ADDRESS="$2"; shift 2 ;;
        --hw-i2c) I2C_MODE="hw"; shift ;;
        --i2c-gpio) shift ;;   # deprecated: the i2c-gpio bus is the default now (no-op, kept for back-compat)
        *) if [ -z "$HOST" ]; then HOST="$1"; shift; else
               echo "unknown arg: $1" >&2; exit 2; fi ;;
    esac
done
[ -n "$HOST" ] || { echo "usage: $0 aj@<pi-host> [-i key] [--hub url] [--g2s-egm egmId] [--companion-id id] [--hw-i2c] [--sas-smib id --sas-address n]" >&2; echo "  (all bindings optional — omit for zero-config: serial id + gateway hub, assign in the UI)" >&2; exit 2; }

# The bus node depends on the I2C layout: the UNIVERSAL software i2c-gpio bus
# (GPIO23/24 -> /dev/i2c-11, the default) vs the hardware bus (--hw-i2c).
if [ "$I2C_MODE" = "gpio" ]; then BUS="/dev/i2c-11"; else BUS="/dev/i2c-1"; fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)
[ -n "$SSHKEY" ] && SSH+=(-i "$SSHKEY")
RSYNC_SSH="${SSH[*]}"

say() { printf '\n=== %s\n' "$*"; }

say "checking SSH + passwordless sudo on $HOST"
"${SSH[@]}" "$HOST" 'sudo -n true' || {
    echo "need passwordless sudo on the Pi first (one-time, run there):" >&2
    echo "  echo \"\$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/010-\$USER-nopasswd" >&2
    exit 1; }

# The shipped unit carries placeholder user/home values — rewrite them for
# whoever this Pi actually runs as (applied at unit install below).
RUSER=$("${SSH[@]}" "$HOST" 'id -un')
RHOME=$("${SSH[@]}" "$HOST" 'echo "$HOME"')

say "I2C for the PN532 (mode=$I2C_MODE -> $BUS)"
# The remote block prints "i2c-changed" if it edited config.txt (=> reboot).
I2C_OUT=$("${SSH[@]}" "$HOST" "
set -e
MODEL=\$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
echo \"board: \$MODEL\" >&2
CFG=/boot/firmware/config.txt; [ -f \"\$CFG\" ] || CFG=/boot/config.txt
sudo cp \"\$CFG\" \"\$CFG.bak-companion\" 2>/dev/null || true
CHANGED=0
if [ '$I2C_MODE' = 'gpio' ]; then
  # SAS HAT owns the hardware I2C pins -> software bus on GPIO23/24 = /dev/i2c-11
  grep -q '^dtoverlay=i2c-gpio.*bus=11' \"\$CFG\" || { printf '\n# Companion PN532: universal software i2c-gpio bus on GPIO23/24 (matches the BB2)\ndtoverlay=i2c-gpio,i2c_gpio_sda=23,i2c_gpio_scl=24,bus=11,i2c_gpio_delay_us=2\n' | sudo tee -a \"\$CFG\" >/dev/null; CHANGED=1; }
else
  # RFID-only board -> hardware I2C on GPIO2/3 = /dev/i2c-1
  grep -q '^dtparam=i2c_arm=on' \"\$CFG\" || { printf '\n# Companion PN532: hardware I2C (GPIO2/3) -> /dev/i2c-1\ndtparam=i2c_arm=on\n' | sudo tee -a \"\$CFG\" >/dev/null; CHANGED=1; }
fi
grep -q '^i2c-dev' /etc/modules 2>/dev/null || { echo i2c-dev | sudo tee -a /etc/modules >/dev/null; CHANGED=1; }
sudo usermod -aG i2c \"\$USER\" 2>/dev/null || true
[ \"\$CHANGED\" = 1 ] && echo i2c-changed || echo i2c-nochange
")
echo "$I2C_OUT"

say "rsync Companion/ (stdlib-only, no venv)"
"${SSH[@]}" "$HOST" 'mkdir -p ~/CasinoNet/deploy'
rsync -a -e "$RSYNC_SSH" --exclude '__pycache__' --exclude '.pytest_cache' \
      "$REPO/Companion/" "$HOST:CasinoNet/Companion/"
# support_bundle.py rides along so "grab a bundle on the satellite" works
rsync -a -e "$RSYNC_SSH" "$REPO/deploy/support_bundle.py" "$HOST:CasinoNet/deploy/"

say "import check (proves stdlib deps resolve on the Pi)"
"${SSH[@]}" "$HOST" 'cd ~/CasinoNet/Companion && python3 -c "import companion_host, reader; print(\"companion import OK\")"'

# Build the ExecStart from ONLY the flags explicitly passed — flagless by
# default (zero-config: the daemon derives id from the Pi serial + hub from the
# gateway, and the binding is assigned in the UI). --bus is added ONLY for the
# non-default hardware bus; the universal gpio bus is the daemon's own default.
EXEC="/usr/bin/python3 -u $RHOME/CasinoNet/Companion/companion_host.py"
[ -n "$HUB" ]          && EXEC="$EXEC --hub $HUB"
[ -n "$COMPANION_ID" ] && EXEC="$EXEC --companion-id $COMPANION_ID"
[ -n "$G2S_EGM" ]      && EXEC="$EXEC --g2s-egm \"$G2S_EGM\""
[ -n "$SAS_SMIB" ]     && EXEC="$EXEC --sas-smib $SAS_SMIB"
[ -n "$SAS_ADDRESS" ]  && EXEC="$EXEC --sas-address $SAS_ADDRESS"
[ "$I2C_MODE" = "hw" ] && EXEC="$EXEC --bus $BUS"

say "install casinonet-companion.service"
echo "  ExecStart=$EXEC"
# Swap the whole ExecStart= line for our generated one, and rewrite the
# placeholder user/home for the actual remote account.
awk -v exec="ExecStart=$EXEC" '/^ExecStart=/{print exec; next} {print}' \
    "$REPO/Companion/casinonet-companion.service" | \
    sed -e "s|^User=.*|User=$RUSER|" -e "s|^Group=.*|Group=$RUSER|" \
        -e "s|/home/aj|$RHOME|g" | \
    "${SSH[@]}" "$HOST" 'sudo tee /etc/systemd/system/casinonet-companion.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable casinonet-companion >/dev/null 2>&1
echo "service installed + enabled"'

if echo "$I2C_OUT" | grep -q i2c-changed; then
    say "rebooting the Pi (config.txt I2C change)"
    "${SSH[@]}" "$HOST" 'sudo reboot' || true
    sleep 40
    for i in $(seq 1 20); do
        "${SSH[@]}" "$HOST" 'echo up' 2>/dev/null && break
        sleep 10
    done
else
    say "no config.txt change — starting the service (no reboot)"
    "${SSH[@]}" "$HOST" 'sudo systemctl restart casinonet-companion || true'
fi

say "verify"
"${SSH[@]}" "$HOST" "ls -la $BUS 2>/dev/null || echo 'NOTE: $BUS not present yet — check wiring/DIP after the reader is plugged in'; systemctl is-active casinonet-companion; journalctl -u casinonet-companion --no-pager | tail -4"

say "DONE — set the PN532 DIPs to I2C (0x24), wire per Companion/README.md, tap a fob"
echo "watch the HUB: journalctl -u casinonet-g2s -f   (expect: 💳 card IN)"
