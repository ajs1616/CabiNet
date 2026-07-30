#!/usr/bin/env bash
# firstboot.sh — adopt the account this card was actually flashed under, then
# enroll the hub's update key. Runs unattended from cabinet-firstboot.service;
# nobody types anything on the Pi, ever.
#
# TWO PROBLEMS, ONE ONESHOT, AND THE ORDER IS THE POINT.
#
#   1. The baked units are keyed to the BAKE username. Companion/
#      cabinet-companion.service and deploy/cabinet-sas.service ship
#      User=owner / Group=owner / /home/owner as PLACEHOLDERS —
#      companion_setup.sh:193-194 and smib_setup.sh:228-229 rewrite them at
#      install time from the live box (RUSER/RHOME). Raspberry Pi Imager
#      renames the baked account and moves its home dir, but it does not
#      rewrite third-party systemd units: a card flashed under any other
#      username boots with cabinet-companion pointing at a home that no longer
#      exists. It never starts, the Pi never announces, and everything
#      downstream has no input. This is the SAME substitution, run locally.
#   2. Nothing authorizes the hub's ssh key on a fresh card, and both setup
#      scripts connect with BatchMode=yes (key-only), so the hub cannot log in
#      to run ANY role script. The block that fixes it already exists and is
#      proven — it just runs in the wrong place, over ssh, from the hub
#      (companion_setup.sh:212-234, smib_setup.sh:179-195). It needs a home
#      dir to append into, which is why it runs SECOND.
#
# IDEMPOTENT THROUGHOUT: a unit is written only when its content actually
# changes, so a re-run never restarts a daemon that was already correct (the
# discipline that stopped smib_setup.sh rebooting a live cabinet for nothing),
# and the key is appended only when grep -qF says it is absent.
#
# EXIT STATUS IS THE RETRY CONTRACT: nonzero until the hub key is in place, so
# cabinet-firstboot.service's Restart=on-failure retries every 60s while the
# hub is still booting (or still unplugged). The .timer supplies the hourly
# re-assert afterwards. Stdlib/shell only — no daemon, no listener, no cloud.
#
# It CANNOT live in the home dir it exists to resolve: the bake installs it at
# /usr/local/sbin/cabinet-firstboot.sh, which is where the unit's ExecStart
# points. The copies of the two service files under ~/CabiNet stay
# placeholders on purpose — the setup scripts read those as templates.
#
# CABINET_FIRSTBOOT_ROOT is a test seam and nothing else: a directory holding
# COPIES of etc/passwd + etc/systemd/system, rewritten offline by the gate.
# Never set on a Pi. When it is set there is no such account and no hub, so
# systemd and the enroll half are skipped.

set -euo pipefail

ROOT="${CABINET_FIRSTBOOT_ROOT:-}"
UNIT_DIR="$ROOT/etc/systemd/system"
PASSWD_FILE="$ROOT/etc/passwd"
# The two units the image bakes with placeholder user/home values. The hub's
# own cabinet-kiosk is not on this list — it never ships on a card.
UNITS="cabinet-companion.service cabinet-sas.service"
CHANGED=""

# The collector's account: the LOWEST uid>=1000 whose home dir exists. The
# name cannot be known at bake time (Imager renames it) and the uid is the
# only stable handle; the home-dir test is what rejects the renamed-away bake
# account, whose passwd entry can outlive its directory. 65534 = nobody.
RUSER="" ; RHOME=""
while IFS=: read -r uid name home; do
    if [ -z "$RUSER" ] && [ -d "$ROOT$home" ]; then
        RUSER="$name" ; RHOME="$home"
    fi
done < <(awk -F: '$3>=1000 && $3<65534 {print $3":"$1":"$6}' "$PASSWD_FILE" \
         | sort -t: -k1,1n)

[ -n "$RUSER" ] || {
    echo "no account to adopt yet (no uid>=1000 with a home dir) — the first" >&2
    echo "boot may still be creating it; will retry" >&2
    exit 1; }
echo "adopting $RUSER ($RHOME)"

