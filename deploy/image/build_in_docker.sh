#!/usr/bin/env bash
# build_in_docker.sh — run build_hub_image.sh inside a container, for a box
# where you have docker but would rather not sudo or install build tools.
# Registers arm64 emulation with the kernel for the build if it is missing
# (it lasts until reboot).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/deploy/image/out"; mkdir -p "$OUT"
exec docker run --rm --privileged \
    -v "$REPO":/src:ro -v "$OUT":/out \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    debian:13-slim bash -ec '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -q >/dev/null
        apt-get install -y -q --no-install-recommends parted e2fsprogs xz-utils curl ca-certificates git qemu-user-static mount util-linux >/dev/null
        mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
        if [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
            printf "%s" ":qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-aarch64-static:F" > /proc/sys/fs/binfmt_misc/register
        fi
        /src/deploy/image/build_hub_image.sh --out /out "$@"
    ' build "$@"
