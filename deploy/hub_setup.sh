#!/usr/bin/env bash
# hub_setup.sh — stand up a CabiNet hub on this box, in one command.
#
# Run ON the hub-to-be, from inside your CabiNet checkout:
#   sudo ./deploy/hub_setup.sh                 # asks which port faces the slots
#   sudo ./deploy/hub_setup.sh --nic enp3s0    # or tell it, for scripted runs
#
# What it does (idempotent — re-run it any time; it is also the repair tool):
#   1. retires an old hand-installed hub (casinonet-* era services) if one is
#      running, and carries its floor data forward
#   2. picks the SLOT-side Ethernet port (the one wired to the machines'
#      switch) — shown with live link state, and it warns you off the port
#      that is currently your house LAN
#   3. gives that port the static hub address, 192.168.50.2/24 (NetworkManager,
#      systemd-networkd, dhcpcd, or ifupdown — whichever this box actually runs)
#   4. installs the five hub services — the G2S host runs as YOU; the four
#      network bootstrap services (DHCP/DNS/NTP/TFTP) run as root because
#      they bind privileged ports — then enables and starts them
#   5. waits for the WHOLE hub to answer, and prints where to point your browser
#
# It does NOT reconfigure your other network ports (Wi-Fi / house LAN stay
# yours — though when it has to ASK which port faces the slots, it briefly
# raises wired links to read their cable state; --nic skips even that) and
# does not create accounts. The hub itself never reaches the internet;
# the ONLY thing this script may download is git (once, if it is missing —
# updates need it; the hub runs fine without).
#
# Everything here can also be done by hand — see "Host install" in
# deploy/DEPLOY.md. This script exists so nobody has to.

set -euo pipefail

say()  { printf '%s\n' "$*"; }
fail() { printf 'hub_setup: %s\n' "$*" >&2; exit 1; }

