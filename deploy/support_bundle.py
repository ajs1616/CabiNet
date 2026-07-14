#!/usr/bin/env python3
"""support_bundle.py — one command, one file to send when something breaks.

Run it on whichever box is misbehaving and attach the .tar.gz it prints:

    python3 deploy/support_bundle.py            # on the host box
    python3 ~/CasinoNet/deploy/support_bundle.py    # on a satellite Pi

It auto-detects what this box is (host / SAS SMIB / Companion — a box can be
several at once) from the installed casinonet-* units, falling back to the
repo layout when no units are installed. It gathers only facts: unit
journals, the host's rotating wire/host logs, state snapshots from the live
API, the SQLite spine, network/system/hardware info. Read-only by design —
it never restarts anything, never edits anything, and it always finalizes a
readable bundle even if a collector crashes mid-run (the crash lands in the
MANIFEST instead of killing the archive).

Stdlib only. Root not required, but unit journals usually need it — the
script tries plain journalctl first, then `sudo -n`, and records in
MANIFEST.txt whatever it couldn't read so nobody chases missing data
silently. The bundle file is created mode 0600.

Privacy note (also written into the bundle): the archive contains YOUR
floor's state — machine ids, player names, fob ids, fun-money balances,
protocol traffic with your machines, and this box's network addresses.
Send it to the CabiNet developer; don't post it publicly.
"""

import argparse
import datetime
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = "1.1"
UNITS = [
    "casinonet-g2s", "casinonet-dhcp", "casinonet-dns", "casinonet-ntp",
    "casinonet-tftp", "casinonet-console", "casinonet-sas",
    "casinonet-companion", "casinonet-smibui", "casinonet-kiosk",
]
HOST_UNIT = "casinonet-g2s"
UNIT_DIRS = ("/etc/systemd/system", "/usr/lib/systemd/system",
             "/lib/systemd/system")
API_SNAPSHOTS = [
    "/api/status", "/api/accounts", "/api/settings", "/api/fobs",
    "/api/names", "/api/players", "/api/tito/tickets", "/api/vouchers",
    "/api/debug/log",
]
TAIL_BYTES = 5 * 1024 * 1024       # cap per log/journal/API artifact
LOG_GROUP_LIMIT = 6                # newest N log files PER prefix group
DB_MAX_BYTES = 100 * 1024 * 1024   # skip a database bigger than this

