#!/usr/bin/env bash
# build_hub_image.sh — build the ready-to-flash CabiNet hub SD card image.
#
#   sudo deploy/image/build_hub_image.sh [--out DIR] [--no-kiosk]
#   deploy/image/build_in_docker.sh      [same options]   # no sudo, needs docker
#
# Takes the stock Raspberry Pi OS Lite (64-bit) image, pinned below by
# checksum, and adds exactly what a hub needs so the card works with no
# internet and no typing:
#   - git, plus (unless --no-kiosk) the kiosk's cage + chromium
#   - this checkout's HEAD commit, as a real git clone in the first account's
#     home (origin = the public repo, so Settings ▸ Updates works)
#   - cabinet-hub-firstboot: runs deploy/hub_setup.sh by itself on first boot
#   - first-boot defaults (hostname "cabinet", login cabinet/cabinet)
# Output: DIR/cabinet-hub-<version>.img.xz and its .sha256.
#
# Needs: Linux, root, losetup parted e2fsprogs xz curl git, and arm64
# emulation (qemu-user-static registered with binfmt_misc).
set -euo pipefail

BASE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz"
BASE_SHA256="483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843"
PUBLIC_REPO="https://github.com/ajs1616/CabiNet.git"
GROW_MB=2560          # room for the kiosk packages + floor data before first-boot resize

fail() { printf 'build_hub_image: %s\n' "$*" >&2; exit 1; }
say()  { printf '==> %s\n' "$*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT="$REPO/deploy/image/out"; KIOSK=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --no-kiosk) KIOSK=0; GROW_MB=768; shift ;;
        -h|--help) sed -n '2,/^$/ s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) fail "unknown option $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || fail "run me with sudo, or use build_in_docker.sh"
for t in losetup parted e2fsck resize2fs xz curl git sha256sum; do
    command -v "$t" >/dev/null || fail "missing tool: $t"
done
[[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 || "$(uname -m)" == "aarch64" ]] \
    || fail "arm64 emulation is not registered (install qemu-user-static, or use build_in_docker.sh)"

git config --global --add safe.directory "$REPO" 2>/dev/null || true
git config --global --add safe.directory "$REPO/.git" 2>/dev/null || true
VERSION="$(git -C "$REPO" describe --tags --always 2>/dev/null || echo dev)"
COMMIT="$(git -C "$REPO" rev-parse HEAD)"
mkdir -p "$OUT"; CACHE="${CABINET_IMAGE_CACHE:-$OUT/cache}"; mkdir -p "$CACHE"
IMG="$OUT/cabinet-hub-$VERSION.img"

# ── base image ──────────────────────────────────────────────────────────────
BASE_XZ="$CACHE/$(basename "$BASE_URL")"
if ! echo "$BASE_SHA256  $BASE_XZ" | sha256sum -c --status 2>/dev/null; then
    say "downloading Raspberry Pi OS Lite"
    curl -fL --retry 3 -o "$BASE_XZ" "$BASE_URL"
    echo "$BASE_SHA256  $BASE_XZ" | sha256sum -c --status || fail "base image checksum mismatch"
fi
say "unpacking to $IMG"
xz -dc "$BASE_XZ" > "$IMG"

# ── grow the root partition ─────────────────────────────────────────────────
truncate -s "+${GROW_MB}M" "$IMG"
parted -s "$IMG" resizepart 2 100%
read -r BOOT_START BOOT_SIZE < <(parted -sm "$IMG" unit B print | awk -F: '$1==1{gsub("B","");print $2, $4}')
read -r ROOT_START ROOT_SIZE < <(parted -sm "$IMG" unit B print | awk -F: '$1==2{gsub("B","");print $2, $4}')

MNT="$(mktemp -d)"; LROOT=""; LBOOT=""
cleanup() {
    set +e
    for m in dev/pts dev proc sys boot/firmware ""; do
        mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"
    done
    [[ -n "$LBOOT" ]] && losetup -d "$LBOOT"
    [[ -n "$LROOT" ]] && losetup -d "$LROOT"
    rmdir "$MNT" 2>/dev/null
}
trap cleanup EXIT