# args first, so --help works from ANY login, root shell, or half-checkout
NIC="" ; ASSUME_YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nic) [[ $# -ge 2 ]] || fail "--nic needs a port name (see --help)"
               NIC="$2"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,/^$/ s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) fail "unknown option $1 (see --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || fail "run me with sudo (network config + systemd need root)"

# ── whose hub is this? ──────────────────────────────────────────────────────
# The shipped units carry a PLACEHOLDER account (`owner`). Same recipe as
# kiosk_setup.sh / companion_setup.sh / smib_setup.sh: the human who
# typed sudo is the operator, overridable with CABINET_USER=<login>.
# ($HOME is /root under sudo — useless here; getent gives the real home.)
RUSER="${CABINET_USER:-${SUDO_USER:-$(id -un)}}"
if ! RHOME="$(getent passwd "$RUSER" | cut -d: -f6)"; then RHOME=""; fi
[[ -n "$RHOME" ]] || fail "no such account '$RUSER'. Set CABINET_USER=<login> and re-run."
[[ "$RUSER" != "root" ]] || fail "refusing to run the hub as root. sudo from your normal login, or set CABINET_USER=<login>."
# the account's real primary group — NOT assumed to share the login's name
RGROUP="$(id -gn "$RUSER")"

# The repo is wherever THIS script lives (so it works no matter where the
# checkout sits), with a sanity check that it really is a CabiNet tree.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
[[ -d "$REPO/G2S" && -d "$REPO/SAS" ]] || fail "$REPO does not look like a CabiNet checkout (no G2S/ + SAS/)"
case "$REPO$RHOME" in
    *'|'*|*'&'*|*'\'*) fail "checkout/home path contains |, & or \\ — move the checkout to a plainer path" ;;
esac

HUB_IP="192.168.50.2"
UNITS=(cabinet-g2s cabinet-dhcp cabinet-dns cabinet-ntp cabinet-tftp)
OLD_UNITS=(casinonet-g2s casinonet-dhcp casinonet-dns casinonet-ntp casinonet-tftp)
UNIT_DST="/etc/systemd/system"

say "==> CabiNet hub setup"
say "    operator : $RUSER  (home $RHOME, group $RGROUP)"
say "    checkout : $REPO"

# ── prerequisites ───────────────────────────────────────────────────────────
command -v python3 >/dev/null || fail "python3 is required. Install it (e.g. sudo apt-get install -y python3) and re-run."
if ! command -v git >/dev/null; then
    say "    git is not installed — updates need it. Trying to install…"
    if command -v apt-get >/dev/null && apt-get install -y git >/dev/null 2>&1; then
        say "    installed git"
    else
        say "    ⚠️  could not install git. The hub will RUN fine, but"
        say "       deploy/update.py needs git — install it before updating."
    fi
fi
# Every unit about to be installed must EXIST before anything is retired.
# A checkout that predates the rename carries only casinonet-* sources, and
# discovering that AFTER stopping the old hub leaves the floor dark
# (live-hit on the dev floor, 2026-07-28).
for u in "${UNITS[@]}"; do
    [[ -f "$REPO/deploy/$u.service" ]] || fail \
        "$REPO/deploy/$u.service is missing — this checkout predates the current release. git -C $REPO pull first, then re-run me."
done

# ── pick the slot-side port ─────────────────────────────────────────────────
# Wired ports only. Wi-Fi is the operator's management path, never the slot
# network (retired 2026-07-15) — so wl* is excluded outright, as are lo,
# containers and bridges.
mapfile -t WIRED < <(ip -brief link show 2>/dev/null \
    | awk '{print $1, $2}' \
    | grep -Ev '^(lo|wl|docker|veth|br-|virbr|tun|tap)' \
    | awk '{print $1}' | cut -d@ -f1)
[[ ${#WIRED[@]} -gt 0 ]] || fail "no wired Ethernet ports found. The slot network needs a wired port."

carrier() {  # UP with link beats DOWN; live link is almost always "the one"
    local c; c="$(cat "/sys/class/net/$1/carrier" 2>/dev/null || echo 0)"
    [[ "$c" == "1" ]] && echo "link UP (cable connected)" || echo "no link"
}
is_house() {  # carries this box's default route = it IS the house LAN uplink
    ip route show default dev "$1" 2>/dev/null | grep -q .
}
house_tag() { is_house "$1" && echo "  ← your house LAN (default route)" || true; }

if [[ -z "$NIC" ]]; then
    # an administratively-DOWN port always reads "no carrier" — briefly raise
    # the wired links so the picker tells the truth about which one has a
    # cable in it (only when we have to ask; an explicit --nic skips this)
    for n in "${WIRED[@]}"; do ip link set dev "$n" up 2>/dev/null || true; done
    sleep 1
    if [[ ${#WIRED[@]} -eq 1 ]]; then
        NIC="${WIRED[0]}"
        say "    slot port: $NIC — the only wired port ($(carrier "$NIC"))"
    else
        say ""
        say "    Which port is wired to the SLOT switch (the machines' own switch)?"
        i=1
        for n in "${WIRED[@]}"; do
            say "      $i) $n   $(carrier "$n")$(house_tag "$n")"
            i=$((i+1))
        done
        if [[ $ASSUME_YES -eq 1 ]]; then
            fail "multiple wired ports and no --nic given — pick one: ${WIRED[*]}"
        fi
        read -r -p "    number: " pick \
            || fail "no input (non-interactive run?) — use --nic <port>"
        [[ "$pick" =~ ^[0-9]+$ ]] && [[ "$pick" -ge 1 && "$pick" -le ${#WIRED[@]} ]] \
            || fail "not a listed number"
        NIC="${WIRED[$((pick-1))]}"
    fi
else
    [[ -e "/sys/class/net/$NIC" ]] || fail "no such port: $NIC (have: ${WIRED[*]})"
    # the slot network is WIRED-only by design (Wi-Fi is the management path)
    printf '%s\n' "${WIRED[@]}" | grep -qx "$NIC" \
        || fail "$NIC is not a wired Ethernet port (have: ${WIRED[*]})"
fi

# Taking over the house-LAN port would drop this box's internet AND start a
# DHCP server on the home network — the one mistake this audience can't debug.
if is_house "$NIC"; then
    say ""
    say "    ⚠️  $NIC currently carries this box's default route — it looks like"
    say "       your house LAN / internet uplink. Making it the slot port takes"
    say "       this box off that network and runs a DHCP server on that wire."
    if [[ $ASSUME_YES -eq 1 ]]; then
        fail "refusing to take over the default-route port non-interactively — wire a dedicated slot port, or run without --yes to confirm"
    fi
    read -r -p "    really use $NIC as the SLOT port? [y/N] " ok \
        || fail "no input — refusing to take over the default-route port"
    [[ "$ok" == "y" || "$ok" == "Y" ]] || fail "stopped — nothing was changed on $NIC"
fi

# ── static IP on the slot port ──────────────────────────────────────────────
# Four managers cover every box we have seen; touch ONLY the chosen port.
# NETWORK_STATE: ok = address is up now; waiting = persistent config written,
# comes up when the cable is in; manual = no manager found, user finishes it.
NETWORK_STATE="manual"

ours_already() {  # the address is present AND one of OUR artifacts owns it
    ip -4 addr show dev "$NIC" 2>/dev/null | grep -q "inet $HUB_IP/" || return 1
    nmcli -t -g NAME con show 2>/dev/null | grep -qx "cabinet-slot" && return 0
    [[ -f /etc/systemd/network/10-cabinet-slot.network ]] && return 0
    [[ -f /etc/network/interfaces.d/cabinet-slot ]] && return 0
    grep -qs '^# CabiNet slot network (hub_setup.sh)$' /etc/dhcpcd.conf && return 0
    return 1
}
nm_manages() {
    systemctl is-active --quiet NetworkManager 2>/dev/null || return 1
    nmcli -t -f DEVICE,STATE device status 2>/dev/null \
        | grep -q "^$NIC:" || return 1
    ! nmcli -t -f DEVICE,STATE device status 2>/dev/null \
        | grep -q "^$NIC:unmanaged"
}

if ours_already; then
    say "    address  : $NIC already has $HUB_IP from an earlier run — leaving it"
    NETWORK_STATE="ok"
elif nm_manages; then
    say "    address  : $HUB_IP/24 on $NIC via NetworkManager (connection 'cabinet-slot')"
    nmcli -t con delete cabinet-slot >/dev/null 2>&1 || true
    nmcli con add type ethernet ifname "$NIC" con-name cabinet-slot \
        connection.autoconnect yes \
        ipv4.method manual ipv4.addresses "$HUB_IP/24" ipv6.method disabled >/dev/null
    nmcli con up cabinet-slot >/dev/null 2>&1 \
        || say "    (no link on $NIC yet — the address comes up when the cable is plugged; autoconnect is on)"
    NETWORK_STATE="waiting"
elif systemctl is-active --quiet systemd-networkd 2>/dev/null; then
    say "    address  : $HUB_IP/24 on $NIC via systemd-networkd"
    # 10-, not 50-: networkd takes the FIRST matching file, and netplan-style
    # runtime files start at 10 — a higher number can silently lose the port
    rm -f /etc/systemd/network/50-cabinet-slot.network
    cat > "/etc/systemd/network/10-cabinet-slot.network" <<EOF
# CabiNet slot network — written by hub_setup.sh (re-run it to regenerate)
[Match]
Name=$NIC
[Network]
Address=$HUB_IP/24
LinkLocalAddressing=no
IPv6AcceptRA=no
EOF
    networkctl reload 2>/dev/null || systemctl restart systemd-networkd
    NETWORK_STATE="waiting"
elif systemctl is-active --quiet dhcpcd 2>/dev/null && [[ -f /etc/dhcpcd.conf ]]; then
    say "    address  : $HUB_IP/24 on $NIC via dhcpcd.conf"
    # idempotent: replace our own stanza if present, else append — and make
    # sure the file ends in a newline first, or the start marker glues onto
    # the last line and the next re-run's range-delete misses it
    sed -i '/^# CabiNet slot network (hub_setup.sh)$/,/^# end CabiNet$/d' /etc/dhcpcd.conf
    [[ -s /etc/dhcpcd.conf && -n "$(tail -c1 /etc/dhcpcd.conf)" ]] && echo >> /etc/dhcpcd.conf
    cat >> /etc/dhcpcd.conf <<EOF
# CabiNet slot network (hub_setup.sh)
interface $NIC
static ip_address=$HUB_IP/24
# end CabiNet
EOF
    systemctl restart dhcpcd 2>/dev/null || true
    NETWORK_STATE="waiting"
elif [[ -f /etc/network/interfaces ]] && command -v ifup >/dev/null; then
    say "    address  : $HUB_IP/24 on $NIC via ifupdown (/etc/network/interfaces.d/cabinet-slot)"
    mkdir -p /etc/network/interfaces.d
    # only forms that would actually pick up an extensionless file count — a
    # legacy 'source /etc/network/interfaces.d/*.cfg' glob would NOT source
    # cabinet-slot, so it must not satisfy this check
    grep -qsE '^[[:space:]]*source-directory[[:space:]]+/etc/network/interfaces\.d[[:space:]]*$|^[[:space:]]*source[[:space:]]+/etc/network/interfaces\.d/(\*|cabinet-slot)[[:space:]]*$' /etc/network/interfaces \
        || printf '\nsource /etc/network/interfaces.d/cabinet-slot\n' >> /etc/network/interfaces
    cat > /etc/network/interfaces.d/cabinet-slot <<EOF
# CabiNet slot network — written by hub_setup.sh (re-run it to regenerate)
auto $NIC
iface $NIC inet static
    address $HUB_IP/24
EOF
    ifdown "$NIC" >/dev/null 2>&1 || true
    ifup "$NIC" >/dev/null 2>&1 \
        || say "    (could not bring $NIC up yet — it will come up on boot / when cabled)"
    NETWORK_STATE="waiting"
else
    say "    ⚠️  no NetworkManager / systemd-networkd / dhcpcd / ifupdown found."
    say "       Give $NIC the address $HUB_IP/24 with your network tool of"
    say "       choice — PERSISTENTLY, so it survives a reboot — then re-run"
    say "       this script to verify. Carrying on with the service install."
fi

# whatever the branch said, believe only the wire: is the address actually up?
# (the manual path checks too — its own instructions say "re-run to verify")
tries=5; [[ "$NETWORK_STATE" == "manual" ]] && tries=1
for _ in $(seq 1 "$tries"); do
    if ip -4 addr show dev "$NIC" 2>/dev/null | grep -q "inet $HUB_IP/"; then
        if [[ "$NETWORK_STATE" == "manual" ]]; then
            NETWORK_STATE="manual_ok"
        else
            NETWORK_STATE="ok"
        fi
        break
    fi
    sleep 1
done

# ── retire an old hand-installed hub (casinonet-* era) ──────────────────────
# The pre-rename tree installed these same five services under casinonet-*
# names. They hold the very ports the new units need (67/53/123/69/8081), so
# they must go first — and the old floor's money state comes along.
OLDREPO=""
found_old=0
for u in "${OLD_UNITS[@]}"; do
    if systemctl list-unit-files --no-legend "$u.service" 2>/dev/null | grep -q .; then
        found_old=1
        if [[ "$u" == "casinonet-g2s" ]]; then
            wd="$(systemctl show -p WorkingDirectory --value "$u" 2>/dev/null || true)"
            [[ -n "$wd" ]] && OLDREPO="$(dirname "$wd")"    # WorkingDirectory=<repo>/G2S
        fi
    fi
done
if [[ $found_old -eq 1 ]]; then
    say "==> retiring the old casinonet-* hub (same services, older names)"
    for u in "${OLD_UNITS[@]}"; do
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        say "    stopped + disabled $u"
    done
fi
if [[ -n "$OLDREPO" && -d "$OLDREPO/G2S/data" && "$OLDREPO" != "$REPO" ]]; then
    if [[ ! -e "$REPO/G2S/data/hub.db" ]]; then
        say "    carrying floor data forward from $OLDREPO/G2S/data"
        mkdir -p "$REPO/G2S/data"
        cp -a "$OLDREPO/G2S/data/." "$REPO/G2S/data/"
        if [[ -d "$OLDREPO/SAS/data" && ! -e "$REPO/SAS/data" ]]; then
            cp -a "$OLDREPO/SAS/data" "$REPO/SAS/"
        fi
        chown -R "$RUSER:$RGROUP" "$REPO/G2S/data" 2>/dev/null || true
        [[ -d "$REPO/SAS/data" ]] && chown -R "$RUSER:$RGROUP" "$REPO/SAS/data" 2>/dev/null || true
        say "    (the old tree at $OLDREPO was left untouched — remove it yourself once you're happy)"
    else
        say "    ⚠️  NOT copying old floor data: this checkout already has G2S/data/hub.db."
        say "       The old data still lives at $OLDREPO/G2S/data if you want it."
    fi
fi

# ── install the services ────────────────────────────────────────────────────
say "==> installing services (G2S host as $RUSER, slot port $NIC)"
TMPU="$(mktemp -d)"
trap 'rm -rf "$TMPU"' EXIT
for u in "${UNITS[@]}"; do
    src="$REPO/deploy/$u.service"
    [[ -f "$src" ]] || fail "$src is missing — incomplete checkout?"
    # Same substitution the other setup scripts do, plus the slot port. The
    # path swap must come FIRST (User=owner lines and /home/owner share text).
    sed -e "s|/home/owner/CabiNet|$REPO|g" \
        -e "s|/home/owner|$RHOME|g" \
        -e "s|^User=.*|User=$RUSER|" \
        -e "s|^Group=.*|Group=$RGROUP|" \
        -e "s|--interface eth0|--interface $NIC|g" \
        "$src" > "$TMPU/$u.service"
done
# Prove the substitutions landed BEFORE anything reaches /etc (217/USER lesson):
grep -qx "User=$RUSER"   "$TMPU/cabinet-g2s.service" \
    || fail "cabinet-g2s.service did not get User=$RUSER — refusing to install it"
grep -qx "Group=$RGROUP" "$TMPU/cabinet-g2s.service" \
    || fail "cabinet-g2s.service did not get Group=$RGROUP — refusing to install it"
for u in cabinet-dhcp cabinet-dns cabinet-ntp cabinet-tftp; do
    grep -q -- "--interface $NIC" "$TMPU/$u.service" \
        || fail "$u.service did not get --interface $NIC — refusing to install it"
done
if [[ "$RUSER" != "owner" ]]; then
    # the exact placeholder shapes, not bare "owner" — a login or checkout
    # path merely CONTAINING that word must not trip this (and a login that
    # IS 'owner' legitimately matches, so the check is skipped above)
    for u in "${UNITS[@]}"; do
        grep -qE "^User=owner$|^Group=owner$|/home/owner(/|$| )" "$TMPU/$u.service" \
            && fail "$u.service still contains a placeholder — refusing to install it"
    done
fi
for u in "${UNITS[@]}"; do
    install -m 644 "$TMPU/$u.service" "$UNIT_DST/$u.service"
    say "    $UNIT_DST/$u.service"
done

# Settings ▸ Updates runs `sudo systemctl restart cabinet-g2s` from inside the
# hub service — no terminal, so no password prompt is possible. Grant exactly
# those verbs on exactly that unit, nothing more.
SYSCTL="$(command -v systemctl)"
cat > /etc/sudoers.d/cabinet <<EOF
# CabiNet — written by hub_setup.sh so Settings ▸ Updates can restart the hub
$RUSER ALL=(root) NOPASSWD: $SYSCTL restart cabinet-g2s, $SYSCTL stop cabinet-g2s, $SYSCTL start cabinet-g2s
EOF
chmod 440 /etc/sudoers.d/cabinet
if ! visudo -cf /etc/sudoers.d/cabinet >/dev/null 2>&1; then
    rm -f /etc/sudoers.d/cabinet
    say "    ⚠️  sudoers drop-in failed validation — removed. The Settings ▸ Updates"
    say "       card will need passwordless sudo for 'systemctl restart cabinet-g2s'."
fi

# the hub writes its state under G2S/data — prove the operator can actually
# write there (mkdir -p alone is a no-op on an existing root-owned dir)
runuser -u "$RUSER" -- sh -c 'mkdir -p "$1" && touch "$1/.cabinet-wtest" && rm -f "$1/.cabinet-wtest"' sh "$REPO/G2S/data" \
    || fail "$RUSER cannot write $REPO/G2S/data — was something here run with sudo? Fix it: sudo chown -R $RUSER:$RGROUP $REPO"

systemctl daemon-reload
say "==> enabling + starting"
for u in "${UNITS[@]}"; do
    systemctl enable "$u" >/dev/null 2>&1 || fail "could not enable $u"
    systemctl restart "$u"      # restart, not start: re-runs pick up changes
done

# ── wait for the hub to answer ──────────────────────────────────────────────
# python3, not curl — curl is not on a minimal Debian and python3 already is.
say "==> waiting for the hub to answer…"
up=0
for _ in $(seq 1 30); do
    if python3 - <<'EOF' 2>/dev/null
import urllib.request, sys
r = urllib.request.urlopen("http://127.0.0.1:8081/api/status", timeout=2)
sys.exit(0 if r.status == 200 else 1)
EOF
    then up=1; break; fi
    sleep 2
done
if [[ $up -ne 1 ]]; then
    say ""
    say "⚠️  the hub did not answer within 60 s. Look at:"
    say "     systemctl status cabinet-g2s"
    say "     journalctl -u cabinet-g2s -n 50"
    exit 1
fi

# make sure it was OUR service that answered — not a leftover manual run or
# an old hub still speaking from another tree
mainpid="$(systemctl show -p MainPID --value cabinet-g2s 2>/dev/null || echo 0)"
portpid="$(ss -ltnp 2>/dev/null | grep -E ':8081[[:space:]]' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"
if [[ -n "$portpid" && "$mainpid" != "0" && "$portpid" != "$mainpid" ]]; then
    fail ":8081 is answered by PID $portpid, but cabinet-g2s is PID $mainpid — another G2S host is running on this box. Stop it (check 'ps -fp $portpid') and re-run."
fi

# ── prove the WHOLE hub is up, not just the web UI ──────────────────────────
# A floor page that answers while DHCP/DNS/NTP/TFTP are dead is the classic
# half-hub: it looks alive, but no machine ever gets a lease and the floor
# stays empty forever. Success here must mean all five services, not one.
bad=()
for u in "${UNITS[@]}"; do
    systemctl is-active --quiet "$u" || bad+=("$u")
done
if [[ ${#bad[@]} -gt 0 ]]; then
    say ""
    say "⚠️  the web UI answers, but these services are NOT running: ${bad[*]}"
    say "   The machines need every one of them. Look at:"
    for u in "${bad[@]}"; do say "     journalctl -u $u -n 30"; done
    say "   (a common cause: another DHCP/DNS server already holds the port)"
    exit 1
fi

say ""
if [[ "$NETWORK_STATE" == "ok" ]]; then
    say "✅ hub is up — all five services running, slot port live on $HUB_IP."
elif [[ "$NETWORK_STATE" == "manual_ok" ]]; then
    say "✅ hub is up — all five services running, slot port live on $HUB_IP."
    say "   (the address was set by hand — make sure your network config keeps"
    say "    it across a reboot; this script wrote nothing for it)"
elif [[ "$NETWORK_STATE" == "waiting" ]]; then
    say "✅ hub services are up — all five running."
    say "   $NIC is configured for $HUB_IP but has no link yet: plug it into the"
    say "   slot switch and the address comes up by itself. Re-run me to verify."
else
    say "✅ hub services are up — all five running."
    say "   ⚠️  UNFINISHED: $NIC still needs the address $HUB_IP/24, set"
    say "   persistently with your network tool of choice. Then re-run me."
fi
say ""
say "   Floor view : http://$HUB_IP:8081/          (any browser on this box or the slot network)"
say "   Wall board : http://$HUB_IP:8081/board     (point a TV at it)"
say ""
say "   Next:"
say "     · plug your machines into the slot switch — they get their network"
say "       from this hub automatically. Machine-side steps (G2S enable, host"
say "       URL, media glass): deploy/AVP_SETUP.md"
say "     · adding a SAS SMIB or RFID Companion Pi? Build its card per"
say "       deploy/SMIB_FRESH_IMAGE.md — it finds this hub on its own; assign"
say "       it to a machine from that machine's ⚙️ Options in the floor UI"
say "     · updating later is one command:  python3 deploy/update.py"