# The substitution companion_setup.sh:193-194 and smib_setup.sh:228-229 run
# over ssh at install time, run here against the INSTALLED unit and IN PLACE —
# so a generated ExecStart (smib_setup.sh's --port/--address/--hub/--smib-id,
# companion_setup.sh's binding flags) survives untouched. cabinet-sas's
# ExecStart also carries /home/owner/venvs/cabinet/bin/python, so the venv
# path follows the home dir on the same global replace.
rewrite_unit() {
    local unit="$1" path="$UNIT_DIR/$1" new
    [ -f "$path" ] || return 0
    # ⚖️ ONLY EVER FIX THE SHIPPED TEMPLATE. The `/home/owner` substitution is
    # literal, but `s|^User=.*|` is not — so on a unit a role script ALREADY
    # installed for a real account, the User= half would fire while the path
    # half did not, leaving the unit naming one account and pointing at
    # another's tree. Worse, that counts as CHANGED, so we would then restart
    # it — taking a WORKING SAS leg off a live floor, recoverable only with a
    # shell, which is the one thing this whole design forbids. A unit with no
    # `/home/owner` and no `User=owner` left in it is somebody's finished
    # install: not ours to touch.
    grep -qE '^(User|Group)=owner|/home/owner' "$path" || {
        echo "  $unit is already installed for a real account — left alone"
        return 0
    }
    new=$(sed -e "s|^User=.*|User=$RUSER|" -e "s|^Group=.*|Group=$RUSER|" \
              -e "s|/home/owner|$RHOME|g" "$path")
    if [ "$new" = "$(cat "$path")" ]; then
        echo "  $unit already runs as $RUSER"
        return 0
    fi
    # Temp + mv: a card yanked out of the wall mid-write must never be left
    # holding a truncated unit file.
    printf '%s\n' "$new" > "$path.cabinet-new"
    chmod --reference="$path" "$path.cabinet-new" 2>/dev/null || \
        chmod 644 "$path.cabinet-new"
    mv -f "$path.cabinet-new" "$path"
    CHANGED="$CHANGED $unit"
    echo "  $unit -> User=$RUSER, home=$RHOME"
}

for u in $UNITS; do rewrite_unit "$u"; done

if [ -n "$ROOT" ]; then
    echo "(offline rewrite only — no systemd, no enroll)"
    exit 0
fi

if [ -n "$CHANGED" ]; then
    systemctl daemon-reload
    for u in $CHANGED; do
        # Only units the image (or a role install) already enabled: a card with
        # no SAS role must not be handed a SAS daemon here. A unit we just
        # rewrote was pointing at a home that does not exist, so it cannot have
        # been serving a live machine — restarting it takes nothing off a floor.
        systemctl is-enabled --quiet "$u" 2>/dev/null || continue
        # Clear the start-limit FIRST. A unit whose WorkingDirectory did not
        # exist has been failing on Restart=always since boot and will have
        # spent StartLimitBurst long before we got here — systemd then refuses
        # the start outright ("start request repeated too quickly"), we would
        # log one line and move on, and no later run would retry because the
        # unit is already correct by then (CHANGED empty). The reader would
        # stay dead until someone power-cycled the Pi.
        systemctl reset-failed "$u" 2>/dev/null || true
        systemctl restart "$u" || echo "  $u did not start" >&2
    done
fi

# companion_setup.sh:91 (smib_setup.sh:91 too) hard-requires `sudo -n true` as
# this account before it will do anything, and the hub has no way to grant it
# later — so a card without it can never be given a role from the page. This is
# that exact gate, run as that exact account rather than a sudoers-file guess.
# Loud, and NOT fatal: the enroll half still has to run, and the fix is a new
# card, not a retry. The sudoers one-liner those scripts print on failure is
# deliberately not repeated here; it must never reach a gameroom screen.
if ! runuser -u "$RUSER" -- sudo -n true 2>/dev/null; then
    echo "WARNING: '$RUSER' cannot use sudo without a password — the hub will" >&2
    echo "not be able to set this Pi up until the card is rebuilt with it" >&2
fi

# Lifted from companion_setup.sh:212-234 / smib_setup.sh:179-195, which run it
# over ssh FROM the hub — the one thing a fresh card cannot wait for, because
# the hub has no way in yet. Same gateway (the hub IS the gateway on the wired
# slot segment), same endpoint, same grep -qF before appending, same 700/600.
# Running as root it must also chown what it creates: sshd ignores an
# authorized_keys the account does not own.
GW=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}' || true)
[ -n "$GW" ] || { echo "no default gateway yet — no hub to ask; will retry" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || {
    echo "this card has no curl — it cannot fetch the hub key" >&2; exit 1; }
KEY=$(curl -fsS --max-time 8 "http://$GW:8081/api/hubkey" 2>/dev/null \
      | sed -n 's/.*"publicKey": *"\([^"]*\)".*/\1/p' || true)
case "$KEY" in
    ssh-*) : ;;
    *) echo "the hub at $GW has not published its key yet; will retry" >&2; exit 1 ;;
esac

mkdir -p "$RHOME/.ssh"
touch "$RHOME/.ssh/authorized_keys"
chown "$RUSER:" "$RHOME/.ssh" "$RHOME/.ssh/authorized_keys" 2>/dev/null || true
chmod 700 "$RHOME/.ssh"
chmod 600 "$RHOME/.ssh/authorized_keys"
if grep -qF "$KEY" "$RHOME/.ssh/authorized_keys"; then
    echo "hub key already authorized"
else
    printf '%s\n' "$KEY" >> "$RHOME/.ssh/authorized_keys"
    echo "hub key authorized — the hub can reach this Pi"
fi