LROOT="$(losetup --show -f --offset "$ROOT_START" --sizelimit "$ROOT_SIZE" "$IMG")"
LBOOT="$(losetup --show -f --offset "$BOOT_START" --sizelimit "$BOOT_SIZE" "$IMG")"
e2fsck -fy "$LROOT" >/dev/null || [[ $? -le 1 ]] || fail "root filesystem check failed"
resize2fs "$LROOT" >/dev/null
mount "$LROOT" "$MNT"
mount "$LBOOT" "$MNT/boot/firmware"
for m in dev dev/pts proc sys; do mount --bind "/$m" "$MNT/$m"; done

# ── packages ────────────────────────────────────────────────────────────────
PKGS=(git)
[[ $KIOSK -eq 1 ]] && PKGS+=(cage chromium)
say "installing: ${PKGS[*]}"
mv "$MNT/etc/resolv.conf" "$MNT/etc/resolv.conf.cabinet-build" 2>/dev/null || true
cp /etc/resolv.conf "$MNT/etc/resolv.conf"
# no services may start inside the build
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"; chmod +x "$MNT/usr/sbin/policy-rc.d"
chroot "$MNT" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C \
    sh -c "apt-get update -q && apt-get install -y -q ${PKGS[*]} && apt-get clean && rm -rf /var/lib/apt/lists/*"
rm -f "$MNT/usr/sbin/policy-rc.d" "$MNT/etc/resolv.conf"
mv "$MNT/etc/resolv.conf.cabinet-build" "$MNT/etc/resolv.conf" 2>/dev/null || true

# ── the checkout ────────────────────────────────────────────────────────────
# A real clone of HEAD (committed files only — never this box's floor data),
# pointed at the public repo so the hub's updater has somewhere to pull from.
PIHOME="$MNT/home/pi"
[[ -d "$PIHOME" ]] || fail "base image has no /home/pi"
say "adding CabiNet $VERSION ($COMMIT)"
BRANCH="$(git -C "$REPO" symbolic-ref --short -q HEAD || echo main)"
git clone -q --no-hardlinks "$REPO" "$PIHOME/CabiNet"
git -C "$PIHOME/CabiNet" checkout -q -B main "$COMMIT"
[[ "$BRANCH" == "main" ]] || git -C "$PIHOME/CabiNet" branch -q -D "$BRANCH" 2>/dev/null || true
git -C "$PIHOME/CabiNet" remote set-url origin "$PUBLIC_REPO"
# The clone's idea of "origin" is the build box's checkout — every local
# branch it had. The hub should know one thing: public main, at this commit.
git -C "$PIHOME/CabiNet" for-each-ref --format='%(refname)' refs/remotes refs/tags \
    | while read -r ref; do git -C "$PIHOME/CabiNet" update-ref -d "$ref"; done
git -C "$PIHOME/CabiNet" update-ref refs/remotes/origin/main "$COMMIT"
git -C "$PIHOME/CabiNet" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
git -C "$PIHOME/CabiNet" config branch.main.remote origin
git -C "$PIHOME/CabiNet" config branch.main.merge refs/heads/main
chown -R 1000:1000 "$PIHOME/CabiNet"

# ── first boot ──────────────────────────────────────────────────────────────
install -m 755 "$SCRIPT_DIR/hub_firstboot.sh" "$MNT/usr/local/sbin/cabinet-hub-firstboot.sh"
install -m 644 "$SCRIPT_DIR/cabinet-hub-firstboot.service" "$MNT/etc/systemd/system/"
ln -sf ../cabinet-hub-firstboot.service \
    "$MNT/etc/systemd/system/multi-user.target.wants/cabinet-hub-firstboot.service"
install -m 644 "$SCRIPT_DIR/user-data" "$MNT/boot/firmware/user-data"
printf 'CabiNet hub image %s (%s), built %s\n' "$VERSION" "$COMMIT" "$(date -u +%F)" \
    > "$MNT/etc/cabinet-image"

sync; cleanup; trap - EXIT
say "compressing"
xz -T0 -6 -f "$IMG"
( cd "$OUT" && sha256sum "$(basename "$IMG").xz" > "$(basename "$IMG").xz.sha256" )
[[ -n "${SUDO_UID:-${HOST_UID:-}}" ]] && chown "${SUDO_UID:-$HOST_UID}:${SUDO_GID:-${HOST_GID:-0}}" "$IMG.xz" "$IMG.xz.sha256" || true
say "built $IMG.xz"