# localhost API calls must never detour through an http_proxy env var
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_rc(cmd, timeout=20):
    """(returncode, text) — rc is None when the command couldn't run at all."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        out = p.stdout
        if p.returncode != 0 and p.stderr.strip():
            out += f"\n[exit {p.returncode}] {p.stderr.strip()}\n"
        return p.returncode, out
    except FileNotFoundError:
        return None, f"[not installed: {cmd[0]}]\n"
    except subprocess.TimeoutExpired:
        return None, f"[timed out after {timeout}s: {' '.join(cmd)}]\n"
    except Exception as e:  # noqa: BLE001 — a bundle must never die mid-collect
        return None, f"[error running {' '.join(cmd)}: {e}]\n"


def run(cmd, timeout=20):
    return run_rc(cmd, timeout)[1]


def truncate_bytes(data, cap=TAIL_BYTES):
    """Keep the LAST cap bytes (byte-accurate; logs matter most at the end)."""
    if len(data) <= cap:
        return data
    return (b"[... truncated to last %d bytes ...]\n" % cap) + data[-cap:]


class Bundle:
    """tar.gz writer + manifest: every add lands in one or the other."""

    def __init__(self, path):
        self.path = path
        # 0600 from birth — the archive holds the tester's floor data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.tar = tarfile.open(fileobj=os.fdopen(fd, "wb"), mode="w:gz")
        self.manifest = []
        self.closed = False

    def add_bytes(self, name, data, mtime=None):
        try:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(mtime if mtime is not None
                             else datetime.datetime.now().timestamp())
            info.mode = 0o600
            self.tar.addfile(info, io.BytesIO(data))
            self.manifest.append(f"OK    {name} ({len(data):,} bytes)")
        except Exception as e:  # noqa: BLE001
            self.skip(name, f"tar write failed: {e}")

    def add_text(self, name, text):
        self.add_bytes(name, truncate_bytes(
            text.encode("utf-8", "replace")))

    def add_file(self, name, src, cap=None):
        try:
            with open(src, "rb") as f:
                data = f.read((cap or 0) + 1 if cap else -1)
            if cap and len(data) > cap:
                # re-read the tail — the END of a log is the useful end
                size = os.path.getsize(src)
                with open(src, "rb") as f:
                    f.seek(max(0, size - cap))
                    data = truncate_bytes(f.read(), cap)
            self.add_bytes(name, data, mtime=os.path.getmtime(src))
        except Exception as e:  # noqa: BLE001
            self.skip(name, str(e))

    def skip(self, name, why):
        self.manifest.append(f"SKIP  {name} — {why}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            data = ("\n".join(self.manifest) + "\n").encode("utf-8", "replace")
            info = tarfile.TarInfo("MANIFEST.txt")
            info.size = len(data)
            info.mtime = int(datetime.datetime.now().timestamp())
            info.mode = 0o600
            self.tar.addfile(info, io.BytesIO(data))
        finally:
            self.tar.close()


def detect_roles():
    """casinonet units installed on this box (installed, not just active)."""
    present = []
    for u in UNITS:
        for d in UNIT_DIRS:
            if os.path.exists(os.path.join(d, u + ".service")):
                present.append(u)
                break
    return present


def repo_root():
    """The CasinoNet/CabiNet checkout this script lives in, or None."""
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.dirname(here), here):
        if any(os.path.isdir(os.path.join(cand, d))
               for d in ("G2S", "SAS", "Companion")):
            return cand
    return None


def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def journal(bundle, unit, since):
    rc, out = run_rc(["journalctl", "-u", unit, "--since", since,
                      "--no-pager", "-o", "short-iso"], timeout=60)
    if rc != 0 or not out.strip():
        src, (rc2, out2) = "sudo", run_rc(
            ["sudo", "-n", "journalctl", "-u", unit, "--since", since,
             "--no-pager", "-o", "short-iso"], timeout=60)
        if rc2 == 0 and out2.strip():
            out = out2
        elif rc != 0 or not out.strip():
            why = (out or out2).strip().splitlines()
            bundle.skip(f"journal/{unit}.log",
                        "not readable (%s) — re-run with sudo for unit logs"
                        % (why[-1][:120] if why else "no output"))
            return
    bundle.add_bytes(f"journal/{unit}.log",
                     truncate_bytes(out.encode("utf-8", "replace")))


def api_snapshot(bundle, base, path):
    name = "api" + path.replace("/", "_") + ".json"
    try:
        with _OPENER.open(base + path, timeout=5) as r:
            data = r.read(TAIL_BYTES + 1)
        if len(data) > TAIL_BYTES:
            data = data[:TAIL_BYTES] + b"\n[... truncated ...]"
        bundle.add_bytes(name, data)
    except Exception as e:  # noqa: BLE001 — a down host is a finding, not a crash
        bundle.add_text(name, json.dumps(
            {"error": f"could not fetch {path}: {e}"}, indent=2))


def log_group(fname):
    """g2s_wire_20260714_121726.log.1 -> 'g2s_wire' (newest-N per group)."""
    return re.sub(r"_\d{8}[_-]?\d{0,6}.*$", "", fname) or fname


def copy_dir_files(bundle, srcdir, arcprefix, cap=None, group_limit=None):
    try:
        names = [f for f in os.listdir(srcdir)
                 if os.path.isfile(os.path.join(srcdir, f))]
    except OSError as e:
        bundle.skip(arcprefix + "/", str(e))
        return
    names.sort(key=lambda f: safe_mtime(os.path.join(srcdir, f)),
               reverse=True)
    seen = {}
    for f in names:
        g = log_group(f)
        seen[g] = seen.get(g, 0) + 1
        if group_limit and seen[g] > group_limit:
            bundle.skip(f"{arcprefix}/{f}",
                        f"older than the newest {group_limit} '{g}' files")
            continue
        bundle.add_file(f"{arcprefix}/{f}", os.path.join(srcdir, f), cap=cap)


def snapshot_sqlite(bundle, src, arcname):
    """Consistent copy of a live WAL-mode SQLite db via the backup API."""
    tmp = None
    try:
        if not os.path.exists(src):
            bundle.skip(arcname, "absent")
            return
        if os.path.getsize(src) > DB_MAX_BYTES:
            bundle.skip(arcname, f"larger than {DB_MAX_BYTES:,} bytes")
            return
        fd, tmp = tempfile.mkstemp(suffix=".db-snapshot")
        os.close(fd)
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as srcdb, \
                sqlite3.connect(tmp) as dstdb:
            srcdb.backup(dstdb)
        bundle.add_file(arcname, tmp)
    except Exception as e:  # noqa: BLE001
        bundle.skip(arcname, f"sqlite backup failed ({e}) — raw-copying "
                             "main + sidecars instead")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(src + suffix):
                bundle.add_file(arcname + suffix + (".raw" if not suffix
                                                   else ""), src + suffix)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def unit_interpreters(roles):
    """Python interpreters the units actually run (venvs matter on SMIBs)."""
    interps = set()
    for u in roles:
        for d in UNIT_DIRS:
            p = os.path.join(d, u + ".service")
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = re.match(r"ExecStart=(\S*python[\w.]*)", line)
                        if m and m.group(1) not in ("/usr/bin/python3",):
                            interps.add(m.group(1))
            except OSError:
                pass
            break
    return sorted(interps)


def collect(b, args, roles, roles_inferred, root):
    hostn = socket.gethostname()
    b.add_text("BUNDLE_README.txt", (
        "CabiNet support bundle\n"
        "======================\n"
        f"created: {datetime.datetime.now().isoformat()}\n"
        f"box: {hostn}\nroles: {', '.join(roles) or 'none detected'}"
        f"{' (inferred from repo layout — no units installed)' if roles_inferred else ''}\n"
        f"script: v{VERSION}\n\n"
        "Contents: unit journals, CabiNet logs, state snapshots, hardware,\n"
        "system and network info from THIS box only. It contains your\n"
        "floor's data — machine ids, player names, fob ids, fun-money\n"
        "balances, protocol traffic — plus this box's network addresses\n"
        "and a list of its listening services.\n"
        "Send it to the CabiNet developer with a note about what went wrong\n"
        "and when (clock time matters — journals are timestamped).\n"
        "Don't post it publicly.\n"))

    meta = {
        "version": VERSION,
        "created": datetime.datetime.now().isoformat(),
        "hostname": hostn,
        "roles": roles,
        "roles_inferred": roles_inferred,
        "repo_root": root,
        "python": sys.version,
        "argv_since": args.since,
    }
    if root:
        meta["git_head"] = run(["git", "-C", root, "rev-parse", "HEAD"]).strip()
        meta["git_last_commit"] = run(["git", "-C", root, "log", "-1",
                                       "--format=%h %cI %s"]).strip()
        meta["git_status"] = run(["git", "-C", root, "status",
                                  "--porcelain"]).strip()
        meta["git_diff_stat"] = run(["git", "-C", root, "diff",
                                     "--stat"]).strip()
    b.add_text("meta.json", json.dumps(meta, indent=2))

    b.add_text("system.txt", "".join([
        "## uname -a\n", run(["uname", "-a"]),
        "\n## os-release\n", run(["cat", "/etc/os-release"]),
        "\n## uptime\n", run(["uptime"]),
        "\n## free -h\n", run(["free", "-h"]),
        "\n## df -h\n", run(["df", "-h"]),
        "\n## ip addr\n", run(["ip", "addr"]),
        "\n## ip route\n", run(["ip", "route"]),
        "\n## ip neigh (who's on the slot segment)\n", run(["ip", "neigh"]),
        "\n## listening sockets\n", run(["ss", "-ltnup"]),
        "\n## time sync\n", run(["timedatectl"]),
        run(["timedatectl", "timesync-status"]),
    ]))

    # hardware facts — useful on ANY box (the host may be a Pi too)
    hw = []
    if os.path.exists("/proc/device-tree/model"):
        hw += ["## board\n",
               run(["sh", "-c", "tr -d '\\0' < /proc/device-tree/model"]),
               "\n"]
    hw += ["\n## serial + i2c devices\n",
           run(["sh", "-c",
                "ls -la /dev/ttyAMA* /dev/ttyS* /dev/i2c* 2>&1 || true"]),
           "\n## throttling / undervoltage (Pi)\n",
           run(["vcgencmd", "get_throttled"]),
           run(["vcgencmd", "measure_temp"])]
    rc, dmesg = run_rc(["dmesg"], timeout=15)
    if rc != 0 or not dmesg.strip():
        rc2, dmesg2 = run_rc(["sudo", "-n", "dmesg"], timeout=15)
        dmesg = dmesg2 if (rc2 == 0 and dmesg2.strip()) else \
            "[dmesg not readable without sudo]\n"
    hw += ["\n## dmesg tail\n",
           "\n".join(dmesg.splitlines()[-100:]) + "\n"]
    b.add_text("hardware.txt", "".join(hw))
    for cfg in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.exists(cfg):
            b.add_file("boot_config.txt", cfg)
            break

    units_txt = run(["systemctl", "list-units", "--all", "--no-pager",
                     "casinonet-*"])
    for u in roles if not roles_inferred else []:
        units_txt += f"\n{'='*60}\n"
        units_txt += run(["systemctl", "status", u, "--no-pager", "-l"])
        units_txt += "\n--- unit file (systemctl cat) ---\n"
        units_txt += run(["systemctl", "cat", u, "--no-pager"])
    b.add_text("units.txt", units_txt)

    interps = unit_interpreters(roles if not roles_inferred else [])
    if interps:
        env_txt = ""
        for i in interps:
            env_txt += f"## {i}\n"
            env_txt += run([i, "--version"])
            env_txt += run([i, "-m", "pip", "list", "--format=freeze"])
            env_txt += "\n"
        b.add_text("python_env.txt", env_txt)

    if not roles_inferred:
        for u in roles:
            journal(b, u, args.since)

    is_host = HOST_UNIT in roles or (roles_inferred and root
                                     and os.path.isdir(os.path.join(root, "G2S")))
    is_sat = any(u in roles for u in ("casinonet-sas", "casinonet-companion",
                                      "casinonet-smibui")) or \
        (roles_inferred and root and os.path.isdir(os.path.join(root, "SAS")))

    if is_host and root:
        g2s = os.path.join(root, "G2S")
        copy_dir_files(b, os.path.join(g2s, "logs"), "host/logs",
                       cap=TAIL_BYTES, group_limit=LOG_GROUP_LIMIT)
        data = os.path.join(g2s, "data")
        try:
            entries = sorted(os.listdir(data)) if os.path.isdir(data) else None
        except OSError as e:
            entries, _ = None, b.skip("host/data/", str(e))
        if entries is None:
            b.skip("host/data/", "directory absent/unreadable")
        else:
            for f in entries:
                src = os.path.join(data, f)
                if not os.path.isfile(src):
                    continue
                if f.endswith(".db"):
                    snapshot_sqlite(b, src, f"host/data/{f}")
                elif re.search(r"\.db(-wal|-shm)$|\.db\.bak.*$|\.seq$", f):
                    b.skip(f"host/data/{f}", "db sidecar (snapshot covers it)")
                else:
                    b.add_file(f"host/data/{f}", src, cap=TAIL_BYTES)
        for leases in ("/var/lib/misc/dnsmasq.leases",):
            if os.path.exists(leases):
                b.add_file("host/dnsmasq.leases", leases)
    elif is_host:
        b.skip("host/", "repo root not found — repo-relative collection skipped")

    if is_host:
        for path in API_SNAPSHOTS:
            api_snapshot(b, args.hub, path)

    if is_sat and root:
        copy_dir_files(b, os.path.join(root, "SAS", "data"),
                       "satellite/sas_data", cap=TAIL_BYTES)

    if not roles and not root:
        b.add_text("NOTE.txt",
                   "No casinonet-* units and no CabiNet repo found from this "
                   "script's location.\nIf this box should be running "
                   "CabiNet, that IS the finding. System info still applies.\n")


def main():
    ap = argparse.ArgumentParser(
        description="Gather CabiNet diagnostics into one .tar.gz to send.")
    ap.add_argument("--since", default="48 hours ago",
                    help='journal window (journalctl syntax, default "48 hours ago")')
    ap.add_argument("--out", default=".",
                    help="directory for the bundle file (created if missing)")
    ap.add_argument("--hub", default="http://127.0.0.1:8081",
                    help="host API base for snapshots (host role only)")
    args = ap.parse_args()

    roles = detect_roles()
    roles_inferred = False
    root = repo_root()
    if not roles and root:
        roles_inferred = True
        if os.path.isdir(os.path.join(root, "G2S")):
            roles.append(HOST_UNIT)
        if os.path.isdir(os.path.join(root, "SAS")):
            roles.append("casinonet-sas")

    hostn = socket.gethostname()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rolestr = "host" if HOST_UNIT in roles else \
        ("satellite" if roles else "unknown")
    out_dir = os.path.abspath(os.path.expanduser(args.out))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print(f"ERROR: cannot create output directory {out_dir}: {e}")
        return 1
    out_path = os.path.join(
        out_dir, f"cabinet-support_{rolestr}_{hostn}_{stamp}.tar.gz")

    print(f"CabiNet support bundle v{VERSION}")
    print(f"  box: {hostn} · roles: {', '.join(roles) or 'none detected'}"
          f"{' (inferred)' if roles_inferred else ''}")
    print(f"  repo: {root or '(not found)'}")
    print("  collecting (read-only) ...")

    try:
        b = Bundle(out_path)
    except OSError as e:
        print(f"ERROR: cannot create {out_path}: {e}")
        return 1
    try:
        collect(b, args, roles, roles_inferred, root)
    except BaseException as e:  # noqa: BLE001 — record the crash, keep the bundle
        b.skip("(collection)", f"collector crashed: {type(e).__name__}: {e}")
        raise
    finally:
        b.close()
        size = os.path.getsize(out_path)
        print(f"\n  wrote {out_path} ({size/1024/1024:.1f} MB)")
        print("  → attach this file to your report, with what went wrong "
              "+ when.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
