#!/usr/bin/env python3
"""cabinetconfig — the CabiNet fleet REPAIR menu.

Run this ON THE HUB, from inside your CabiNet clone:

    ./cabinetconfig                       # the menu
    ./cabinetconfig smib smib-bb2 doctor  # every menu leaf is also a command
    ./cabinetconfig hub doctor --json     # machine-readable doctor output

A HEALTHY FLOOR NEVER NEEDS THIS TOOL. Zero-config onboarding (the setup
scripts + hub DHCP) is step 1, always; this exists for the night something is
MISconfigured and nobody can say which of the look-alike broken states they
are in. Its first job is diagnosis — naming the state (a SAS leg can be off
three different ways: NEVER-PROVISIONED / UNIT-OFF / HUB-PARKED, and they need
three different fixes) — and only then the handful of repairs that genuinely
have no other path.

WHAT IT IS
  * Repairs are hub-only; LOOKING is not. Two independent questions decide
    the whole shape of a run: am I the hub (DEAD-STATE evidence — this tree
    is the one the installed cabinet-g2s unit points at, hub unit files
    exist, .50.2 on a NIC; never API liveness, because the tool must work on
    exactly the nights cabinet-g2s is dead or :8081 has an imposter), and is
    a hub REACHABLE. Identity decides mutations, reachability decides
    visibility, and there are three places:
      1. on the hub — everything, repairs included;
      2. off it with a hub in reach (a routed VLAN) — the whole floor,
         read-only, with each repair relocated to its exact hub command;
      3. off it with no hub in reach — the front door, which explains what
         the tool is and how to get to the hub.
    API down while ON the hub = DEGRADED mode (discovery falls back to
    leases + `ip neigh` + SSH probes), not exit.
  * Repair-only, no opinions of its own: every mutation replays an existing
    proven mechanism (a setup script, a single systemctl), probes first, shows
    the literal plan, confirms, verifies on the wire, one outcome line.
  * A guest, never an authority: the hub-API write whitelist is EMPTY, `*/data`
    is read-only, it never invents an identity, never syncs code (that is
    update.py's job), and it re-derives everything live so it can never drift
    into being a second config system.

WHAT IT WILL NOT DO
  * No EGM-level config (denoms, enables, ticket header) — web UI + proven
    ceremonies only; this tool signposts.
  * No hub.db writes, no hub-API writes, ever. Gate-asserted.
  * Never apt, never rsync, never a binding/link/label, never a rename.
"""

import argparse
import contextlib
import glob
import io
import json
import os
import re
import shlex
import socket
import struct
import sys
import textwrap
import time

# ---------------------------------------------------------------------------
# VOICE — imported from update.py, then teed
# ---------------------------------------------------------------------------
# update.py is the house voice (say/step/run, the Proceed?-idiom) and the
# import source for the fleet idioms (sat_ssh, hub_status, find_satellites,
# repo_root). It carries a __main__ guard, so importing it runs nothing.
# The shim makes the import work from ANY cwd — including a bare checkout on
# a dev box and the gate's throwaway worktree — and sits FIRST on sys.path so
# a site-packages module named `update` can never shadow ours.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import update as _u

Fail = _u.Fail

HUB_URL = "http://127.0.0.1:8081"      # identity is local; no flag for this
DEFAULT_KEY = os.path.expanduser("~/.ssh/smib")
UNIT_DIR = "/etc/systemd/system"
LOCK_NAME = ".cabinet-update.lock"      # SHARED with update.py — one writer
LEASES_REL = "G2S/data/dhcp_leases.json"   # read-only, degraded discovery

# The transcript lives in $HOME — NEVER under any */data path (data/ is
# sacred and excluded from every sync; a log growing there would either be
# clobber-bait or snapshot bloat, and update.py's excludes exist because of
# exactly this class of mistake).
TRANSCRIPT = os.path.expanduser("~/.cabinetconfig-last.log")
TRANSCRIPT_KEEP = 5

_LOG = None                             # open transcript file, or None
_ANSI = re.compile(r"\x1b\[[0-9;]*m")   # keep the log grep-able
# Bold is the only escape this tool emits. A pipe, a TERM=dumb console and a
# gate walk must all get plain text — the screens are read through all three.
_ANSI_OK = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _open_transcript():
    """Rotate then open ~/.cabinetconfig-last.log. Siblings are timestamped
    from the old file's mtime (the moment that session ended), keep-5."""
    global _LOG
    home = os.path.dirname(TRANSCRIPT)
    if os.path.exists(TRANSCRIPT):
        stamp = time.strftime("%Y%m%d-%H%M%S",
                              time.localtime(os.path.getmtime(TRANSCRIPT)))
        os.replace(TRANSCRIPT,
                   os.path.join(home, ".cabinetconfig-%s.log" % stamp))
    sibs = sorted(glob.glob(os.path.join(home, ".cabinetconfig-[0-9]*.log")))
    for old in sibs[:-TRANSCRIPT_KEEP]:
        try:
            os.unlink(old)
        except OSError:
            pass
    try:
        _LOG = open(TRANSCRIPT, "a")
        _LOG.write("# cabinetconfig transcript — %s\n"
                   % time.strftime("%Y-%m-%d %H:%M:%S"))
        _LOG.flush()
    except OSError:
        _LOG = None                     # a read-only $HOME must not kill us


def say(msg=""):
    if _LOG:
        try:
            _LOG.write(_ANSI.sub("", str(msg)) + "\n")
            _LOG.flush()
        except OSError:
            pass
    print(msg if _ANSI_OK else _ANSI.sub("", str(msg)), flush=True)


def step(msg):
    say("\n\033[1m== %s\033[0m" % msg)


# Route update.py's own voice (the `   $ cmd` echo inside run/sat_ssh) through
# the tee, so the transcript is COMPLETE — the command echoes are the part a
# post-mortem needs most.
_u.say = say


def confirm(question, assume_yes=False):
    """The [y/N] posture, update.py's exact idiom: default-no, --yes skips,
    and no-terminal (ssh one-liner, gate pipe) refuses cleanly instead of
    dying in a traceback. Returns True only on an explicit yes."""
    if assume_yes:
        say("   (--yes) %s — proceeding." % question)
        return True
    try:
        ans = input("%s [y/N] " % question).strip().lower()
    except EOFError:
        say("No terminal to confirm on — rerun with --yes.")
        return False
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# TRANSPORT — one interface, three carriers
# ---------------------------------------------------------------------------

class Transport(object):
    """run(cmd) -> (rc, out). `cmd` is a shell string on the REMOTE side for
    ssh (sat_ssh semantics) and a shell string locally for the hub — one
    grammar everywhere so a doctor probe reads identically in all three."""
    name = "?"

    def run(self, cmd, check=False, timeout=30, quiet=False):
        raise NotImplementedError

    def reachable(self):
        """One cheap round-trip before a doctor or verb runs its battery:
        can this carrier answer AT ALL? Local and mock carriers already
        can; ssh must PROVE it — a dead box fails every probe identically
        to a box with no units, and without this gate it classifies as
        NEVER-PROVISIONED: a confidently wrong verdict that sends someone
        re-provisioning a Pi that is merely dark or unkeyed."""
        return True


class LocalTransport(Transport):
    """The hub itself (and the loopback enrollment reader)."""

    def __init__(self):
        self.name = "hub"

    def run(self, cmd, check=False, timeout=30, quiet=False):
        return _u.run(cmd, check=check, timeout=timeout, quiet=quiet)


class SshTransport(Transport):
    """A satellite, via update.py's sat_ssh idiom: -i ~/.ssh/smib,
    BatchMode=yes (never hangs on a password prompt), ConnectTimeout=8."""

    def __init__(self, peer, user=None, key=DEFAULT_KEY):
        self.peer = peer
        self.user = user or _u.SAT_USER
        self.key = key
        self.name = "%s@%s" % (self.user, peer)

    def run(self, cmd, check=False, timeout=30, quiet=False):
        # sat_ssh's exact argv, built here because sat_ssh has no quiet=
        # knob: a doctor's dozen probes must not drown the verdict under a
        # page of `$ ssh …` echo lines (the local hub probes are already
        # silent — the two doctors must read identically). Mutations keep
        # the echo: that command line is the transcript's most valuable one.
        return _u.run(["ssh", "-i", self.key, "-o", "BatchMode=yes",
                       "-o", "StrictHostKeyChecking=accept-new",
                       "-o", "ConnectTimeout=8",
                       "%s@%s" % (self.user, self.peer), cmd],
                      check=check, timeout=timeout, quiet=quiet)

    def reachable(self):
        # `true` is the whole payload — the same bar as discovery's probe:
        # sshd answered and the hub key is authorized, nothing more.
        rc, _out = self.run("true", check=False, timeout=15, quiet=True)
        return rc == 0


class MockTransport(Transport):
    """Gate fixtures: canned {cmd: (rc, out)} plus a journal of every command
    asked, so the gate can assert BOTH what a doctor concluded and exactly
    what it ran (the zero-*/data-writes law is an assert over this journal)."""

    def __init__(self, responses, name="mock", reachable=True):
        self.responses = dict(responses or {})
        self.journal = []
        self.name = name
        self._reachable = reachable   # the gate's dead-box fixture knob

    def run(self, cmd, check=False, timeout=30, quiet=False):
        self.journal.append(cmd)
        rc, out = self.responses.get(
            cmd, (127, "mock: no canned response for %r" % cmd))
        if check and rc != 0:
            raise Fail("command failed (rc=%d): %s\n%s" % (rc, cmd, out))
        return rc, out

    def reachable(self):
        return self._reachable


def atomic_write(path, text):
    """EVERY file rewrite in this tool goes through here: write a temp
    sibling, fsync, rename, fsync the directory. A power blip mid-write
    must never leave a truncated unit file on a headless Pi — and the
    rename itself must land too, or a blip right after replace() can still
    lose the directory entry. (No callers yet: Phase 0/1 rewrites no files
    — the write rail waits here for the first unit-rewrite verb to earn
    its way in.)"""
    tmp = "%s.tmp-cabinetconfig" % path
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# hub identity + degraded mode
# ---------------------------------------------------------------------------

def api_status(timeout=3):
    """GET /api/status on the local hub. Returns the parsed dict, or
    {"__error__": ...} — update.py's hub_status shape. This is a DIAGNOSTIC,
    never the identity gate."""
    return _u.hub_status(HUB_URL, timeout=timeout)


def _default_gateway():
    """The IPv4 default-route gateway from /proc/net/route (lowest metric —
    the kernel's preferred egress), or None. Same derivation
    Companion/companion_host.py uses: on the slot segment the hub IS the
    gateway, which is what lets a flagless satellite find it with no config.
    On a laptop the gateway is the house router — which is exactly why it is
    tried SECOND, after the documented hub address."""
    best_ip, best_metric = None, None
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) < 8:
                    continue
                dest, gw, flags = fields[1], fields[2], int(fields[3], 16)
                metric = int(fields[6])
                if dest == "00000000" and (flags & 0x2):
                    if best_metric is None or metric < best_metric:
                        best_metric = metric
                        best_ip = socket.inet_ntoa(
                            struct.pack("<L", int(gw, 16)))
    except (OSError, ValueError):
        return None
    return best_ip


def hub_locate(explicit=None):
    """Find a hub from OFF the hub. Hub IDENTITY and hub REACHABILITY are
    different questions: identity decides whether repairs may run, and this
    one decides what can be SEEN. A managed VLAN routes a laptop straight to
    the floor, and answering "no hub evidence" there was the bug.

    Conventional, no new config system, first answer wins: --hub, then the
    documented default, then this box's own default gateway. Documented
    default BEFORE gateway on purpose — on a laptop the gateway is the house
    router, so probing it first buys a wasted timeout on every launch.
    1.5 s each, so a laptop with no hub reaches the front door in ~3 s.
    `--hub none` skips discovery entirely.

    The probe is urllib (update.py's hub_status), never a shell curl through
    a Transport: the doctors' single allowed /api/hubkey GET is the whole
    hub-API surface this tool is permitted, and the gate asserts it."""
    if explicit == "none":
        return {"url": None, "how": None, "status": None, "tried": []}
    cands = []
    if explicit:
        cands.append((explicit.rstrip("/"), "from --hub"))
    cands.append(("http://%s2:8081" % SLOT_PREFIX, "the documented default"))
    gw = _default_gateway()
    if gw and not any(gw in u for u, _h in cands):
        cands.append(("http://%s:8081" % gw, "this computer's gateway"))
    tried = []
    for url, how in cands:
        st = _u.hub_status(url, timeout=1.5)
        if isinstance(st, dict) and "__error__" not in st:
            return {"url": url, "how": how, "status": st, "tried": tried}
        tried.append((url, how))
    return {"url": None, "how": None, "status": None, "tried": tried}


def hub_identity(root, t):
    """Judge "am I on the hub, in the right clone?" from DEAD-STATE evidence
    only, over Transport `t` (mockable):
      * the installed cabinet-g2s unit's WorkingDirectory points into `root`
        (readable even when the unit is dead) — the right-clone test;
      * hub unit files present (cabinet-g2s + cabinet-dhcp);
      * 192.168.50.2 on a NIC.
    Returns {"units": bool, "wd": str, "wd_matches": bool, "slot_nic": bool,
    "verdict": "hub"|"wrong-clone"|"not-hub"}."""
    rc, out = t.run("grep -h '^WorkingDirectory=' %s/cabinet-g2s.service "
                    "2>/dev/null || true" % UNIT_DIR, quiet=True)
    wd = ""
    for line in out.splitlines():
        if line.startswith("WorkingDirectory="):
            wd = line.split("=", 1)[1].strip()
    rc, out = t.run("ls %s/cabinet-g2s.service %s/cabinet-dhcp.service "
                    "2>/dev/null | wc -l" % (UNIT_DIR, UNIT_DIR), quiet=True)
    units = out.strip().endswith("2")
    rc, out = t.run("ip -4 addr 2>/dev/null || true", quiet=True)
    slot_nic = "192.168.50.2/" in out
    wd_matches = bool(wd) and (
        os.path.realpath(os.path.dirname(wd)) == os.path.realpath(root))
    if wd_matches:
        verdict = "hub"
    elif units or slot_nic:
        # A hub box, but the unit points at a DIFFERENT tree — running repairs
        # from here would diagnose one clone and mutate another.
        verdict = "wrong-clone"
    else:
        verdict = "not-hub"
    return {"units": units, "wd": wd, "wd_matches": wd_matches,
            "slot_nic": slot_nic, "verdict": verdict}


def lock_holder(root):
    """Who holds .cabinet-update.lock RIGHT NOW, or None. The FILE lingering
    is normal (a flock dies with its process); only a held flock matters, so
    probe with a non-blocking shared lock instead of trusting existence."""
    p = os.path.join(root, LOCK_NAME)
    if not os.path.exists(p):
        return None
    import fcntl
    try:
        fh = open(p, "r")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None
    except OSError:
        label = fh.read(120).strip()
        # update.py writes no label into the lock file; an empty one is it.
        return label or "update.py (unlabeled lock)"
    finally:
        fh.close()


_RUN_LOCK = None    # held for process life once a mutating verb starts


def take_lock(root):
    """ONE WRITER on this hub, shared with update.py: mutating verbs take the
    same .cabinet-update.lock (exclusive flock, labeled so the other tool's
    banner can name us). Refuses politely when someone else holds it. Reads
    are lock-free by design. Returns the open handle (keep it alive), or
    None on refusal."""
    global _RUN_LOCK
    import fcntl
    fh = open(os.path.join(root, LOCK_NAME), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        say("❌ another writer is active on this hub (%s) — wait for it to "
            "finish, then rerun. Nothing was changed."
            % (lock_holder(root) or "unknown"))
        return None
    fh.seek(0)
    fh.truncate()
    fh.write("cabinetconfig pid=%d started=%s\n"
             % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
    fh.flush()
    _RUN_LOCK = fh
    return fh


def release_lock(fh):
    """Drop the label so a later lock_holder() probe reads clean."""
    global _RUN_LOCK
    try:
        fh.seek(0)
        fh.truncate()
        fh.close()          # closing releases the flock
    except OSError:
        pass
    _RUN_LOCK = None


# ---------------------------------------------------------------------------
# DISCOVERY + FLEET — F1/F2/F3 (the D3 sweep lives after the doctors)
# ---------------------------------------------------------------------------

SLOT_PREFIX = "192.168.50."     # the wired slot segment; the hub is .2


def _read_leases(root):
    """G2S/data/dhcp_leases.json, READ-ONLY and absent-tolerant — the DHCP
    server's own never-fatal loader posture: a missing / corrupt /
    wrong-shaped file is an empty list, never a crash (this tool must work
    best on the nights things are broken). On-disk shape is {mac: {ip,
    hostname, ...}}; returns [{ip, mac, hostname}]."""
    try:
        with open(os.path.join(root, LEASES_REL)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    if isinstance(raw, dict):
        for mac, l in sorted(raw.items()):
            if isinstance(l, dict) and l.get("ip"):
                out.append({"ip": str(l["ip"]), "mac": mac,
                            "hostname": str(l.get("hostname") or "")})
    return out


def _slot_nic(t):
    """The NIC carrying the hub's slot address (192.168.50.2), from
    `ip -4 -o addr` (one line per address, so no stateful parse). Not found
    (dev box, sparse mock) → None; the neigh probe then falls back to the
    unscoped table filtered by prefix."""
    rc, out = t.run("ip -4 -o addr 2>/dev/null || true", quiet=True)
    for line in out.splitlines():
        toks = line.split()
        # "2: eth0    inet 192.168.50.2/24 brd ..." — [1]=nic, [3]=addr
        if len(toks) >= 4 and toks[2] == "inet" \
                and toks[3].startswith(SLOT_PREFIX + "2/"):
            return toks[1]
    return None


def _neigh(t, nic):
    """`ip neigh` rows on the slot segment: [{ip, mac, state}]. With the
    slot NIC known the kernel scopes for us; otherwise the unscoped table is
    filtered to the slot prefix — on a house-LAN dev box that is simply
    empty, which is the honest answer."""
    if nic:
        cmd = "ip neigh show dev %s 2>/dev/null || true" % nic
    else:
        cmd = "ip neigh 2>/dev/null || true"
    rc, out = t.run(cmd, quiet=True)
    rows = []
    for line in out.splitlines():
        toks = line.split()
        if not toks or not toks[0].startswith(SLOT_PREFIX):
            continue
        mac = ""
        if "lladdr" in toks and toks.index("lladdr") + 1 < len(toks):
            mac = toks[toks.index("lladdr") + 1]
        state = toks[-1] if toks[-1].isupper() else ""
        rows.append({"ip": toks[0], "mac": mac, "state": state})
    return rows


def _arp_live(neigh):
    """The wire-live set. FAILED/INCOMPLETE rows can linger (sometimes even
    with an old lladdr) but they mean the wire did NOT answer — only the
    answering states count."""
    return {n["ip"] for n in neigh
            if n["mac"] and n["state"] not in ("FAILED", "INCOMPLETE")}


def _egm_peer(ent):
    """A G2S machine's IP, parsed from its own egmLocation — the callback
    URL the EGM told us to POST to, so it is the machine's own claim."""
    m = re.search(r"//(\d+\.\d+\.\d+\.\d+)",
                  str((ent or {}).get("egmLocation") or ""))
    return m.group(1) if m else ""


def _probe_cmd(peer, user):
    """The BatchMode reachability probe as a plain shell string, so it rides
    ANY transport — MockTransport cans it, and the gate's journal can assert
    exactly what discovery ran. `true` is the whole payload: reachable means
    "sshd answered and the hub key is authorized", nothing more.
    The identity is shell-QUOTED: `user` is a satellite's own self-reported
    sshUser and `peer` comes from leases/ARP — data, never trusted as shell
    (this string runs ON THE HUB; a hostile report must stay one ssh
    argument). Quoting is a no-op for well-formed names, so the journal the
    gate asserts over is byte-identical on a clean floor."""
    return ("ssh -i %s -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=8 %s true"
            % (shlex.quote(DEFAULT_KEY),
               shlex.quote("%s@%s" % (user, peer))))


def discover_fleet(status, root, local=None):
    """Union discovery — who is (or should be) on the floor. READ-ONLY:
    never writes anything, anywhere.

    Args: `status` = api_status() result (None or an __error__ dict ⇒
    DEGRADED: the status leg is dropped, every other leg still answers);
    `root` = repo root; `local` = Transport for hub-side commands (default
    LocalTransport; the gate passes a MockTransport).
    Sources: /api/status via update.py's find_satellites + find_companions
    (their shapes kept; doctor/table extras ride along) ∪ the machines' own
    status entries ∪ G2S/data/dhcp_leases.json (read-only) ∪ `ip neigh` on
    the slot NIC ∪ BatchMode ssh probes.
    Probing is evidence, not inventory: full mode probes only what already
    looks dark (a silent SMIB, a stale companion, a lease nothing claims);
    degraded mode probes every lease/ARP candidate, because ssh IS the
    discovery then. The hub itself is never a candidate.
    Returns {"degraded":   bool,
             "sats":       [{smibId, peer, online, user, silent,
                             keys: [{key, online, stale, reportAgeSec}]}],
             "companions": [{companionId, peer, fresh, readerOk, label}],
             "machines":   [{egmId, name, peer, commsState, offline,
                             joining}],
             "leases":     [{ip, mac, hostname}],
             "neigh":      [{ip, mac, state}],
             "probes":     {peer: bool}}    # candidates only, see above"""
    t = local or LocalTransport()
    degraded = not isinstance(status, dict) or "__error__" in status
    st = {} if degraded else status

    # The status legs — update.py's own discovery, so the fleet is never
    # hardcoded here either.
    sats = _u.find_satellites(st)
    comps = _u.find_companions(st)

    # Per-tile truth rides along on each SMIB row: find_satellites dedupes
    # by peer (one row per Pi), but health is per smibId/address KEY — a
    # multidrop Pi can have one live machine and one dark one. `silent` =
    # every key stale = the Pi (or its report leg) is gone, not the machine.
    by_peer = {}
    for key, ent in (st.get("sas") or {}).items():
        e = ent or {}
        by_peer.setdefault(str(e.get("peer") or "").strip(), []).append(
            {"key": key, "online": bool(e.get("online")),
             "stale": bool(e.get("stale")),
             "reportAgeSec": e.get("reportAgeSec")})
    for s in sats:
        s["keys"] = by_peer.get(s["peer"], [])
        s["silent"] = bool(s["keys"]) and all(k["stale"] for k in s["keys"])
    for c in comps:
        e = (st.get("companions") or {}).get(c["companionId"]) or {}
        c["readerOk"] = e.get("readerOk")
        c["label"] = e.get("label") or ""

    # Machines: every top-level status entry with a commsState (update.py's
    # own machine test). Registered machines seed as offline tiles and never
    # vanish, so a powered-off cabinet is a row here by design.
    names = st.get("names") or {}
    machines = []
    for egm, ent in sorted(st.items()):
        if isinstance(ent, dict) and "commsState" in ent:
            machines.append({"egmId": egm, "name": names.get(egm) or egm,
                             "peer": _egm_peer(ent),
                             "commsState": ent.get("commsState"),
                             "offline": bool(ent.get("offline")),
                             "joining": bool(ent.get("joining"))})

    leases = _read_leases(root)
    neigh = _neigh(t, _slot_nic(t))

    claimed = ({m["peer"] for m in machines} | {s["peer"] for s in sats}
               | {c["peer"] for c in comps})
    if degraded:
        cands = {l["ip"] for l in leases} | _arp_live(neigh)
    else:
        cands = ({s["peer"] for s in sats if s["silent"]}
                 | {c["peer"] for c in comps if not c["fresh"]}
                 | ({l["ip"] for l in leases} - claimed))
    cands -= {"", SLOT_PREFIX + "2"}
    users = {s["peer"]: _u.sat_user(s) for s in sats}
    probes = {}
    for peer in sorted(cands):          # sorted → a deterministic journal
        rc, _out = t.run(_probe_cmd(peer, users.get(peer, _u.SAT_USER)),
                         check=False, timeout=15, quiet=True)
        probes[peer] = (rc == 0)

    return {"degraded": degraded, "sats": sats, "companions": comps,
            "machines": machines, "leases": leases, "neigh": neigh,
            "probes": probes}


# Broken sorts first: ❌ then ⚠️ then • (informational) then ✅.
_FLAG_RANK = {"❌": 0, "⚠️": 1, "•": 2, "✅": 3}


def _print_rows(rows):
    """The one aligned-table renderer (F1 + the sweep summary): flag first,
    then name/kind/peer padded, detail free-form at the end."""
    rows.sort(key=lambda r: (_FLAG_RANK.get(r["flag"], 2),
                             r["kind"], r["name"]))
    w_n = max(len(r["name"]) for r in rows)
    w_k = max(len(r["kind"]) for r in rows)
    w_p = max(len(r["peer"]) for r in rows)
    col = 3 + 2 + 1 + w_n + 2 + w_k + 2 + w_p + 2
    for r in rows:
        # The detail is free-form (a lastError, a codes list) and used to run
        # straight past 80 on the one table titled "Every device, in full".
        parts = _wrapped(r["detail"], col)
        say("   %s %-*s  %-*s  %-*s  %s" % (_flagcell(r["flag"]),
                                            w_n, r["name"], w_k, r["kind"],
                                            w_p, r["peer"], parts[0]))
        for p in parts[1:]:
            say(" " * col + p)


def fleet_table(fleet):
    """F1 [ro] — the floor table: one aligned row per machine / SMIB Pi /
    companion (+ leases nothing claims), broken rows sorted first. Degraded
    mode renders the lease × ARP × probe fallback table instead, clearly
    marked. Args: `fleet` = discover_fleet() dict. Prints through say();
    returns the row list ([{name, kind, peer, flag, detail}]) for --json."""
    step("Floor table")
    rows = []
    if fleet["degraded"]:
        say("   (status dark — lease × ARP × ssh evidence only; machine/"
            "SMIB/companion truth needs the hub API back)")
        live = _arp_live(fleet["neigh"])
        by_ip = {n["ip"]: n for n in fleet["neigh"]}
        lease = {l["ip"]: l for l in fleet["leases"]}
        for ip in sorted(set(lease) | set(by_ip)):
            if fleet["probes"].get(ip):
                flag, why = "✅", "ssh answers"
            elif ip in live:
                flag, why = "⚠️", ("on the wire, ssh does not answer "
                                   "(an EGM, or no hub key there)")
            else:
                flag, why = "❌", "leased but dark on the wire"
            rows.append({"name": lease.get(ip, {}).get("hostname") or "?",
                         "kind": "lease" if ip in lease else "wire",
                         "peer": ip, "flag": flag,
                         "detail": "%s · %s"
                         % ((by_ip.get(ip) or {}).get("state") or "no ARP",
                            why)})
    else:
        for m in fleet["machines"]:
            if m["commsState"] == "onLine":
                flag, why = "✅", "onLine"
            elif m["joining"]:
                flag, why = "⚠️", "joining — reached the hub, not joined yet"
            else:
                flag, why = "❌", "offline"
            if m["name"] != m["egmId"]:
                why += " · %s" % m["egmId"]
            rows.append({"name": m["name"], "kind": "egm",
                         "peer": m["peer"] or "?", "flag": flag,
                         "detail": why})
        for s in fleet["sats"]:
            keys = ",".join(k["key"] for k in s["keys"])
            if s["silent"]:
                age = min((k["reportAgeSec"] or 0) for k in s["keys"])
                flag, why = "❌", ("silent — no report for %ds (Pi down, or "
                                  "its report leg)" % age)
            elif any(k["online"] for k in s["keys"]):
                flag, why = "✅", "reporting, machine leg up"
            else:
                flag, why = "⚠️", ("reporting, machine leg dark (EGM off, "
                                   "wiring, or hub-parked — see the doctor)")
            rows.append({"name": s["smibId"], "kind": "smib",
                         "peer": s["peer"], "flag": flag,
                         "detail": "%s · %s" % (why, keys)})
        for c in fleet["companions"]:
            if not c["fresh"]:
                flag, why = "❌", "stale — daemon not reporting"
            elif c["readerOk"] is False:
                flag, why = "⚠️", ("reporting, readerOk=false (and readerOk "
                                   "can lie — see the doctor)")
            else:
                flag, why = "✅", "reporting, reader claims OK"
            if c["label"]:
                why += " · %s" % c["label"]
            rows.append({"name": c["companionId"], "kind": "companion",
                         "peer": c["peer"], "flag": flag, "detail": why})
        claimed = ({m["peer"] for m in fleet["machines"]}
                   | {s["peer"] for s in fleet["sats"]}
                   | {c["peer"] for c in fleet["companions"]})
        for l in fleet["leases"]:
            if l["ip"] in claimed:
                continue
            rows.append({"name": l["hostname"] or l["mac"], "kind": "lease",
                         "peer": l["ip"], "flag": "•",
                         "detail": "leased, nothing reports from it (an EGM "
                                   "that never joined, or a dark Pi)"
                         + (" · ssh answers" if fleet["probes"].get(l["ip"])
                            else "")})
    if not rows:
        say("   (nothing found — no status rows, no leases, no ARP "
            "neighbours on %sx)" % SLOT_PREFIX)
        return rows
    _print_rows(rows)
    return rows


def three_set_diff(fleet):
    """F2 [ro] — the signature move: has-a-DHCP-lease × ARP-live ×
    reporting-to-hub, i.e. "who is lying to whom". "Reporting" means the hub
    hears it NOW (a joined machine, a fresh SAS key, a fresh companion) —
    a stale tile is the hub REMEMBERING, not hearing, and folding those in
    would hide exactly the broken-report-leg case this diff exists to catch
    (F1 owns the remembered-tile story). Membership:
      healthy       = reporting ∧ wire-live      (hub and wire agree)
      reportingOnly = reporting ∧ ¬wire-live     (heard now yet no live ARP
                      entry — usually just an aged-out ARP row)
      arpOnly       = wire-live ∧ ¬reporting     (on the wire, silent to the
                      hub — an unjoined EGM, a foreign box, or a Pi whose
                      report leg is down)
      leaseOnly     = leased ∧ ¬wire ∧ ¬reporting (took a lease once, dark
                      now — a powered-off cabinet or Pi)
      cloned        = same smibId from TWO peers (a cloned SD card: baked
                      --smib-id or legacy hostname on a second board). A
                      same-KEY clone flaps the one tile between peers and is
                      invisible in a single snapshot — that one shows as a
                      flapping peer in the UI, not here.
    Degraded mode: the reporting set is dark, so every wire-live box lands
    in arpOnly — the narrative says so instead of pretending health. Args:
    discover_fleet() dict; prints the narrative; returns the five lists
    (+ degraded)."""
    lease_ips = {l["ip"] for l in fleet["leases"]}
    wire = _arp_live(fleet["neigh"])
    rep = ({m["peer"] for m in fleet["machines"]
            if m["commsState"] == "onLine" or m["joining"]}
           | {s["peer"] for s in fleet["sats"]
              if any(not k["stale"] for k in s["keys"])}
           | {c["peer"] for c in fleet["companions"] if c["fresh"]}) - {""}
    host = {l["ip"]: l["hostname"] for l in fleet["leases"] if l["hostname"]}

    def _fmt(ips):
        return ", ".join(ip + (" (%s)" % host[ip] if ip in host else "")
                         for ip in sorted(ips)) or "—"

    by_id = {}
    for s in fleet["sats"]:
        by_id.setdefault(s["smibId"], set()).add(s["peer"])
    cloned = [{"id": i, "peers": sorted(p)}
              for i, p in sorted(by_id.items()) if len(p) > 1]

    # WIRE-DARK: off the hub there is no G2S/data/ (a sacred sync exclude)
    # and no slot NIC, so BOTH the lease and ARP legs are empty sets.
    # Differencing against ∅ would file every healthy, reporting box under
    # "no live ARP entry" — a perfectly healthy floor rendered as five
    # warnings. The hub HEARING boxes with no wire evidence at all is the
    # fingerprint, and it can only mean we are not the hub.
    wire_dark = bool(rep) and not lease_ips and not wire
    step("Lease × ARP × reporting")
    if fleet["degraded"]:
        say("   ⚠️  status dark — the reporting set is empty, so every "
            "wire-live box shows below as 'silent to the hub'. Rerun when "
            "the hub API is back for the real diff.")
    def _set_row(label, ips):
        # An address list is as long as the floor is big; unwrapped these ran
        # past 80 the moment a collector had more than three boxes.
        parts = _wrapped(_fmt(ips), 43)
        say("   %s %s" % (label, parts[0]))
        for p in parts[1:]:
            say(" " * 43 + p)

    if wire_dark:
        say("   Address leases and who-is-on-the-wire are things only the "
            "hub can")
        say("   see — from here you get everything the hub hears.")
        _set_row("• heard by the hub right now:          ", rep)
    else:
        _set_row("✅ healthy — hub and wire agree:       ", rep & wire)
        _set_row("⚠️ heard by the hub, no live ARP entry:", rep - wire)
        _set_row("⚠️ wire-live, but silent to the hub:   ", wire - rep)
        _set_row("• leased once, dark now:               ",
                 lease_ips - wire - rep)
    for cl in cloned:
        for ln in _indent("❌ CLONED SD CARD — smibId %r reports from %s: the "
                          "same identity on two boards flaps its tile and "
                          "looks like network trouble. Re-image one board (or "
                          "pin a registry name on it) before chasing the "
                          "network." % (cl["id"], ", ".join(cl["peers"])), 3):
            say(ln)
    return {"healthy": sorted(rep & wire),
            "reportingOnly": sorted(rep - wire),
            "arpOnly": sorted(wire - rep),
            "leaseOnly": sorted(lease_ips - wire - rep),
            "cloned": cloned, "degraded": fleet["degraded"],
            "wireDark": wire_dark}


def _git(root, *args):
    """A local git fact, quietly; rc != 0 covers both "not a clone" and "no
    git on this box" (a copied install has neither, and F3 must not care)."""
    try:
        return _u.run(["git", "-C", root] + list(args),
                      check=False, quiet=True, timeout=30)
    except OSError:
        return 127, ""


def _age_str(sec):
    """Humanize an age the way the Updates card talks: 42s / 17m / 3h / 2d."""
    sec = int(sec)
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= div:
            return "%d%s" % (sec // div, unit)
    return "%ds" % sec


def tree_facts(root, title=None):
    """F3 [ro] — LOCAL facts only, deliberately no network (the product
    never phones home; a remote check is its own explicit action with a
    timeout, and it is not this one): HEAD short-hash + subject, branch,
    dirty-tree count + diffstat tail, update-transcript age
    (G2S/data/update_last.log mtime, read-only), hub key state (~/.ssh/smib
    + .pub), adopt-pending marker. Args: repo root, and an optional title —
    off the hub "hub/tree facts" would be a lie, and the caller knows where
    it is standing. Prints; returns the fact dict."""
    step(title or "Hub/tree facts (local only)")

    def _row(text):
        for ln in _indent(text, 3):
            say(ln)

    facts = {"root": root}
    rc, out = _git(root, "log", "-1", "--format=%h %s")
    if rc == 0 and out.strip():
        facts["head"] = out.strip().splitlines()[0]
        _rc, br = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        facts["branch"] = br.strip() if _rc == 0 else "?"
        # A commit subject, a diffstat and a key path are all arbitrary
        # length; unwrapped, the HEAD line alone ran to 103 columns on the
        # front door — the one screen a stranger reads first.
        _row("✅ HEAD %s (branch %s)" % (facts["head"], facts["branch"]))
        _rc, porc = _git(root, "status", "--porcelain")
        dirty = [ln for ln in porc.splitlines() if ln.strip()]
        facts["dirty"] = len(dirty)
        if dirty:
            # Diffstat tail, not a judgment: hot dev edits may be deliberate
            # (the floor is the dev floor); update.py owns the refusal table.
            _rc, stat = _git(root, "diff", "--stat")
            tail = stat.strip().splitlines()[-1].strip() if stat.strip() else ""
            _row("⚠️ dirty tree — %d path(s)%s" %
                 (len(dirty), " · %s" % tail if tail else ""))
        else:
            _row("✅ tree clean")
    else:
        facts["head"], facts["dirty"] = None, None
        _row("⚠️ not a git clone — update.py's adopt turns this install into "
             "one, in place")
    log_p = os.path.join(root, "G2S/data/update_last.log")
    try:
        facts["updateLogAgeSec"] = int(time.time() - os.path.getmtime(log_p))
        _row("✅ last update transcript: %s old (G2S/data/update_last.log)"
             % _age_str(facts["updateLogAgeSec"]))
    except OSError:
        facts["updateLogAgeSec"] = None
        _row("• no update transcript yet — update.py has not run here")
    facts["hubKey"] = os.path.exists(DEFAULT_KEY)
    facts["hubKeyPub"] = os.path.exists(DEFAULT_KEY + ".pub")
    if facts["hubKey"]:
        _row("✅ hub key present (%s%s)"
             % (DEFAULT_KEY, " + .pub" if facts["hubKeyPub"]
                else " — but its .pub is MISSING"))
    else:
        _row("❌ no %s — satellite ssh (probes, doctors, update.py) has no "
             "key" % DEFAULT_KEY)
    facts["adoptPending"] = os.path.exists(os.path.join(root, _u.ADOPT_MARKER))
    if facts["adoptPending"]:
        _row("⚠️ %s present — the last adoption never completed an update"
             % _u.ADOPT_MARKER)
    return facts


# ---------------------------------------------------------------------------
# DOCTORS — C1 / S1 / H1 (codes distilled from the first tester's real
# stuck floor: a month of "no SAS polling" that was three separate setup
# states wearing one symptom)
# ---------------------------------------------------------------------------
# Shared shape: every doctor returns
#   {"target", "kind", "codes": [str], "checks": [{probe, ok, detail}],
#    "findings": [{code, severity, evidence, hint}], "verdict"}
# `checks` is the probe table (ok True/False, None = informational);
# `findings` carry the CLASSIFICATION — the diagnosis layer IS the product,
# and the gate asserts on code strings, never on prose. Severity: fatal ❌ /
# warn ⚠️ / info •. Doctors ask, never fix: zero mutating commands, ever
# (the gate asserts that over MockTransport.journal too).

_SEV_FLAG = {"fatal": "❌", "warn": "⚠️", "info": "•"}

# THE banner (C1.1/H1.1) — this one sentence, surfaced on the
# right night, would have killed the first tester's month of "no SAS polling"
# at the source: he believed the RFID companion polls SAS. VERBATIM and on
# one line, so a support-bundle grep always finds it whole.
_C11_BANNER = ("COMPANION TIER: RFID only — this Pi does NOT poll SAS; SAS "
               "needs deploy/smib_setup.sh on a Pi wired to the machine.")


def _q(t, cmd):
    """Quiet probe over any transport. An unanswerable probe (dead box,
    un-canned mock) is EMPTY output, never fake evidence — every parser
    below treats "" as "could not look", not "looked and saw nothing"."""
    try:
        rc, out = t.run(cmd, check=False, timeout=30, quiet=True)
    except Exception:               # a wedged ssh must not kill the doctor
        return ""
    return out if rc == 0 else ""


def _q_rc(t, cmd):
    """Like _q, but the caller gets the rc too — for the parsers where
    "looked and saw NOTHING" is itself a verdict (SILENT-MISMATCH, the
    dmesg clean bill): those must never mint evidence from a probe that
    could not look, and only the rc tells the two apart."""
    try:
        return t.run(cmd, check=False, timeout=30, quiet=True)
    except Exception:               # same wedged-ssh posture as _q
        return 255, ""


def _doc_unreachable(doc):
    """The short-circuit for a box that answered NOTHING: name it honestly
    instead of letting every failed probe read as absence. UNREACHABLE is
    not NEVER-PROVISIONED — the wrong verdict here sends a collector
    re-provisioning a Pi that is merely powered off or unkeyed."""
    _check(doc, "reachability", False,
           "no answer over ssh (BatchMode, hub key)")
    _finding(doc, "UNREACHABLE", "fatal",
             "not one probe got through — a dead box and a bare box look "
             "identical from here, so nothing below was actually examined",
             "power, wire, lease first; then the hub key: ssh-copy-id -i "
             "~/.ssh/smib.pub <user>@<pi> from the hub (the C8/H14 "
             "authorize verb is a later phase).")
    return _doc_close(doc)


def _doc_new(target, kind):
    return {"target": target or "?", "kind": kind, "codes": [],
            "checks": [], "findings": [], "verdict": None}


def _check(doc, probe, ok, detail):
    """One probe-table row; ok True/False = judged, None = informational."""
    doc["checks"].append({"probe": probe, "ok": ok, "detail": detail})


def _finding(doc, code, severity, evidence, hint):
    """One classification. First sighting wins — S1.4 and S1.5 can both see
    PORT-MISSING, and the second copy would only bury the verdict."""
    if code in doc["codes"]:
        return
    doc["codes"].append(code)
    doc["findings"].append({"code": code, "severity": severity,
                            "evidence": evidence, "hint": hint})


_STATE_CODES = ("NEVER-PROVISIONED", "UNIT-OFF", "HUB-PARKED")


def _doc_close(doc, states=_STATE_CODES):
    """Verdict + render. The three SAS-leg STATES outrank every fatal —
    naming WHICH look-alike broken state this is comes first, because a
    never-provisioned Pi fails every downstream probe too and none of those
    probes are the answer. `states=()` for the hub: one tile parked on
    purpose must never become the whole hub's verdict.

    RENDER TIME ONLY: the plan codes that ORGANISE this source file (S1.4,
    C2, H14 …) come out of every printed line here — probe labels re-aligned,
    evidence and hints run through _plain. The strings inside `doc` stay
    byte-identical, so --json, the gate's probe-command transcript and every
    classification code are untouched. Menu screens already stripped them;
    this closes the same leak on the one-liner path, which has no screen."""
    fatal = [f["code"] for f in doc["findings"] if f["severity"] == "fatal"]
    doc["verdict"] = next((s for s in states if s in doc["codes"]),
                          fatal[0] if fatal else "OK")
    if doc["checks"]:
        names = [_PLAN_PREFIX.sub("", c["probe"]) for c in doc["checks"]]
        w = max(len(n) for n in names)
        col = 3 + 2 + 1 + w + 2
        for c, n in zip(doc["checks"], names):
            flag = "✅" if c["ok"] else ("•" if c["ok"] is None else "❌")
            parts = _wrapped(_plain(c["detail"]), col)
            say("   %s %-*s  %s" % (_flagcell(flag), w, n, parts[0]))
            for p in parts[1:]:
                say(" " * col + p)
    if doc["findings"]:
        say("")
    for f in doc["findings"]:
        for ln in _indent("%s %s — %s" % (_SEV_FLAG[f["severity"]], f["code"],
                                          _plain(f["evidence"])), 3):
            say(ln)
        if f.get("hint"):
            for ln in _indent("↳ %s" % _plain(f["hint"]), 6):
                say(ln)
    say("")
    say("   %s verdict: %s"
        % ("✅" if doc["verdict"] == "OK"
           else "⚠️" if doc["verdict"] == "HUB-PARKED" else "❌",
           doc["verdict"]))
    return doc


def _unit_facts(cat_out):
    """The unit's literal truths from `systemctl cat`: the LAST
    ExecStart=/User=/WorkingDirectory= wins (drop-ins override)."""
    u = {"exec": "", "user": "", "wd": ""}
    for line in cat_out.splitlines():
        line = line.strip()
        for key, pfx in (("exec", "ExecStart="), ("user", "User="),
                         ("wd", "WorkingDirectory=")):
            if line.startswith(pfx) and len(line) > len(pfx):
                u[key] = line[len(pfx):].strip()
    return u


def _flag_val(toks, flag):
    """`--flag value` from an ExecStart token list, or None."""
    for i, tk in enumerate(toks):
        if tk == flag and i + 1 < len(toks):
            return toks[i + 1]
    return None


def _sas_port(toks):
    """The serial port from the unit's OWN ExecStart (install facts are
    ALWAYS read from the target's unit, never guessed): the first non-flag
    token after sas_host.py; the shipped default when unparseable."""
    seen = False
    for tk in toks:
        if seen and not tk.startswith("-"):
            return tk
        if tk.endswith("sas_host.py"):
            seen = True
    return "/dev/ttyAMA0"


def _sas_entries(st, name, peer):
    """This leg's status tiles as [(key, entry)] — matched by smibId, key
    head, or peer, per smibId/address KEY (a multidrop Pi can carry one
    live machine and one dark one). Shared by the smib doctor, the S2
    verify, and the finish pass, so the three can never drift apart on
    what "this leg" means."""
    out = []
    for key, e in sorted((st.get("sas") or {}).items()):
        e = e or {}
        if ((name and (e.get("smibId") == name
                       or key.split("/")[0] == name))
                or (peer and e.get("peer") == peer)):
            out.append((key, e))
    return out


def _comp_entry(st, name, peer):
    """This reader's status row as (companionId, entry) — matched by id
    first, then by peer; (None, None) when the hub has never heard of it.
    Shared by the companion doctor, the C2 verify, and the finish pass."""
    comps = st.get("companions") or {}
    if name and name in comps:
        return name, comps[name] or {}
    for cid, e in sorted(comps.items()):
        if peer and (e or {}).get("peer") == peer:
            return cid, e or {}
    return None, None


def _probe_health(t, doc):
    """SD health + clock, on EVERY doctor (plan ⚑). "sed succeeded, reboot
    bricked" is how headless Pis die — the write verbs refuse on a sick
    card, and this is where the sickness gets NAMED. Codes: SD-UNHEALTHY
    (ro rootfs fatal / dmesg mmc errors warn), DISK-FULL, CLOCK-SKEW."""
    out = _q(t, "findmnt -n -o OPTIONS / 2>/dev/null || true").strip()
    opts = out.splitlines()[0].split(",") if out else []
    if not opts:
        _check(doc, "sd rootfs", None, "findmnt did not answer")
    elif "ro" in opts:
        _check(doc, "sd rootfs", False, "mounted READ-ONLY")
        _finding(doc, "SD-UNHEALTHY", "fatal",
                 "rootfs is mounted read-only — the kernel does that when "
                 "the card is failing",
                 "image a replacement card before ANY repair: a config edit "
                 "that cannot persist plus a reboot = SD-card surgery on a "
                 "headless Pi. Write verbs refuse in this state.")
    else:
        _check(doc, "sd rootfs", True, "mounted rw")
    # The clean bill needs dmesg to have ANSWERED: a locked-down or
    # unanswerable dmesg (kernel.dmesg_restrict, a sparse fixture) must
    # stay "could not look" — never "✅ no mmc errors" minted from silence.
    rc_d, dout = _q_rc(t, "if dmesg >/dev/null 2>&1; then dmesg 2>/dev/null"
                          " | grep -iE 'mmc[0-9]+.*(error|timeout|fail)' "
                          "| tail -3; else echo __dmesg-unreadable__; fi")
    dout = dout.strip()
    if rc_d != 0 or dout == "__dmesg-unreadable__":
        _check(doc, "sd dmesg", None, "dmesg not readable here")
    elif dout:
        _check(doc, "sd dmesg", False, dout.splitlines()[-1][:100])
        _finding(doc, "SD-UNHEALTHY", "warn",
                 "dmesg shows mmc errors — the SD card is going",
                 "image a replacement before it stops mounting rw; every "
                 "repair on a dying card is borrowed time.")
    else:
        _check(doc, "sd dmesg", True, "no mmc errors in dmesg")
    toks = _q(t, "df -P / 2>/dev/null | tail -1 || true").split()
    if len(toks) >= 5 and toks[4].endswith("%") and toks[4][:-1].isdigit():
        pct = int(toks[4][:-1])
        _check(doc, "disk /", pct < 90, "%d%% used" % pct)
        if pct >= 90:
            _finding(doc, "DISK-FULL", "fatal" if pct >= 98 else "warn",
                     "rootfs is %d%% full" % pct,
                     "a full disk corrupts db writes and journals — clear "
                     "space (old logs, __pycache__) before repairing "
                     "anything else.")
    else:
        _check(doc, "disk /", None, "df did not answer")
    sync = None
    for line in _q(t, "timedatectl show 2>/dev/null || true").splitlines():
        if line.startswith("NTPSynchronized="):
            sync = line.split("=", 1)[1].strip() == "yes"
    if sync is None:
        _check(doc, "clock", None, "timedatectl did not answer")
    elif sync:
        _check(doc, "clock", True, "NTP-synchronized")
    else:
        _check(doc, "clock", False, "not set from the hub")
        _finding(doc, "CLOCK-SKEW", "warn",
                 "this Pi's clock has never been set from the hub",
                 "the hub keeps time for the whole floor. Until this Pi "
                 "picks it up, anything it timestamps — its own log, a "
                 "ticket, a lease — can read out of order.")


def doctor_companion(t, name=None, status=None):
    """C1 [ro] — companion doctor over Transport `t` (name = companionId;
    `status` = api_status() result, fetched when None — the gate passes a
    fixture). An unreachable box short-circuits to UNREACHABLE — every
    probe below fails identically on a dead box, and none of those
    failures may read as absence. Probes → classification codes:
      C1.1 unit census: cabinet-companion present AND cabinet-sas absent →
           COMPANION-TIER (info) with the verbatim banner; no companion
           unit at all → NEVER-PROVISIONED.
      C1.2 PN532 / readerOk+lastError from status (journal fallback when
           the status row is dark): ENXIO → READER-NOT-DETECTED.
      C1.3 systemd NRestarts churn → DAEMON-CRASH-LOOP.
      C1.4 bindings all null → NEVER-BOUND (informational).
    Plus unit state/flags, i2c overlay FORM (canonical bus=11 and the
    legacy no-bus line BOTH count as present), i2c node, 0x24 scan,
    lastTap, hub-key, journal tail, SD health, clock. Returns the shared
    doctor dict; the runner json-dumps it under --json."""
    doc = _doc_new(name or t.name, "companion")
    step("Companion doctor: %s" % doc["target"])
    if not t.reachable():
        return _doc_unreachable(doc)
    if status is None:
        status = api_status()
    st = status if isinstance(status, dict) and "__error__" not in status \
        else {}

    # C1.1 — unit census first: WHICH duty does this Pi actually have?
    comp_unit = "cabinet-companion.service" in _q(
        t, "systemctl list-unit-files cabinet-companion.service "
           "--no-legend --no-pager 2>/dev/null || true")
    sas_here = "cabinet-sas.service" in _q(
        t, "systemctl list-unit-files cabinet-sas.service "
           "--no-legend --no-pager 2>/dev/null || true")
    if comp_unit and not sas_here:
        _check(doc, "C1.1 unit census", True,
               "cabinet-companion present, no cabinet-sas on this Pi")
        _finding(doc, "COMPANION-TIER", "info",
                 "cabinet-companion present AND cabinet-sas absent",
                 _C11_BANNER)
    elif comp_unit:
        _check(doc, "C1.1 unit census", True,
               "cabinet-companion + cabinet-sas (combined SMIB+reader Pi)")
    else:
        _check(doc, "C1.1 unit census", False, "no cabinet-companion unit")
        _finding(doc, "NEVER-PROVISIONED", "fatal",
                 "cabinet-companion.service does not exist here (fine if "
                 "this Pi is a SAS-only SMIB)",
                 "deploy/companion_setup.sh <user>@<pi> from the hub clone "
                 "builds it — that is SETUP, not repair, and it is "
                 "idempotent.")
    legacy = _q(t, "systemctl list-unit-files 'casinonet-companion*' "
                   "--no-legend --no-pager 2>/dev/null || true").strip()
    if legacy:
        _check(doc, "legacy unit", False, legacy.splitlines()[0])
        _finding(doc, "RENAME-CONTENTION", "fatal",
                 "a pre-rename casinonet-companion unit is still installed "
                 "— two daemons fight for the reader",
                 "sudo systemctl disable --now casinonet-companion; a "
                 "companion_setup.sh re-run retires it properly.")
    else:
        _check(doc, "legacy unit", True, "no casinonet-companion residue")

    toks = []
    if comp_unit:
        enab = _q(t, "systemctl is-enabled cabinet-companion "
                     "2>/dev/null || true").strip()
        act = _q(t, "systemctl is-active cabinet-companion "
                    "2>/dev/null || true").strip()
        if enab in ("disabled", "masked") or act != "active":
            _check(doc, "unit state", False,
                   "%s / %s" % (enab or "?", act or "?"))
            _finding(doc, "UNIT-OFF", "fatal",
                     "cabinet-companion exists but is %s/%s"
                     % (enab or "?", act or "?"),
                     "sudo systemctl enable --now cabinet-companion (the C2 "
                     "restart verb covers running-but-wedged).")
        else:
            _check(doc, "unit state", True, "enabled / active")
        # C1.3 — crash loop: the tester's Pi3 looped 4x in 5 minutes and
        # then went dark; NRestarts is systemd's own count of Restart= fires.
        nres = _q(t, "systemctl show -p NRestarts --value cabinet-companion "
                     "2>/dev/null || true").strip()
        if nres.isdigit():
            _check(doc, "restart churn", int(nres) < 3, "NRestarts=%s" % nres)
            if int(nres) >= 3:
                _finding(doc, "DAEMON-CRASH-LOOP", "fatal",
                         "systemd has restarted the daemon %s times" % nres,
                         "read the journal tail below — a crash loop is a "
                         "stack trace, not a reader problem.")
        else:
            _check(doc, "restart churn", None, "NRestarts not readable")
        unit = _unit_facts(_q(t, "systemctl cat cabinet-companion "
                                 "2>/dev/null || true"))
        toks = unit["exec"].split()
        if "--mock" in toks:
            _check(doc, "unit flags", False,
                   "--mock in a FLOOR unit — this daemon FAKES taps (bench "
                   "flag; strip it by re-running companion_setup.sh)")
        elif toks:
            _check(doc, "unit flags", True,
                   " ".join(tk for tk in toks if tk.startswith("--"))
                   or "flagless (zero-config)")
        else:
            _check(doc, "unit flags", None, "ExecStart not readable")

    # I2C plumbing — overlay FORM first. Canonical is the universal software
    # bus (GPIO23/24 -> bus=11); a legacy no-bus line COUNTS AS PRESENT (an
    # exact-match grep here once cost a board a needless reboot —
    # companion_setup.sh 0014d8e), its node just lands on a dynamic number.
    # A missing NODE is overlay/module (or a pending reboot), NEVER wiring
    # or DIP switches — those only decide whether 0x24 ANSWERS.
    cfg = _q(t, 'CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || '
                'CFG=/boot/config.txt; grep -E '
                '"^dtoverlay=i2c-gpio|^dtparam=i2c_arm=on" "$CFG" '
                '2>/dev/null || true')
    gpio = [l for l in cfg.splitlines() if l.startswith("dtoverlay=i2c-gpio")
            and "i2c_gpio_sda=23" in l and "i2c_gpio_scl=24" in l]
    hw = any(l.startswith("dtparam=i2c_arm=on") for l in cfg.splitlines())
    node = None
    if gpio and any("bus=11" in l for l in gpio):
        _check(doc, "i2c overlay", True,
               "software i2c-gpio on GPIO23/24, bus=11 (canonical)")
        node = "/dev/i2c-11"
    elif gpio:
        _check(doc, "i2c overlay", True,
               "legacy i2c-gpio form (no bus=11) — present; the bus number "
               "is dynamic on this board")
    elif hw:
        _check(doc, "i2c overlay", True, "hardware I2C (GPIO2/3)")
        node = "/dev/i2c-1"
    else:
        _check(doc, "i2c overlay", False,
               "no i2c overlay in config.txt — re-run companion_setup.sh "
               "(it IS the idempotent I2C repair)")
    node = _flag_val(toks, "--bus") or node    # the unit's own opinion wins
    nodes = _q(t, "ls /dev/i2c-* 2>/dev/null || true").split()
    if node and node not in nodes:
        _check(doc, "i2c node", False,
               "%s missing (have: %s) — overlay/i2c-dev or a pending "
               "reboot, never wiring/DIP" % (node, " ".join(nodes) or "none"))
    elif nodes:
        _check(doc, "i2c node", True, " ".join(nodes))
    else:
        _check(doc, "i2c node", False, "no /dev/i2c-* nodes at all")
    scan_at = node if node in nodes else (nodes[0] if len(nodes) == 1
                                          else None)
    if scan_at and scan_at.rsplit("-", 1)[-1].isdigit():
        grid = _q(t, "command -v i2cdetect >/dev/null 2>&1 && i2cdetect -y "
                     "%s 0x24 0x24 2>/dev/null || echo no-i2cdetect"
                     % scan_at.rsplit("-", 1)[-1])
        if "no-i2cdetect" in grid:
            _check(doc, "0x24 scan", None,
                   "i2cdetect not installed — judged from readerOk instead")
        elif " 24" in grid:
            _check(doc, "0x24 scan", True,
                   "PN532 answers at 0x24 on %s" % scan_at)
        elif grid.strip():
            _check(doc, "0x24 scan", False, "0x24 silent on %s" % scan_at)
            _finding(doc, "READER-NOT-DETECTED", "fatal",
                     "0x24 does not answer on %s — the node exists, so this "
                     "IS wiring/DIP territory" % scan_at,
                     "set the PN532 DIP switches to I2C and check the "
                     "wiring (Companion/README.md: SDA GPIO23 / SCL "
                     "GPIO24, 3.3V, GND).")
        else:
            _check(doc, "0x24 scan", None, "i2cdetect did not answer")

    # C1.2 / C1.4 — the hub's view: readerOk / lastError / lastTap /
    # bindings ride the status row (readerOk CAN LIE — reference law — so
    # true is only "the reader CLAIMS ok", never more).
    _cid, ent = _comp_entry(st, name, getattr(t, "peer", None))
    if ent:
        err = str(ent.get("lastError") or "")
        r_ok = ent.get("readerOk")
        if "ENXIO" in err or "Errno 6" in err:
            _check(doc, "C1.2 readerOk", False,
                   "readerOk=%s lastError=%s" % (r_ok, err[:60]))
            _finding(doc, "READER-NOT-DETECTED", "fatal",
                     "lastError=%s — the PN532 never enumerated on the bus"
                     % err[:80],
                     "DIP switches to I2C + wiring per Companion/README.md; "
                     "if the i2c NODE above is missing, fix the overlay "
                     "first — a missing node is never wiring.")
        elif r_ok is False:
            _check(doc, "C1.2 readerOk", False,
                   "readerOk=false%s — a wedged PN532; the C2 restart verb "
                   "is the proven reset"
                   % ((" lastError=%s" % err[:60]) if err else ""))
            # A wedge needs a CODE, not just a failed row: the D3 sweep
            # folds every box to verdict+codes, and a down reader — the
            # exact class C2 shipped for — must never render as a clean ✅.
            _finding(doc, "READER-WEDGED", "warn",
                     "readerOk=false with no ENXIO — the PN532 enumerated "
                     "and then stopped answering (the wedge class, not "
                     "wiring)",
                     "`cabinetconfig companion <pi> restart` (C2) is the "
                     "proven reset; still false after that → the wiring/"
                     "i2c rows above are the next suspects.")
        elif r_ok:
            _check(doc, "C1.2 readerOk", True,
                   "reader claims OK (readerOk can lie — judge by a real "
                   "tap)")
        else:
            _check(doc, "C1.2 readerOk", None, "readerOk not reported")
        tap = ent.get("lastTap")
        _check(doc, "lastTap", None,
               ("uid %s at %s" % (tap.get("uid"), tap.get("at"))) if tap
               else "no tap ever recorded")
        binds = ent.get("bindings") or {}
        live = ["%s=%s" % (k, v) for k, v in sorted(binds.items()) if v]
        if live:
            _check(doc, "C1.4 bindings", True, ", ".join(live))
        else:
            _check(doc, "C1.4 bindings", None, "all null")
            _finding(doc, "NEVER-BOUND", "info",
                     "bindings all null — no machine has ever been assigned",
                     "informational: taps land nowhere until the reader is "
                     "bound in the web UI (or auto-bound by co-locating it "
                     "on a SAS SMIB).")
    else:
        _check(doc, "hub status row", None,
               "no status row for this companion (hub API dark, or unknown "
               "companionId) — readerOk/lastTap/bindings unavailable")
        # degraded fallback for C1.2: the journal remembers ENXIO
        j = _q(t, "journalctl -u cabinet-companion -b --no-pager -o cat "
                  "2>/dev/null | grep -iE 'ENXIO|Errno 6' | tail -2 || true"
                  ).strip()
        if j:
            _finding(doc, "READER-NOT-DETECTED", "fatal",
                     "journal: %s" % j.splitlines()[-1][:90],
                     "the PN532 never enumerated — DIP switches to I2C + "
                     "wiring per Companion/README.md; a missing i2c node "
                     "above is overlay, never wiring.")
    if comp_unit:
        tail = _q(t, "journalctl -u cabinet-companion --no-pager -n 5 "
                     "-o cat 2>/dev/null | tail -3 || true").strip()
        _check(doc, "journal tail", None,
               " | ".join(tail.splitlines())[:150] or "(empty)")
    if isinstance(t, SshTransport):
        # Only reached when the entry reachability probe ANSWERED over the
        # hub key — evidence, not an assumption (an unreachable box never
        # gets this far, so this row can never lie about a dark one).
        _check(doc, "hub key", True,
               "authorized (the reachability probe rode it)")
    _probe_health(t, doc)
    return _doc_close(doc)


def doctor_smib(t, name=None, status=None):
    """S1 [ro] — the SAS-leg doctor; FIRST JOB is naming which of the three
    "SAS leg off" states this is (they look identical from the floor and
    need three different fixes). An unreachable box short-circuits to
    UNREACHABLE first — a dead box fails every probe below exactly like a
    bare one, and misreading that as NEVER-PROVISIONED sends someone
    re-running setup on a Pi that is merely dark. Probes over Transport
    `t`; `status` = api_status() result, fetched when None:
      S1.1 list-unit-files cabinet-sas.service → absent = NEVER-PROVISIONED;
           present+disabled/inactive = UNIT-OFF; present+active = continue.
      S1.2 list-unit-files 'casinonet-sas*' → any hit = RENAME-CONTENTION.
      S1.3 systemctl cat → ExecStart parse: no --hub = REPORTING-DISABLED;
           --hub http://127.0.0.1 on a non-hub Pi = SELF-REPORT;
           User=owner / /home/owner paths = UNREWRITTEN-UNIT.
      S1.4 UART five-conditions + venv import → MINI-UART-NO-PARITY /
           CONSOLE-EATS-LINE / GETTY-CONTENTION / PORT-MISSING /
           NO-DIALOUT; Pi5 + port=serial0 = DEBUG-CONNECTOR.
      S1.5 journalctl -u cabinet-sas (GREP, never tail — "hub report failed"
           is edge-logged once) → PORT-MISSING / HUB-UNREACHABLE /
           HUB-PARKED ("SAS DISABLED by hub — parking") / TREE-MISMATCH
           (ImportError) / SILENT-MISMATCH (active + empty journal).
      S1.6 ip addr + ip route + curl -m3 hub /api/hubkey → WRONG-SEGMENT /
           HUB-DOWN / HOME-ROUTER-DERIVED.
      S1.7 bench poll is INTERACTIVE-ONLY — the doctor never stops the
           service; it signposts the documented stop→sas_bench_poll→start
           dance after the verdict (a fresh-tile-but-online=false machine
           leg = MACHINE-LEG-OFF, and the dance settles it in one pass).
    NEVER-PROVISIONED short-circuits S1.3-S1.5: naming the state IS the
    answer, and a Pi with no unit fails every downstream probe for free.
    Returns the shared doctor dict; verdict is NEVER-PROVISIONED | UNIT-OFF
    | HUB-PARKED | OK | <first fatal code>."""
    doc = _doc_new(name or t.name, "smib")
    step("SAS-leg doctor: %s" % doc["target"])
    if not t.reachable():
        return _doc_unreachable(doc)
    if status is None:
        status = api_status()
    st = status if isinstance(status, dict) and "__error__" not in status \
        else {}
    ip_out = _q(t, "ip -4 addr 2>/dev/null || true")
    is_hub_box = "192.168.50.2/" in ip_out   # a co-located hub leg is legit

    # S1.1 — the three-state census. The centerpiece: NEVER-PROVISIONED /
    # UNIT-OFF / HUB-PARKED (the third is named by S1.5's journal grep or
    # the hub tile below — a parked leg looks ON from the unit side).
    present = "cabinet-sas.service" in _q(
        t, "systemctl list-unit-files cabinet-sas.service "
           "--no-legend --no-pager 2>/dev/null || true")
    active = ""
    if not present:
        comp_here = "cabinet-companion.service" in _q(
            t, "systemctl list-unit-files cabinet-companion.service "
               "--no-legend --no-pager 2>/dev/null || true")
        _check(doc, "S1.1 unit census", False,
               "cabinet-sas.service does not exist")
        hint = ("deploy/smib_setup.sh <user>@<pi> from the hub clone builds "
                "UART + venv + tree + unit in one idempotent pass (SETUP, "
                "not repair); then reboot the Pi and do the machine-side "
                "checklist.")
        if comp_here:
            hint = _C11_BANNER + " " + hint
        _finding(doc, "NEVER-PROVISIONED", "fatal",
                 "no cabinet-sas unit — nothing on this Pi has EVER polled "
                 "SAS%s" % (" (it is a companion: RFID tier only)"
                            if comp_here else ""),
                 hint)
    else:
        enab = _q(t, "systemctl is-enabled cabinet-sas "
                     "2>/dev/null || true").strip()
        active = _q(t, "systemctl is-active cabinet-sas "
                       "2>/dev/null || true").strip()
        if enab in ("disabled", "masked") or active != "active":
            _check(doc, "S1.1 unit census", False,
                   "present, %s / %s" % (enab or "?", active or "?"))
            _finding(doc, "UNIT-OFF", "fatal",
                     "cabinet-sas exists but is %s/%s"
                     % (enab or "?", active or "?"),
                     "`cabinetconfig smib <pi> up` is the literal fix: "
                     "fuser port check, systemctl enable --now cabinet-sas, "
                     "tile-live verify. Seconds, no reboot.")
        else:
            _check(doc, "S1.1 unit census", True,
                   "present, enabled / active")

    # S1.2 — pre-rename contention. Checked even with no cabinet-sas: an
    # old casinonet-sas alone IS a pre-rename SMIB, and it still holds the
    # tty, double-polls, and reports under a ghost smibId.
    legacy = _q(t, "systemctl list-unit-files 'casinonet-sas*' "
                   "--no-legend --no-pager 2>/dev/null || true").strip()
    if legacy:
        # Severity turns on whether the old unit can actually WAKE. Installed
        # + enabled/active = two pollers racing one tty (fatal). Installed but
        # disabled AND inactive = a dormant leftover: real housekeeping, but
        # calling it fatal buries a healthy leg's verdict under a file that is
        # doing nothing (live-caught on a fully-polling SMIB 2026-07-29).
        armed = ("disabled" not in legacy.split()
                 or _q(t, "systemctl is-active casinonet-sas "
                          "2>/dev/null || true").strip() == "active")
        _check(doc, "S1.2 rename residue", False,
               legacy.splitlines()[0] + (" — ARMED" if armed else " — dormant"))
        if armed:
            _finding(doc, "RENAME-CONTENTION", "fatal",
                     "a pre-rename casinonet-sas unit is installed AND armed "
                     "— two pollers on one SAS line = framing chaos",
                     "sudo systemctl disable --now casinonet-sas; a "
                     "smib_setup.sh re-run retires it properly.")
        else:
            _finding(doc, "RENAME-RESIDUE", "warn",
                     "a leftover service file from an older CabiNet version "
                     "is still on this Pi. It is switched off and doing "
                     "nothing today",
                     "nothing is wrong right now — but if anything ever "
                     "started it, two copies would fight over the same "
                     "serial port. Setting this Pi up again clears it away.")
    else:
        _check(doc, "S1.2 rename residue", True, "no casinonet-sas residue")

    # S1.3 — the unit's literal knobs (--hub is what makes a tile EXIST)
    unit = {"exec": "", "user": "", "wd": ""}
    toks = []
    port = "/dev/ttyAMA0"
    if present:
        unit = _unit_facts(_q(t, "systemctl cat cabinet-sas "
                                 "2>/dev/null || true"))
        toks = unit["exec"].split()
        port = _sas_port(toks)
        hub_flag = _flag_val(toks, "--hub")
        if unit["user"] == "owner" or "/home/owner" in (unit["exec"]
                                                        + unit["wd"]):
            _check(doc, "S1.3 unit knobs", False,
                   "shipped placeholder user/paths (owner)")
            _finding(doc, "UNREWRITTEN-UNIT", "fatal",
                     "the unit still carries the shipped placeholder "
                     "User=owner / /home/owner paths — it never went "
                     "through smib_setup.sh's rewrite and fails instantly",
                     "re-run deploy/smib_setup.sh (its sed rewrites "
                     "user/home/port/address/hub for the real account).")
        if not hub_flag:
            _check(doc, "S1.3 --hub", False, "no --hub flag")
            _finding(doc, "REPORTING-DISABLED", "fatal",
                     "--hub is omitted — that means NO reporting at all, "
                     "by design: polling can be perfect and the floor "
                     "stays empty forever",
                     "re-run smib_setup.sh (its default --hub auto is "
                     "right on the wired slot segment), or add --hub "
                     "http://192.168.50.2:8081 to ExecStart.")
        elif hub_flag.startswith("http://127.0.0.1") and not is_hub_box:
            _check(doc, "S1.3 --hub", False, hub_flag)
            _finding(doc, "SELF-REPORT", "fatal",
                     "--hub %s on a Pi that is NOT the hub — the satellite "
                     "reports to ITSELF: connection refused, no tile, ever "
                     "(a hand-copied unit that never met smib_setup's sed)"
                     % hub_flag,
                     "re-run smib_setup.sh, or point --hub at "
                     "http://192.168.50.2:8081.")
        else:
            _check(doc, "S1.3 --hub", True,
                   "%s · --address %s · port %s"
                   % (hub_flag, _flag_val(toks, "--address") or "1", port))

    # S1.4 — the UART five-condition audit + the board's own traps
    board = _q(t, "tr -d '\\0' < /proc/device-tree/model 2>/dev/null "
                  "|| echo unknown").strip() or "unknown"
    _check(doc, "board", None, board)
    if present:
        cfg = _q(t, 'CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || '
                    'CFG=/boot/config.txt; grep -E '
                    '"^enable_uart|^dtoverlay=disable-bt" "$CFG" '
                    '2>/dev/null || true')
        have_uart = any(l.startswith("enable_uart=1")
                        for l in cfg.splitlines())
        have_bt = any(l.startswith("dtoverlay=disable-bt")
                      for l in cfg.splitlines())
        if not have_bt:
            _check(doc, "S1.4 config.txt", False,
                   "dtoverlay=disable-bt MISSING"
                   + ("" if have_uart else " (enable_uart=1 too)"))
            _finding(doc, "MINI-UART-NO-PARITY", "fatal",
                     "without dtoverlay=disable-bt the GPIO14/15 header "
                     "gets the mini-UART — no parity support, and SAS "
                     "wakeup IS mark/space parity per byte: silence with "
                     "perfect wiring",
                     "smib_setup.sh writes the two config.txt lines "
                     "(enable_uart=1 + dtoverlay=disable-bt); reboot "
                     "after — config.txt changes are boot-time.")
        elif not have_uart:
            _check(doc, "S1.4 config.txt", False,
                   "disable-bt present but enable_uart=1 MISSING")
        else:
            _check(doc, "S1.4 config.txt", True,
                   "enable_uart=1 + dtoverlay=disable-bt")
        cmdline = _q(t, 'CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || '
                        'CMD=/boot/cmdline.txt; cat "$CMD" '
                        '2>/dev/null || true')
        if re.search(r"console=(serial0|ttyAMA0)", cmdline):
            _check(doc, "S1.4 cmdline", False, "console= on the SAS line")
            _finding(doc, "CONSOLE-EATS-LINE", "fatal",
                     "cmdline.txt still routes a kernel console onto the "
                     "serial line — the kernel eats SAS frames",
                     "strip console=serial0,.../console=ttyAMA0,... from "
                     "cmdline.txt (smib_setup.sh's sed does) and reboot.")
        else:
            _check(doc, "S1.4 cmdline", True, "no serial console")
        hci = _q(t, "systemctl is-active hciuart "
                    "2>/dev/null || true").strip()
        if hci == "active":
            _check(doc, "S1.4 hciuart", False,
                   "active — BlueZ still holds the PL011 (reboot pending, "
                   "or disable-bt missing)")
        else:
            _check(doc, "S1.4 hciuart", True, hci or "inactive")
        tty = port.rsplit("/", 1)[-1]
        getty_en = _q(t, "systemctl is-enabled serial-getty@%s.service "
                         "2>/dev/null || true" % tty).strip()
        getty_act = _q(t, "systemctl is-active serial-getty@%s.service "
                          "2>/dev/null || true" % tty).strip()
        if getty_act == "active":
            _check(doc, "S1.4 getty", False,
                   "serial-getty@%s is ACTIVE" % tty)
            _finding(doc, "GETTY-CONTENTION", "fatal",
                     "a login getty is up on %s — it contends for the port "
                     "and sprays login prompts into the machine" % port,
                     "sudo systemctl mask --now serial-getty@%s.service "
                     "(smib_setup.sh masks it)." % tty)
        elif getty_en != "masked":
            _check(doc, "S1.4 getty", False,
                   "serial-getty@%s is %s (not masked) — it will contend "
                   "after the next boot" % (tty, getty_en or "?"))
        else:
            _check(doc, "S1.4 getty", True, "serial-getty@%s masked" % tty)
        if _q(t, "ls %s 2>/dev/null || true" % port).strip():
            _check(doc, "S1.4 port", True, "%s exists" % port)
        else:
            _check(doc, "S1.4 port", False, "%s MISSING" % port)
            _finding(doc, "PORT-MISSING", "fatal",
                     "%s does not exist — and the unit stays ACTIVE anyway "
                     "(sas_host retries a missing port forever, 30s "
                     "backoff): `systemctl is-active` is NOT proof of a "
                     "live link" % port,
                     "the config.txt UART lines plus a reboot bring the "
                     "PL011 up; on a non-Pi board there is no PL011 and "
                     "no SAS.")
        if "Pi 5" in board and port.endswith("serial0"):
            _finding(doc, "DEBUG-CONNECTOR", "fatal",
                     "/dev/serial0 on a Pi 5 is ttyAMA10 — the DEBUG "
                     "pads, never SAS",
                     "point the unit at /dev/ttyAMA0, the GPIO14/15 "
                     "header UART (S6 fix-knobs is a later phase; "
                     "smib_setup.sh gets it right).")
        who = unit["user"]
        g_out = _q(t, ("id -nG %s 2>/dev/null || true" % who) if who
                   else "id -nG 2>/dev/null || true")
        if g_out.strip():
            if "dialout" in g_out.split():
                _check(doc, "S1.4 dialout", True,
                       "%s is in dialout" % (who or "service user"))
            else:
                _check(doc, "S1.4 dialout", False,
                       "%s NOT in dialout" % (who or "service user"))
                _finding(doc, "NO-DIALOUT", "fatal",
                         "the service user is not in the dialout group — "
                         "the port open fails (permission denied) in the "
                         "same quiet 30s retry loop",
                         "sudo usermod -aG dialout %s — takes a re-login "
                         "or reboot to stick (smib_setup.sh does this)."
                         % (who or "<user>"))
        else:
            _check(doc, "S1.4 dialout", None, "groups not readable")
        # venv import check — the deps smib_setup.sh installs, imported by
        # the unit's OWN interpreter (token 0 of ExecStart, never guessed)
        venv_py = toks[0] if toks and toks[0].startswith("/") else ""
        if venv_py:
            v = _q(t, "[ -x %s ] && { %s -c 'import serial, crcmod, "
                      "loguru' >/dev/null 2>&1 && echo venv-ok || echo "
                      "deps-broken; } || echo venv-missing"
                      % (venv_py, venv_py)).strip()
            if v == "venv-ok":
                _check(doc, "S1.4 venv", True,
                       "%s imports pyserial/crcmod/loguru" % venv_py)
            elif v in ("deps-broken", "venv-missing"):
                _check(doc, "S1.4 venv", False,
                       "%s: %s — re-run smib_setup.sh (S8 venv repair is a "
                       "later phase)" % (venv_py, v))
            else:
                _check(doc, "S1.4 venv", None, "venv probe did not answer")

    # S1.5 — journal GREP, never tail: "hub report failed" is edge-logged
    # ONCE per outage and the parking line once per park; tail-and-wait
    # misses both. Scoped to this boot: current state, not archaeology.
    if present and active == "active":
        hits = _q(t, "journalctl -u cabinet-sas -b --no-pager -o cat "
                     "2>/dev/null | grep -E 'cannot open|hub report failed"
                     "|parking the poll loop|ImportError"
                     "|ModuleNotFoundError' | tail -12 || true")
        # The line count rides the raw probe's OWN rc: a journal that
        # cannot be read here (service user outside adm on a hand-built
        # board, a box that died mid-doctor) is "could not look" and must
        # never mint the fatal SILENT-MISMATCH against a healthy tree.
        rc_j, jraw = _q_rc(t, "journalctl -u cabinet-sas -b --no-pager "
                              "-o cat 2>/dev/null")
        nhits = len(hits.strip().splitlines()) if hits.strip() else 0
        if rc_j != 0:
            _check(doc, "S1.5 journal", None,
                   "journal not readable here — the silent-mismatch check "
                   "cannot run")
        else:
            _check(doc, "S1.5 journal", None,
                   "%d lines this boot, %d signature hits"
                   % (len(jraw.splitlines()), nhits))
        if "cannot open" in hits:
            _finding(doc, "PORT-MISSING", "fatal",
                     "journal: cannot open %s — retrying quietly while the "
                     "unit stays active ('active' is not 'alive')" % port,
                     "see the S1.4 rows: config.txt/reboot for a missing "
                     "node, dialout for permission denied.")
        if "hub report failed" in hits:
            _finding(doc, "HUB-UNREACHABLE", "warn",
                     "journal: 'hub report failed' — edge-logged ONCE per "
                     "outage, retried quietly every 1s; the machine can "
                     "show ONLINE here while the floor shows nothing",
                     "check the S1.6 network rows + cabinet-g2s on the "
                     "hub; the tile returns within ~1s of the hub "
                     "answering again.")
        if "parking the poll loop" in hits:
            _finding(doc, "HUB-PARKED", "warn",
                     "this Pi's own log says the hub told it to stand "
                     "down, so it stopped polling the machine — a setting "
                     "someone chose, not a fault",
                     "turn it back on in the web UI: Switchboard ▸ "
                     "Machines ▸ <machine> ▸ the SAS switch. This tool "
                     "deliberately never changes hub settings itself.")
        if "ImportError" in hits or "ModuleNotFoundError" in hits:
            _finding(doc, "TREE-MISMATCH", "fatal",
                     "journal: import errors — the satellite's SAS tree "
                     "does not match the code it was started against",
                     "SAS/core must be identical hub/satellite: run "
                     "deploy/update.py, or re-run smib_setup.sh from the "
                     "hub's CURRENT clone — never a private rsync.")
        if rc_j == 0 and not jraw.splitlines():
            _finding(doc, "SILENT-MISMATCH", "fatal",
                     "unit active with an EMPTY journal this boot — the "
                     "silent flavor of a stale tree: the service runs, "
                     "logs nothing, and the SAS link is simply dead",
                     "diff SAS/core against the hub clone, or just re-run "
                     "smib_setup.sh / deploy/update.py (both idempotent).")

    # S1.6 — network: wired slot segment, hub-derived hub URL, hub reach
    on_seg = ("inet " + SLOT_PREFIX) in ip_out
    gw = ""
    for line in _q(t, "ip route 2>/dev/null || true").splitlines():
        tk = line.split()
        if tk[:1] == ["default"] and "via" in tk:
            gw = tk[tk.index("via") + 1]   # kernel lists lowest-metric
            break                          # first — what --hub auto derives
    if is_hub_box:
        _check(doc, "S1.6 network", True,
               "this box IS the hub (192.168.50.2)")
    elif not on_seg:
        _check(doc, "S1.6 network", False, "no %sx address" % SLOT_PREFIX)
        _finding(doc, "WRONG-SEGMENT", "fatal",
                 "no 192.168.50.x lease on any NIC — wrong switch/port, or "
                 "cabinet-dhcp is down on the hub",
                 "satellites are WIRED-ONLY on the slot segment; plug into "
                 "the slot switch and let the hub's DHCP lease it (that IS "
                 "the zero-config keystone — never hand-configure the "
                 "NIC).")
    else:
        _check(doc, "S1.6 network", True,
               "on the slot segment, default via %s" % (gw or "?"))
    if _flag_val(toks, "--hub") == "auto" and gw \
            and not gw.startswith(SLOT_PREFIX):
        _finding(doc, "HOME-ROUTER-DERIVED", "fatal",
                 "--hub auto derives http://<default-gateway>:8081, and "
                 "the lowest-metric default route here is via %s — a HOME "
                 "router: reports post into the void" % gw,
                 "wire the Pi so the hub is its gateway (slot-segment DHCP "
                 "does exactly that) or set --hub http://192.168.50.2:8081 "
                 "explicitly; drop any leftover Wi-Fi attachment.")
    if on_seg or is_hub_box:
        reach = _q(t, "curl -m 3 -s http://192.168.50.2:8081/api/hubkey "
                      ">/dev/null 2>&1 && echo hub-answers || echo "
                      "hub-dark").strip()
        if reach == "hub-answers":
            _check(doc, "S1.6 hub reach", True,
                   "/api/hubkey answers from here")
        elif reach == "hub-dark":
            _check(doc, "S1.6 hub reach", False,
                   "http://192.168.50.2:8081/api/hubkey does not answer")
            _finding(doc, "HUB-DOWN", "warn",
                     "the hub API does not answer from this Pi",
                     "run the hub doctor ON the hub (cabinet-g2s down, or "
                     ":8081 has an imposter); the report leg retries "
                     "quietly and the tile returns within ~1s of the hub "
                     "coming back.")
        else:
            _check(doc, "S1.6 hub reach", None, "curl did not answer")

    # The hub's view of this leg (per smibId/address KEY — a multidrop Pi
    # can have one live machine and one dark one)
    ents = _sas_entries(st, name, getattr(t, "peer", None))
    online_any = False
    for key, e in ents:
        # Sentences, never the dict. "online=True stale=False" is a hub
        # internal spelled at a collector; the age is the one number worth
        # showing, and only when it says something.
        row = "SAS %s" % key
        _a = e.get("reportAgeSec")
        age = _age_str(_a) if isinstance(_a, (int, float)) else ""
        if e.get("sasEnabled") is False:
            _check(doc, row, False,
                   "the hub is telling this connection to stand down")
            _finding(doc, "HUB-PARKED", "warn",
                     "the hub is set to keep %s switched off, so the Pi "
                     "parks it every time it reports — this is a setting "
                     "someone chose, not a fault" % key,
                     "turn it back on in the web UI: Switchboard ▸ "
                     "Machines ▸ <machine> ▸ the SAS switch.")
        elif e.get("stale"):
            _check(doc, row, False,
                   "the hub has not heard from it%s — either this Pi went "
                   "quiet or its reports are not arriving"
                   % ((" for " + age) if age else ""))
        elif e.get("online"):
            online_any = True
            _check(doc, row, True,
                   "the hub hears it and the machine is answering")
        else:
            _check(doc, row, False,
                   "the hub hears this Pi, but the machine is not answering")
            _finding(doc, "MACHINE-LEG-OFF", "warn",
                     "%s is reporting in fine, so the Pi is healthy — our "
                     "connection just isn't reaching the machine (it may be "
                     "powered off, its SAS channel/address may not match, "
                     "or the wiring)" % key,
                     "the first-contact bench check below settles "
                     "machine-side versus wiring in one pass.")
    if not ents:
        _check(doc, "hub tile", None,
               "no status tile matched (hub API dark, or nothing ever "
               "reported under this name/peer)")

    _probe_health(t, doc)
    _doc_close(doc)

    # S1.7 [signpost] — the deliberately-MANUAL first-contact bench dance.
    # This tool NEVER stops the service or touches the port; it prints the
    # documented sequence (SMIB_FRESH_IMAGE §3a) and how to read it.
    if present and not online_any \
            and "NEVER-PROVISIONED" not in doc["codes"]:
        venv_py = toks[0] if toks and toks[0].startswith("/") \
            else "~/venvs/cabinet/bin/python"
        say("")
        signpost_bench_dance(port, venv_py)
    return doc


def signpost_bench_dance(port="/dev/ttyAMA0",
                         venv_py="~/venvs/cabinet/bin/python"):
    """S1.7 — the deliberately-MANUAL first-contact bench dance. This tool
    never stops the service and never touches the port; it prints the
    documented sequence (SMIB_FRESH_IMAGE §3a) and how to read it. Lives out
    here so the menu can offer it exactly where MACHINE-LEG-OFF is named,
    instead of making someone go looking for it."""
    # 80 columns, hard: this is a live menu leaf AND the tail of every SAS
    # check, so it is one of the most-read screens in the tool. _pad wraps
    # the prose; _cmds wraps the commands on a shell continuation.
    for ln in _indent("First-contact check — you run this one yourself on "
                      "the Pi; this tool never touches the serial port "
                      "while the machine might be using it.", 3):
        say(ln)
    for ln in _cmds("sudo systemctl stop cabinet-sas",
                    "cd ~/CabiNet/SAS && %s tools/sas_bench_poll.py %s "
                    "--credits" % (venv_py, port),
                    "sudo systemctl start cabinet-sas"):
        say(ln)
    for ln in _indent("How to read it — the machine does what it was told, "
                      "so OUR chain is what is in question:", 3):
        say(ln)
    for ln in _indent("Nothing at all = our connection isn't reaching the "
                      "machine: its SAS channel may be off or set to a "
                      "different address, or TX/RX are not crossed.", 6):
        say(ln)
    for ln in _indent("Garbled replies = GOOD news, the machine is alive "
                      "and talking: swap TX/RX, check the ground, and keep "
                      "a capture for COMPATIBILITY.md.", 6):
        say(ln)
    return {"port": port, "codes": ["MACHINE-LEG-OFF"], "signpost": True}


def doctor_hub(root, t, status=None):
    """H1 [ro] — hub doctor; MUST work in degraded mode (`status` may carry
    __error__ — hub.db and journal probes still answer; fetched when None).
    Codes (the same stuck-floor catalog as S1/C1):
      H1.1 status .sas empty AND hub.db satellites has zero kind='sas' rows
           (read-only SELECT, mode=ro) → NO-SAS-SATELLITE-EVER-REPORTED;
           prints the C1.1 companion-tier sentence — this one check names
           the whole never-built-leg class from the hub alone.
      H1.2 per-tile: stale=true → SATELLITE-SILENT; online=false + fresh
           reports → MACHINE-LEG-DARK; prefs sasEnabled=false → HUB-PARKED.
      H1.3 casinonet-* unit files / unit WorkingDirectory vs live tree →
           RENAME-RESIDUE / TWO-TREE (kiosk/console/smibui are
           grandfathered by name until their next reinstall — expected,
           never residue).
      H1.4 service start time vs HEAD commit time → PARTIAL-DEPLOY.
      H1.5 journal count "POST to unexpected path /G2S127.0.0.1" →
           EGM-HOST-POINT-UNSET.
    Plus the hub hardware/infra battery: five units (HUB-DOWN when
    cabinet-g2s is down),
    :8081 owner vs MainPID, port owners 67/53/123/69, slot-NIC sanity,
    hub.db quick_check (ro), hub key, locks/markers, git state,
    KillMode=process presence, SD health + df, clock.
    Returns the shared doctor dict with target "hub"; verdict is OK or the
    first fatal code — a tile parked on purpose never becomes the hub's
    verdict."""
    doc = _doc_new("hub", "hub")
    step("Hub doctor")
    if status is None:
        status = api_status()
    degraded = not isinstance(status, dict) or "__error__" in status
    st = {} if degraded else status
    _check(doc, "hub API", None if degraded else True,
           "/api/status is not answering — degraded probes (units, db, "
           "journal still tell the story)" if degraded
           else "/api/status answers")

    # identity — TWO-TREE is H1.3's second half, but it gates everything:
    # diagnosing one clone while the units run another wastes the night
    # (the first tester ran a stray second tree for days).
    ident = hub_identity(root, t)
    if ident["wd_matches"]:
        _check(doc, "unit tree", True,
               "cabinet-g2s runs THIS tree (%s)" % (ident["wd"] or root))
    elif ident["wd"]:
        _check(doc, "unit tree", False,
               "unit WorkingDirectory=%s is NOT this clone" % ident["wd"])
        _finding(doc, "TWO-TREE", "fatal",
                 "the installed cabinet-g2s unit runs a DIFFERENT tree "
                 "than this one",
                 "cd into %s (the tree the unit actually runs) and work "
                 "there; one tree per hub." % ident["wd"])
    else:
        _check(doc, "unit tree", False,
               "cabinet-g2s unit unreadable or absent")

    units = ["cabinet-g2s", "cabinet-dhcp", "cabinet-dns", "cabinet-ntp",
             "cabinet-tftp"]
    states = _q(t, "systemctl is-active %s 2>/dev/null || true"
                % " ".join(units)).split()
    if len(states) == len(units):
        _check(doc, "hub units", all(s == "active" for s in states),
               ", ".join("%s=%s" % (u.replace("cabinet-", ""), s)
                         for u, s in zip(units, states)))
        if states[0] != "active":
            # `is-active` says "inactive" for a unit that does not even
            # exist — ident["units"] (real unit FILES) tells the two apart.
            if ident["units"]:
                _finding(doc, "HUB-DOWN", "fatal",
                         "cabinet-g2s is %s — no floor, no API, no SAS "
                         "ingest" % states[0],
                         "journalctl -u cabinet-g2s -n 50 names the crash "
                         "(the H2 restart verb is a later phase — and "
                         "never bounce the hub while someone is mid-config "
                         "at a machine).")
            else:
                _finding(doc, "HUB-DOWN", "fatal",
                         "the hub units are not installed on this box at "
                         "all",
                         "sudo ./deploy/hub_setup.sh builds the hub — "
                         "SETUP, not repair (and this box may simply not "
                         "be the hub).")
    else:
        _check(doc, "hub units", None, "systemctl did not answer")

    # :8081 — owner vs MainPID (an imposter here mimics a dead hub)
    mainpid = _q(t, "systemctl show -p MainPID --value cabinet-g2s "
                    "2>/dev/null || true").strip()
    ss8081 = _q(t, "ss -ltnp 2>/dev/null | grep ':8081 ' || true").strip()
    m = re.search(r"pid=(\d+)", ss8081)
    if not ss8081:
        _check(doc, ":8081 owner", None if degraded else False,
               "no listener visible on :8081")
    elif m and mainpid.isdigit() and mainpid != "0" \
            and m.group(1) != mainpid:
        _check(doc, ":8081 owner", False,
               "pid %s owns :8081 but cabinet-g2s MainPID is %s — an "
               "imposter, or a manual dev host (the floor is the dev "
               "floor: is it yours, running on purpose?)"
               % (m.group(1), mainpid))
    elif m:
        _check(doc, ":8081 owner", True, "cabinet-g2s (pid %s)" % mainpid)
    else:
        _check(doc, ":8081 owner", None,
               "listener present; owner needs root to see")
    udp = _q(t, "ss -lunp 2>/dev/null | grep -E ':(67|53|123|69) ' "
                "|| true").strip()
    if udp:
        foreign = [l for l in udp.splitlines()
                   if "pid=" in l and "python" not in l]
        if foreign:
            _check(doc, "udp 67/53/123/69", False,
                   "foreign owner: %s — half-hub / port squatter (H3 is a "
                   "later phase; evicting systemd-resolved kills the "
                   "hub's own DNS unless repointed)" % foreign[0][:80])
        else:
            _check(doc, "udp 67/53/123/69", True,
                   "%d listener(s), ours where visible"
                   % len(udp.splitlines()))
    else:
        _check(doc, "udp 67/53/123/69", None if degraded else False,
               "no DHCP/DNS/NTP/TFTP listeners visible")
    _check(doc, "slot NIC", ident["slot_nic"],
           "192.168.50.2 present" if ident["slot_nic"]
           else "192.168.50.2 on no NIC — the slot segment is dark "
                "(hub_setup.sh --nic repair is a later phase)")

    # hub.db — READ-ONLY, mode=ro URI (this tool never writes hub.db, ever)
    db = os.path.join(root, "G2S", "data", "hub.db")
    qc = _q(t, "python3 -c \"import sqlite3;print(sqlite3.connect("
               "'file:%s?mode=ro',uri=True).execute('pragma quick_check')"
               ".fetchone()[0])\" 2>/dev/null || true" % db).strip()
    if qc == "ok":
        _check(doc, "hub.db quick_check", True, "ok (read-only)")
    elif qc:
        _check(doc, "hub.db quick_check", False,
               qc.splitlines()[-1][:100] + " — hub.db is damaged; the "
               "host quarantines a corrupt db on open, and update.py's "
               "snapshots are the restore path (H9, later phase)")
    else:
        _check(doc, "hub.db quick_check", None,
               "hub.db not readable here (no db yet, or python3/sqlite "
               "unavailable)")

    # H1.1 — the never-built-leg check: has ANY SAS satellite EVER reported?
    sas_db = _q(t, "python3 -c \"import sqlite3;print(sqlite3.connect("
                   "'file:%s?mode=ro',uri=True).execute(\\\"select "
                   "count(*) from satellites where kind='sas'\\\")"
                   ".fetchone()[0])\" 2>/dev/null || true" % db).strip()
    live_sas = st.get("sas") or {}
    if sas_db.isdigit():
        _check(doc, "H1.1 sas census", None,
               "hub.db kind='sas' rows: %s · live sas tiles: %s"
               % (sas_db, "?" if degraded else len(live_sas)))
        if int(sas_db) == 0 and (degraded or not live_sas):
            _finding(doc, "NO-SAS-SATELLITE-EVER-REPORTED", "info",
                     "status .sas is empty and hub.db has ZERO kind='sas' "
                     "rows — not one POST to /api/sas/report has EVER "
                     "reached this hub. That is not a broken SAS leg; it "
                     "is a leg that was never built (fine on a G2S-only "
                     "floor)",
                     _C11_BANNER)
    else:
        _check(doc, "H1.1 sas census", None,
               "hub.db satellites not readable — census unavailable")

    # H1.2 — per-tile triage (status only; the tiles are in-memory)
    if degraded:
        _check(doc, "H1.2 tiles", None,
               "status dark — per-tile triage unavailable")
    for key, e in sorted(live_sas.items()):
        e = e or {}
        # Sentences, not the dict: this table is read by someone who has
        # never seen the hub's internals, next to rows like "disk / 6% used".
        row = "SAS %s" % key
        _a = e.get("reportAgeSec")
        age = _age_str(_a) if isinstance(_a, (int, float)) else ""
        where = e.get("peer") or "the Pi"
        if e.get("stale"):
            _check(doc, row, False,
                   "silent%s" % ((" for " + age) if age else ""))
            _finding(doc, "SATELLITE-SILENT", "warn",
                     "the hub has not heard from %s%s — the Pi is powered "
                     "off, off the network, or its reports are not "
                     "arriving" % (key, (" for " + age) if age else ""),
                     "check that Pi from the floor list — or it is simply "
                     "switched off; a machine that has been set up never "
                     "disappears from the floor, by design.")
        elif e.get("sasEnabled") is False:
            _check(doc, row, False,
                   "the hub is telling this connection to stand down")
            _finding(doc, "HUB-PARKED", "warn",
                     "the hub is set to keep %s switched off, so the Pi "
                     "parks it every time it reports — a setting someone "
                     "chose, not a fault" % key,
                     "turn it back on in the web UI: Switchboard ▸ "
                     "Machines ▸ <machine> ▸ the SAS switch (never leave "
                     "the machine enabled at 0 credits, and never "
                     "re-enable one someone locked).")
        elif not e.get("online"):
            _check(doc, row, False,
                   "the Pi is reporting, the machine is not answering")
            _finding(doc, "MACHINE-LEG-DARK", "warn",
                     "%s is reporting in fine, so the Pi is healthy — our "
                     "connection just isn't reaching the machine (powered "
                     "off, its SAS channel/address may not match, or the "
                     "wiring)" % key,
                     "open %s in the floor list and run its check — the "
                     "first-contact bench check settles machine-side "
                     "versus wiring in one pass." % where)
        else:
            _check(doc, row, True,
                   "the hub hears it and the machine is answering")

    # H1.3 — rename residue. kiosk/console/smibui still answer casinonet-*
    # on live boxes until their next reinstall (grandfathered by name) —
    # expected, never flagged.
    resid = _q(t, "systemctl list-unit-files 'casinonet-*' --no-legend "
                  "--no-pager 2>/dev/null || true")
    names = [l.split()[0] for l in resid.splitlines() if l.split()]
    grand = [n for n in names
             if any(g in n for g in ("kiosk", "console", "smibui"))]
    bad = [n for n in names if n not in grand]
    if bad:
        _check(doc, "H1.3 legacy units", False, " ".join(bad))
        _finding(doc, "RENAME-RESIDUE", "warn",
                 "pre-rename unit files still installed: %s"
                 % ", ".join(bad),
                 "sudo ./deploy/hub_setup.sh retires them and keeps the "
                 "data (update.py refuses a pre-rename hub at preflight "
                 "for exactly this reason).")
    else:
        _check(doc, "H1.3 legacy units", True,
               "no casinonet-* residue"
               + (" (grandfathered: %s)" % ", ".join(grand)
                  if grand else ""))

    # H1.4 — deploy skew: a process older than HEAD runs code that is not
    # on disk (the first tester's DHCP ran pre-fix code for two days).
    rc_h, head_ct = _git(root, "log", "-1", "--format=%ct")
    if rc_h == 0 and head_ct.strip().isdigit():
        starts = _q(t, 'for u in %s; do echo "$u $(systemctl show -p '
                       'ExecMainStartTimestamp --value $u 2>/dev/null)"; '
                       'done' % " ".join(units))
        skewed, seen = [], 0
        for line in starts.splitlines():
            parts = line.split(None, 1)
            m2 = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                           parts[1] if len(parts) > 1 else "")
            if not m2:
                continue
            seen += 1
            try:
                begun = time.mktime(time.strptime(m2.group(0),
                                                  "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if begun < int(head_ct.strip()):
                skewed.append("%s (up since %s)" % (parts[0], m2.group(0)))
        if not seen:
            _check(doc, "H1.4 deploy skew", None,
                   "no running hub services to compare against HEAD")
        elif skewed:
            _check(doc, "H1.4 deploy skew", False, "; ".join(skewed))
            _finding(doc, "PARTIAL-DEPLOY", "warn",
                     "running process older than the tree's HEAD: %s — "
                     "the code on disk is not the code in memory"
                     % "; ".join(skewed),
                     "a webui-only change needs no restart; anything else "
                     "does — run deploy/update.py (it restarts + verifies "
                     "the whole fleet) rather than bouncing units by "
                     "hand.")
        else:
            _check(doc, "H1.4 deploy skew", True,
                   "every hub service started after HEAD landed")
    else:
        _check(doc, "H1.4 deploy skew", None,
               "not a git clone — cannot compare HEAD age to process age")

    # H1.5 — half-hub spam: an EGM whose host point still holds the factory
    # 127.0.0.1 placeholder mis-joins it onto our path (GitHub issue #1)
    cnt = _q(t, "journalctl -u cabinet-g2s -b --no-pager -o cat "
                "2>/dev/null | grep -c 'POST to unexpected path "
                "/G2S127.0.0.1' || true").strip()
    if cnt.isdigit() and int(cnt) > 0:
        _check(doc, "H1.5 half-hub spam", False, "%s hits this boot" % cnt)
        _finding(doc, "EGM-HOST-POINT-UNSET", "warn",
                 "%s 'POST to unexpected path /G2S127.0.0.1…' journal "
                 "hits — an EGM still carries the 127.0.0.1 host-point "
                 "placeholder" % cnt,
                 "set the G2S host ON THE MACHINE, manually, as three URI "
                 "segments: http:// 192.168.50.2 :8081/G2S (there is no "
                 "DHCP-delivered host apply — closed, wire-proven), then "
                 "re-enable G2S in the debug menu.")
    elif cnt.isdigit():
        _check(doc, "H1.5 half-hub spam", True,
               "no unexpected-path spam this boot")
    else:
        _check(doc, "H1.5 half-hub spam", None, "journal not readable")

    # KillMode + housekeeping facts. `systemctl show` invents defaults for
    # units that do not exist ("control-group") — only judge drift when the
    # unit file is really installed, or every dev box screams KillMode.
    km = _q(t, "systemctl show -p KillMode --value cabinet-g2s "
               "2>/dev/null || true").strip()
    if not ident["units"]:
        _check(doc, "KillMode", None, "cabinet-g2s not installed here")
    elif km == "process":
        _check(doc, "KillMode", True,
               "process (LOAD-BEARING: the card-spawned updater must "
               "outlive the restart it performs)")
    elif km:
        _check(doc, "KillMode", False,
               "%s — drift! restore KillMode=process (H8 is a later "
               "phase; restore-only, never removal)" % km)
    else:
        _check(doc, "KillMode", None, "not readable")
    holder = lock_holder(root)
    _check(doc, "update lock", None,
           ("held by %s" % holder) if holder else "free")
    if os.path.exists(os.path.join(root, _u.ADOPT_MARKER)):
        _check(doc, "adopt marker", False,
               "%s present — the last adoption never completed an update; "
               "run deploy/update.py before repairing" % _u.ADOPT_MARKER)
    rc_p, porc = _git(root, "status", "--porcelain")
    if rc_p == 0:
        dirty = len([l for l in porc.splitlines() if l.strip()])
        _check(doc, "git tree", None,
               "clean" if not dirty else
               "%d dirty path(s) — hot dev edits may be deliberate "
               "(update.py owns the refusal table)" % dirty)
    _check(doc, "hub key", os.path.exists(DEFAULT_KEY),
           DEFAULT_KEY if os.path.exists(DEFAULT_KEY)
           else "%s missing — satellite ssh (probes, doctors, update.py) "
                "has no key" % DEFAULT_KEY)
    _probe_health(t, doc)
    return _doc_close(doc, states=())


def sweep(ctx, summary=True):
    """D3 [ro] — fleet-wide doctor sweep: doctor_hub first, then doctor_smib
    per satellite and doctor_companion per companion from discover_fleet.
    Unreachable boxes are REPORTED rows (verdict UNREACHABLE), never silent
    skips. Degraded mode still sweeps: the hub doctor runs on __error__
    status by contract, and each ssh-reachable lease/ARP candidate gets BOTH
    doctors — their own unit-census probes (S1.1/C1.1) say which duty the
    box actually has. Prints each doctor then a flagged-first summary table.
    Args: ctx (ctx["local"] overrides the hub-side transport for the gate);
    summary=False suppresses the unnumbered summary table for the menu, which
    renders its own NUMBERED one so a number still means "the n-th thing on
    this screen". Returns the list of doctor dicts (hub first) for --json."""
    local = ctx.get("local") or LocalTransport()
    status = api_status()
    fleet = discover_fleet(status, ctx["root"], local=local)
    users = {s["peer"]: _u.sat_user(s) for s in fleet["sats"]}
    # ON the hub, a box that will not answer ssh IS unreachable — the hub is
    # the box that owns the slot segment and the key. From anywhere else the
    # same silence only means THIS computer has no route there: routing to
    # the hub is not routing to the slot segment. Calling a healthy floor
    # UNREACHABLE from a laptop is the confidently-wrong sentence this tool
    # exists to kill, so off the hub the row says what it honestly is — the
    # same courtesy the hub's own row already gets below.
    here = (ctx.get("identity") or {}).get("verdict") == "hub"
    NOROUTE = "(not reachable from this computer)"

    def _dark(target, kind, peer):
        if here:
            return {"target": target, "kind": kind, "peer": peer,
                    "codes": ["UNREACHABLE"], "verdict": "UNREACHABLE"}
        return {"target": target, "kind": kind, "peer": peer,
                "codes": [], "verdict": NOROUTE}

    def _reach(peer, user):
        # Reuse discovery's probe when it has one; otherwise probe now — a
        # doctor run against a dead box would just be 20 slow failures.
        if peer not in fleet["probes"]:
            rc, _o = local.run(_probe_cmd(peer, user),
                               check=False, timeout=15, quiet=True)
            fleet["probes"][peer] = (rc == 0)
        return fleet["probes"][peer]

    def _row(target, kind, peer, result):
        if result is None:              # a still-stubbed doctor
            result = {"target": target, "codes": [],
                      "verdict": "(doctor not built yet)"}
        result.setdefault("kind", kind)
        result.setdefault("peer", peer)
        return result

    step("Fleet-wide doctor sweep")
    if not here:
        # doctor_hub's git, lock, adopt-marker and identity legs all evaluate
        # against the LOCAL path: run off the hub it diagnoses this laptop
        # and emits HUB-DOWN about a hub that answers in milliseconds. That
        # lie is precisely what this tool exists to kill, so the row says
        # what it honestly is instead.
        say("   • the hub's own checkup reads unit files, ports, the "
            "database and the")
        say("     journal — all of which need a shell ON the hub, so it is "
            "not examined")
        say("     below. Everything the hub HEARS is still on the board.")
        results = [{"target": "hub", "kind": "hub", "peer": SLOT_PREFIX + "2",
                    "codes": [], "verdict": "(not examined from here)"}]
    else:
        results = [_row("hub", "hub", SLOT_PREFIX + "2",
                        doctor_hub(ctx["root"], local, status))]
    for s in fleet["sats"]:
        user = users[s["peer"]]
        say("   Checking %s (%s)…" % (s["smibId"], s["peer"]))
        if not _reach(s["peer"], user):
            results.append(_dark(s["smibId"], "smib", s["peer"]))
            continue
        results.append(_row(s["smibId"], "smib", s["peer"],
                            doctor_smib(SshTransport(s["peer"], user=user),
                                        s["smibId"], status)))
    for c in fleet["companions"]:
        user = users.get(c["peer"], _u.SAT_USER)
        say("   Checking %s (%s)…" % (c["companionId"], c["peer"]))
        if not _reach(c["peer"], user):
            results.append(_dark(c["companionId"], "companion", c["peer"]))
            continue
        results.append(_row(c["companionId"], "companion", c["peer"],
                            doctor_companion(SshTransport(c["peer"],
                                                          user=user),
                                             c["companionId"], status)))
    if fleet["degraded"]:
        say("   ⚠️ status dark — sweeping ssh-reachable lease/ARP boxes "
            "with both doctors; the unit census on each says its duty.")
        name = {l["ip"]: l["hostname"] or l["ip"] for l in fleet["leases"]}
        for peer, ok in sorted(fleet["probes"].items()):
            if not ok:
                results.append(_dark(name.get(peer, peer), "wire", peer))
                continue
            t = SshTransport(peer, user=_u.SAT_USER)
            results.append(_row(name.get(peer, peer), "smib", peer,
                                doctor_smib(t, name.get(peer, peer),
                                            status)))
            results.append(_row(name.get(peer, peer), "companion", peer,
                                doctor_companion(t, name.get(peer, peer),
                                                 status)))

    boxes = [r for r in results if r.get("kind") != "hub"]
    if not here and boxes and all(r.get("verdict") == NOROUTE
                                  for r in boxes):
        say("")
        say("   Nothing on the slot segment answered ssh from this "
            "computer, so")
        say("   no checkup ran below. The hub owns that segment and the "
            "key — run")
        say("   the sweep there and every box gets a real one.")
    if not summary:
        return results
    say("")
    say("   sweep summary (flagged rows first):")
    rows = []
    for r in results:
        v = r.get("verdict") or "?"
        warned = any(f.get("severity") == "warn"
                     for f in r.get("findings") or [])
        if v == "OK":
            # A verdict-OK box can still carry warn findings (a wedged
            # reader, a parked tile) — flagged-rows-first is this table's
            # whole promise, so those must never render as a clean ✅.
            flag = "⚠️" if warned else "✅"
        elif v.startswith("("):         # "not examined" — never a verdict
            flag = "•"
        elif r.get("codes"):
            flag = "❌"
        else:
            flag = "⚠️"
        codes = [c for c in (r.get("codes") or []) if c != v]
        rows.append({"name": r.get("target") or "?", "kind": r["kind"],
                     "peer": r.get("peer") or "?", "flag": flag,
                     "detail": v + (" · " + ",".join(codes)
                                    if codes else "")})
    _print_rows(rows)
    return results


# ---------------------------------------------------------------------------
# VERBS — S2 / C2 (+ the S3 signpost and the finish pass)
# ---------------------------------------------------------------------------
# The verb contract, every time: probe first → show the LITERAL plan
# (--dry-run stops there) → confirm [y/N] → mutate via the existing proven
# mechanism (a single systemctl) → wire-verify → ONE outcome line. The
# caller (run_mutating) already holds the one-writer lock and has refused
# read-only mode — a verb body never takes the lock itself.


def _peer_of(t):
    """The peer the hub's status records for this transport: the ssh peer,
    or loopback for the hub's own box (a co-located SAS leg / the
    enrollment reader reports from 127.0.0.1)."""
    return getattr(t, "peer", None) or "127.0.0.1"


def _tournament_phase(status):
    """The tournament refusal gate (floor law: armed/countdown/running →
    every restart verb refuses, phase named — a mid-tournament restart
    loses seats/scores/countdown). Returns the phase when the refusal
    applies, else None. A dark hub API cannot be hosting a live tournament
    (the engine lives inside cabinet-g2s), so degraded mode never trips
    this."""
    if not isinstance(status, dict) or "__error__" in status:
        return None
    ph = str((status.get("tournament") or {}).get("phase") or "")
    return ph if ph in ("armed", "countdown", "running") else None


def _sd_ro(t):
    """⚑ The SD-health refusal probe: True when the target's
    rootfs is mounted read-only — the kernel's own verdict on a dying card.
    "sed succeeded, reboot bricked" is how headless Pis die, so every write
    verb refuses in this state instead of stacking config onto a card that
    cannot persist it. A probe that cannot answer reads as False: refusing
    on silence would strand every degraded box, and the doctor's
    SD-UNHEALTHY finding already names a sick card loudly."""
    out = _q(t, "findmnt -n -o OPTIONS / 2>/dev/null || true").strip()
    opts = out.splitlines()[0].split(",") if out else []
    return "ro" in opts


def _verify_wait(matcher, bound=15, interval=2):
    """The wire-verify loop: poll /api/status until matcher(status) answers
    truthy or ~bound seconds pass. Returns (matcher's hit or None, the last
    status dict). An __error__ status returns IMMEDIATELY so the caller can
    fall back to its degraded (unit + journal) verify instead of burning
    the whole bound against a dark API."""
    deadline = time.time() + bound
    while True:
        st = api_status()
        if "__error__" in st:
            return None, st
        hit = matcher(st)
        if hit:
            return hit, st
        if time.time() >= deadline:
            return None, st
        time.sleep(interval)


def verb_smib_up(t, sat, session, assume_yes=False, dry_run=False):
    """S2 [y/N] — bring the SAS leg up; the literal "fix it and bring it
    back up" motion for state UNIT-OFF. Seconds, no reboot. Over `t`:
      1. probe: re-run the S1.1 census — refuse NEVER-PROVISIONED (signpost
         S4/smib_setup.sh) and HUB-PARKED (signpost_hub_park); then ⚑ SD
         health — refuse a read-only rootfs (write verbs never build on a
         dying card);
      2. probe: port from the unit's OWN ExecStart (never assumed), then
         `fuser <port>` — anything holding it = refuse and name the pids
         (a bench poll or zombie casinonet-sas must not be yanked);
      3. show the literal plan; --dry-run stops here; confirm [y/N];
      4. `sudo systemctl enable --now cabinet-sas`;
      5. tile-live verify: /api/status sas entry for this smibId fresh
         (reportAgeSec 0-1) within a bounded wait (~15 s); degraded mode →
         fall back to journal-shows-reporting + say the verify is partial.
    Appends ("smib", smibId, peer) to session["touched"]. ONE outcome line
    (✅ smib-… SAS leg is back on the floor. / ❌ …). Returns True only on
    verified-up. Caller holds the writer lock (run_mutating)."""
    step("Bring the SAS leg up: %s" % (sat or t.name))
    status = api_status()
    st = status if "__error__" not in status else {}
    peer = _peer_of(t)
    phase = _tournament_phase(status)
    if phase:
        say("❌ tournament is %s — restarts are REFUSED until it finishes "
            "(a mid-tournament restart loses seats/scores/countdown). "
            "Nothing was changed." % phase)
        return False

    # 0. can the box answer at all? UNREACHABLE is not UNIT-OFF — with no
    # round-trip, the census below would misread silence as absence.
    if not t.reachable():
        say("   ❌ %s did not answer a single ssh probe — powered off, off "
            "the wire, or the hub key is not authorized there." % t.name)
        say("❌ %s: refused — UNREACHABLE; there is nothing to switch on "
            "until the box answers. Nothing was changed." % (sat or t.name))
        return False

    # 1. the S1.1 census, re-probed NOW — a doctor verdict may be an hour
    # stale, and this verb only exists for state UNIT-OFF.
    present = "cabinet-sas.service" in _q(
        t, "systemctl list-unit-files cabinet-sas.service "
           "--no-legend --no-pager 2>/dev/null || true")
    if not present:
        comp_here = "cabinet-companion.service" in _q(
            t, "systemctl list-unit-files cabinet-companion.service "
               "--no-legend --no-pager 2>/dev/null || true")
        say("   cabinet-sas.service does not exist on %s — that is state "
            "NEVER-PROVISIONED: there is no leg to switch on." % t.name)
        if comp_here:
            say("   ↳ %s" % _C11_BANNER)
        say("   ↳ SETUP, not repair: deploy/smib_setup.sh <user>@<pi> from "
            "the hub clone (idempotent: UART + venv + tree + unit in one "
            "pass), reboot the Pi, then the machine-side checklist.")
        say("❌ %s: refused — NEVER-PROVISIONED; run smib_setup.sh first. "
            "Nothing was changed." % (sat or t.name))
        return False

    # 2. HUB-PARKED is a hub-side SWITCH, not a unit state — enable --now
    # would only park again on the next report. Tiles carry the truth in
    # full mode; in degraded mode the journal's parking line is the tell.
    ents0 = _sas_entries(st, sat, peer)
    leg = sat or (ents0[0][1].get("smibId") if ents0 else None) or t.name
    parked = [k for k, e in ents0 if e.get("sasEnabled") is False]
    if not st:
        pk = _q(t, "journalctl -u cabinet-sas -b --no-pager -o cat "
                   "2>/dev/null | grep -c 'parking the poll loop' "
                   "|| true").strip()
        if pk.isdigit() and int(pk) > 0:
            parked = ["journal: 'SAS DISABLED by hub — parking the poll "
                      "loop'"]
    if parked:
        say("   this leg is HUB-PARKED (%s) — the unit side is fine."
            % "; ".join(parked))
        signpost_hub_park(leg)
        say("❌ %s: refused — HUB-PARKED is fixed in the web UI, never "
            "here. Nothing was changed." % leg)
        return False

    # ⚑ SD-health refusal: enable --now writes unit symlinks, and
    # a rootfs the kernel remounted READ-ONLY cannot hold them — name the
    # dying card, not the symptom it would cause.
    if _sd_ro(t):
        say("   ❌ rootfs on %s is mounted READ-ONLY — the kernel does that "
            "when the SD card is failing; write verbs refuse on a sick card "
            "(image a replacement first)." % t.name)
        say("❌ %s: refused — SD card unhealthy (read-only rootfs). Nothing "
            "was changed." % leg)
        return False

    enab = _q(t, "systemctl is-enabled cabinet-sas "
                 "2>/dev/null || true").strip()
    active = _q(t, "systemctl is-active cabinet-sas "
                   "2>/dev/null || true").strip()
    say("   unit state: %s / %s" % (enab or "?", active or "?"))

    # 3. the port, from the unit's OWN ExecStart (never guessed), then the
    # holder check. A running cabinet-sas holds its own port — that is not
    # contention, so its MainPID is excused; anything ELSE (a bench poll
    # mid-dance, a zombie casinonet-sas) must never be yanked out from
    # under. No fuser on the box reads as no holders — honest enough:
    # enable --now against a busy port is the quiet retry loop, not damage.
    unit = _unit_facts(_q(t, "systemctl cat cabinet-sas "
                             "2>/dev/null || true"))
    port = _sas_port(unit["exec"].split())
    mainpid = _q(t, "systemctl show -p MainPID --value cabinet-sas "
                    "2>/dev/null || true").strip()
    pids = [p for p in _q(t, "fuser %s 2>/dev/null || true" % port).split()
            if p.isdigit()]
    holders = [p for p in pids if p != mainpid]
    if holders:
        names = _q(t, "ps -o pid=,args= -p %s 2>/dev/null || true"
                   % ",".join(holders)).strip()
        say("   ❌ %s is held by pid(s) %s%s"
            % (port, ", ".join(holders),
               (" — " + "; ".join(names.splitlines())[:120]) if names
               else ""))
        say("      ↳ a bench poll mid-dance or a zombie casinonet-sas must "
            "not be yanked — stop the holder first (the smib doctor's "
            "unit-census and serial-config rows name the usual suspects).")
        say("❌ %s: refused — something else holds %s. Nothing was changed."
            % (leg, port))
        return False
    if pids:
        say("   ✅ %s held only by cabinet-sas itself (pid %s)"
            % (port, mainpid))
    else:
        say("   ✅ %s is free" % port)

    say("")
    say("   plan (on %s):" % t.name)
    say("      sudo systemctl enable --now cabinet-sas")
    say("      verify: /api/status tile for this leg fresh "
        "(reportAgeSec 0-1) within ~15 s")
    if dry_run:
        say("   (dry-run) stopping here — nothing was changed.")
        return False
    if not confirm("Bring the SAS leg up on %s?" % t.name, assume_yes):
        say("❌ %s: not confirmed — nothing was changed." % leg)
        return False

    rc, out = t.run("sudo systemctl enable --now cabinet-sas",
                    check=False, timeout=60)
    session["touched"].append(("smib", leg, peer))
    if rc != 0:
        say("❌ %s: systemctl enable --now failed (rc=%d): %s"
            % (leg, rc, out.strip()[-200:] or "(no output)"))
        return False

    def _fresh(stx):
        # fresh = the hub heard this leg JUST now (the tile auto-appears
        # on the first report — within ~1s of a live service starting)
        for key, e in _sas_entries(stx, sat, peer):
            age = e.get("reportAgeSec")
            if not e.get("stale") and isinstance(age, (int, float)) \
                    and age <= 2:
                return (key, e)
        return None

    hit, last = _verify_wait(_fresh, bound=15)
    if hit:
        key, e = hit
        say("✅ %s SAS leg is back on the floor — tile %s fresh "
            "(reportAgeSec %s%s)."
            % (leg, key, e.get("reportAgeSec"),
               "" if e.get("online") else
               "; machine leg still dark — the first-contact bench check "
               "settles machine-side vs wiring"))
        return True
    if "__error__" in last:
        # degraded — the tile cannot testify; unit + journal stand in and
        # the verify is honestly PARTIAL.
        time.sleep(2)
        act = _q(t, "systemctl is-active cabinet-sas "
                    "2>/dev/null || true").strip()
        beat = _q(t, "journalctl -u cabinet-sas -b --no-pager -o cat -n 3 "
                     "2>/dev/null || true").strip()
        if act == "active" and beat:
            say("✅ %s SAS leg is up (PARTIAL verify — hub API dark: unit "
                "active + journal alive; recheck the tile when the hub is "
                "back)." % leg)
            return True
        say("❌ %s: unit is %s after enable --now (journal %s) — run the "
            "smib doctor." % (leg, act or "?",
                              "alive" if beat else "empty"))
        return False
    say("❌ %s: no fresh tile within 15 s — the unit is on, but the floor "
        "does not hear it; run the smib doctor (report leg: --hub knob, "
        "network, hub ingest)." % leg)
    return False


def verb_companion_restart(t, comp, session, assume_yes=False, dry_run=False):
    """C2 [y/N] — restart cabinet-companion: the wedged-PN532 reset (proven
    on the floor 07-29). Over `t`:
      1. probe: unit present (absent → signpost companion_setup.sh); then
         ⚑ SD health — refuse a read-only rootfs (write-verb law);
      2. show the plan; --dry-run stops here; confirm [y/N];
      3. `sudo systemctl restart cabinet-companion`;
      4. verify: /api/status companions entry fresh AND readerOk true within
         a bounded wait (~15 s). readerOk CAN LIE (reference law) — a true
         is "reader claims OK", a persistent false after restart = say so
         and point at C1's wiring/i2c probes; degraded mode → journal check
         + partial-verify warning.
    Appends ("companion", companionId, peer) to session["touched"]. ONE
    outcome line. Returns True on verified restart. Caller holds the lock."""
    step("Restart cabinet-companion: %s" % (comp or t.name))
    status = api_status()
    st = status if "__error__" not in status else {}
    peer = _peer_of(t)
    phase = _tournament_phase(status)
    if phase:
        say("❌ tournament is %s — restarts are REFUSED until it finishes. "
            "Nothing was changed." % phase)
        return False

    # can the box answer at all? — same gate as S2: silence is never absence
    if not t.reachable():
        say("   ❌ %s did not answer a single ssh probe — powered off, off "
            "the wire, or the hub key is not authorized there." % t.name)
        say("❌ %s: refused — UNREACHABLE; nothing to restart until the box "
            "answers. Nothing was changed." % (comp or t.name))
        return False

    present = "cabinet-companion.service" in _q(
        t, "systemctl list-unit-files cabinet-companion.service "
           "--no-legend --no-pager 2>/dev/null || true")
    if not present:
        say("   cabinet-companion.service does not exist on %s — nothing "
            "to restart (NEVER-PROVISIONED)." % t.name)
        say("   ↳ SETUP, not repair: deploy/companion_setup.sh <user>@<pi> "
            "from the hub clone builds overlay + tree + unit in one "
            "idempotent pass (RFID tier only — no SAS).")
        say("❌ %s: refused — no companion unit here. Nothing was changed."
            % (comp or t.name))
        return False

    # ⚑ SD-health refusal — the write-verb law, uniformly: a card
    # in its read-only death state gets a replacement, not a repair.
    if _sd_ro(t):
        say("   ❌ rootfs on %s is mounted READ-ONLY — the kernel does that "
            "when the SD card is failing; write verbs refuse on a sick card "
            "(image a replacement first)." % t.name)
        say("❌ %s: refused — SD card unhealthy (read-only rootfs). Nothing "
            "was changed." % (comp or t.name))
        return False

    cid0, ent0 = _comp_entry(st, comp, peer)
    name = comp or cid0 or t.name
    if ent0:
        say("   hub's last word: %s, readerOk=%s%s"
            % ("fresh" if not ent0.get("stale") else "STALE",
               ent0.get("readerOk"),
               (", lastError=%s" % str(ent0.get("lastError"))[:60])
               if ent0.get("lastError") else ""))

    say("")
    say("   plan (on %s):" % t.name)
    say("      sudo systemctl restart cabinet-companion")
    say("      verify: companions row fresh + readerOk within ~15 s "
        "(true = the reader CLAIMS ok — readerOk can lie; a real tap is "
        "the judge)")
    if dry_run:
        say("   (dry-run) stopping here — nothing was changed.")
        return False
    if not confirm("Restart cabinet-companion on %s?" % t.name, assume_yes):
        say("❌ %s: not confirmed — nothing was changed." % name)
        return False

    rc, out = t.run("sudo systemctl restart cabinet-companion",
                    check=False, timeout=60)
    session["touched"].append(("companion", name, peer))
    if rc != 0:
        say("❌ %s: systemctl restart failed (rc=%d): %s"
            % (name, rc, out.strip()[-200:] or "(no output)"))
        return False

    def _ok(stx):
        _c, e = _comp_entry(stx, comp, peer)
        return e if (e and not e.get("stale") and e.get("readerOk")) \
            else None

    hit, last = _verify_wait(_ok, bound=15)
    if hit:
        say("✅ %s restarted — reporting fresh, reader claims OK (readerOk "
            "can lie: judge by a real tap)." % name)
        return True
    if "__error__" in last:
        # degraded — the daemon logs 'PN532 ready' on a good start (and
        # 'PN532 not answering' on a bad one); the fresh tail stands in.
        time.sleep(3)
        act = _q(t, "systemctl is-active cabinet-companion "
                    "2>/dev/null || true").strip()
        tail = _q(t, "journalctl -u cabinet-companion --no-pager -o cat "
                     "-n 15 2>/dev/null || true")
        if "PN532 ready" in tail:
            say("✅ %s restarted (PARTIAL verify — hub API dark: journal "
                "says 'PN532 ready'; recheck readerOk when the hub is "
                "back)." % name)
            return True
        say("❌ %s: hub API dark and the fresh journal does not say "
            "'PN532 ready' (unit %s) — run the companion doctor: it probes "
            "the wiring/DIP switches and the i2c bus." % (name, act or "?"))
        return False
    _c, e = _comp_entry(last, comp, peer)
    if e and not e.get("stale") and e.get("readerOk") is False:
        say("❌ %s: restarted and reporting, but readerOk is STILL false — "
            "a restart was not the fix; run the companion doctor: wiring/DIP "
            "switches, the i2c node, and the reader at 0x24." % name)
    else:
        say("❌ %s: no fresh companions row within 15 s after restart — "
            "run the companion doctor (crash loop? report leg?)." % name)
    return False


def signpost_hub_park(target=None):
    """S3 [signpost] — HUB-PARKED is fixed in the web UI, never here."""
    step("Where to switch this SAS leg back on")
    say("   This leg is switched off from the hub, on purpose. The Pi polls"
        "\n   fine; on every report the hub tells it to park, so the floor"
        "\n   honestly shows the leg as not polling. That is a switch, not a"
        "\n   fault, and the hub's own web page owns it — it has the confirm"
        "\n   and the reconcile logic that go with flipping it. This tool"
        "\n   never writes to the hub (its write list is EMPTY), so it will"
        "\n   not flip it for you. Here is where it lives:")
    say("")
    say("      Switchboard ▸ Machines ▸ %s ▸ SAS toggle"
        % (target or "<machine>"))
    say("")
    say("   Leave-state reminder: un-parking puts the machine back on the"
        "\n   floor — never leave the BB2 enabled at 0 credits, and never"
        "\n   re-enable a machine someone LOCKED.")
    return {"target": target, "codes": ["HUB-PARKED"], "signpost": True}


def finish_pass(session):
    """The quit hook — a session never ends ambiguous. Contract: re-poll
    every device in session["touched"] (deduped, in touch order) and print
    ONE final ✅/❌ line each from live evidence (status tile / readerOk),
    plus the leave-state reminder when a SAS machine was touched. Nothing
    touched → exactly one no-op line. Args: the run's session dict
    ({"touched": [(kind, name, peer)]}). Returns None."""
    if not session.get("touched"):
        say("\nFinish pass: nothing was touched this session.")
        return
    step("Finish pass — re-polling every touched device")
    seen, order = set(), []
    for item in session["touched"]:
        if item not in seen:
            seen.add(item)
            order.append(item)
    st = api_status()
    dark = "__error__" in st
    if dark:
        say("   ⚠️ hub API dark — no tile evidence to close on; each "
            "verb's own verify line above is tonight's best answer.")
    for kind, name, peer in order:
        if dark:
            say("   ❌ %s %s — cannot re-poll (status dark)."
                % (kind, name))
        elif kind == "smib":
            # "still on the floor" = a tile the hub hears NOW (not stale);
            # the 0-1s freshness bar was the bring-up verify's job.
            ents = _sas_entries(st, name, peer)
            live = [k for k, e in ents if not e.get("stale")]
            if live:
                on = [k for k, e in ents if e.get("online")]
                say("   ✅ smib %s — tile fresh (%s)%s."
                    % (name, ", ".join(live),
                       "" if on else " · machine leg dark"))
            else:
                say("   ❌ smib %s — %s."
                    % (name, "tile went stale again" if ents
                       else "no tile ever appeared"))
        else:
            cid, e = _comp_entry(st, name, peer)
            if e and not e.get("stale") and e.get("readerOk") is not False:
                say("   ✅ companion %s — reporting, reader claims OK "
                    "(readerOk can lie)." % (cid or name))
            elif e and not e.get("stale"):
                say("   ❌ companion %s — reporting, readerOk=false."
                    % (cid or name))
            else:
                say("   ❌ companion %s — not reporting." % (cid or name))
    if any(kind == "smib" for kind, _n, _p in order):
        say("")
        say("   Leave-state (a SAS machine was touched): judge credits via "
            "SAS 0x1A; DISABLE the machine when you leave the bench; never "
            "leave the BB2 enabled at 0 credits, and never re-enable a "
            "machine someone LOCKED.")


# ---------------------------------------------------------------------------
# MENU — the floor, then the device, then its fix
# ---------------------------------------------------------------------------
# Two screens and one prompt. The ROOT is the floor: every device, worst
# first, in plain sentences, one number space. Picking a number opens that
# DEVICE — its doctor runs, the findings print, and the actions THOSE
# FINDINGS IMPLY are numbered directly underneath them. Sub-pages (all
# checks, a journal tail, a signpost, the one-liners) are the third and last
# level and open nothing below themselves.
#
# Three rules the rest of this section only implements:
#   * A NUMBER NEVER CHANGES ANYTHING. A number opens a screen or shows a
#     plan; only an explicit y at a confirm() prompt mutates. That is what
#     makes worst-first renumbering safe — the list can reorder between two
#     glances and a stale keystroke still cannot fire a repair.
#   * AN ACTION IS RENDERED ONLY IF IT CAN RUN RIGHT NOW, here, given this
#     verdict. A blocked one is replaced by a single sentence saying why and
#     when it comes back — never a menu item that does nothing.
#   * THE SCREEN IS NEVER CLEARED. Every redraw appends, so scrollback is
#     evidence and the transcript is the same thing on disk. That is also
#     what keeps the tool honest under TERM=dumb and through a pipe.

_WIDTH = 78                     # every screen fits an 80-column terminal
# Plan codes (S1.4, C2, H14 …) are how the source file is ORGANISED, not how
# a floor is described. They are stripped at RENDER TIME ONLY: the strings
# inside every doctor dict — checks[].probe, findings[].evidence/hint — stay
# byte-identical, so --json and the gate's probe-command transcript are
# untouched, and a doctor written next year cannot leak one into a screen.
_PLAN_PREFIX = re.compile(r"^[SCHFD]\d+(\.\d+)?\s+")
# S/C/H only: D and F would eat real evidence (the PN532's "no D5 4B for 50
# polls" is a wire fact, not a plan code), and no D/F code is left in any
# printed string — they only survive in docstrings, where they belong.
_PLAN_WORD = re.compile(r"\b[SCH]\d{1,2}(\.\d+)?\b[/-]?")
_SEV_RANK = {"fatal": 0, "warn": 1, "info": 2}
_KIND_RANK = {"machine": 0, "smib": 1, "companion": 2, "hub": 3, "wire": 4}


def _b(text):
    """Bold. say() strips it back out when stdout is not a terminal."""
    return "\033[1m%s\033[0m" % text


def _plain(text):
    """Doctor prose, with its plan codes taken back out. See _PLAN_WORD."""
    return re.sub(r"\s{2,}", " ",
                  _PLAN_WORD.sub("", str(text))).replace("( ", "(").strip()


def _flagcell(flag):
    """A fixed two-cell flag column. ❌ ✅ ⚠️ are double-width glyphs and •
    is not, so without this pad every name after a • row shifts left one."""
    return flag if flag in ("❌", "✅", "⚠️") else "%s " % flag


def _wrapped(text, col, width=_WIDTH):
    """`text` wrapped to fit starting at column `col`, continuation lines
    landing under it. _print_rows never wrapped at all, so a long detail ran
    straight past 80 columns on the one screen that must stay readable.
    Hyphens do not break: this tool's nouns are `smib-2831b6`, `cabinet-sas`
    and `/home/aj/.ssh/smib`, and half of one on each line is unreadable and
    un-pasteable. A token longer than the whole width still breaks — the
    column law wins over everything."""
    return textwrap.wrap(str(text), width=max(width - col, 24),
                         break_on_hyphens=False) or [""]


def _rowlines(num, flag, name, peer, detail, w_name, w_peer):
    """One board row: `   N) F name  peer  detail`, one number column
    everywhere, detail wrapped under itself."""
    col = 5 + 1 + 2 + 1 + w_name + 2 + w_peer + 2
    parts = _wrapped(detail, col)
    head = "%4s) %s %-*s  %-*s  " % (num, _flagcell(flag), w_name, name,
                                     w_peer, peer)
    return [head + parts[0]] + [" " * col + p for p in parts[1:]]


_FLAGS = ("✅", "❌", "⚠️", "•")


def _indent(text, pad=8):
    """Indented, wrapped prose. A line that OPENS with a flag hangs its
    continuation past the glyph: without that, the second half of a wrapped
    `✅ running <long commit subject>` lands in the flag column and reads as
    a second, flagless bullet."""
    text = str(text)
    hang = 3 if text.startswith(_FLAGS) else 0
    out = _wrapped(text, pad + hang)
    return [" " * pad + out[0]] + [" " * (pad + hang) + l for l in out[1:]]


def _pad(lines, pad=3):
    """Indent a block, WRAPPING its prose. The `Show how to fix it` pages are
    built from source-concatenated sentences; unwrapped they ran to 187
    columns on the leaf screens the whole flow exists to land on. A line
    indented six or more (a `_cmds` block) is what pastes and runs, so it is
    moved and never re-flowed; a bullet keeps a hanging indent."""
    out = []
    for l in lines:
        if not l:
            out.append("")
            continue
        body = l.lstrip()
        lead = len(l) - len(body)
        if len(l) + pad <= _WIDTH or lead >= 6:
            out.append(" " * pad + l)
            continue
        hang = lead + (2 if body[:1] in ("·", "•", "-") else 0)
        parts = _wrapped(body, pad + hang)
        out.append(" " * (pad + lead) + parts[0])
        out += [" " * (pad + hang) + p for p in parts[1:]]
    return out


def _clip(text, n):
    """Shorten with an ellipsis, never mid-word-and-silent. A quoted error
    chopped clean off reads as a corrupted string rather than a shortened
    one — on an error message that is actively misleading."""
    text = str(text)
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _cmds(*cmds):
    """A copy-paste block — indented, blank line either side. A command too
    long for 80 columns breaks on a shell continuation, so what is on the
    screen is still exactly what pastes and runs."""
    # Wrapped THREE columns narrower than the screen: a command block is
    # often re-indented by _pad on the way to a fix page, and a shell
    # continuation that fits here but not there is an 81-column line.
    width = _WIDTH - 3
    out = [""]
    for c in cmds:
        line = "      %s" % c
        while len(line) > width:
            cut = line.rfind(" ", 0, width - 2)
            if cut <= 8:
                break
            out.append(line[:cut] + " \\")
            line = "        " + line[cut + 1:]
        out.append(line)
    return out + [""]


# --- who and where an ssh line points at -----------------------------------
# Six printed ssh lines used to interpolate getpass.getuser(). That is the
# account on the box RUNNING this tool — the satellite default, and off the
# hub simply the laptop's login. A headline copy-paste that fails on paste is
# worse than one that visibly asks to be filled in.

def _login(dev):
    """The account a DEVICE answers ssh as: what the satellite reported for
    itself, and only then this box's own login. A lease row (took an address,
    never reported) has no reported login at all — indexing dev["user"] on one
    is what crashed the menu the moment a lease row was opened."""
    return (dev or {}).get("user") or _u.SAT_USER


def _hub_login(ctx):
    """The account to ssh into THE HUB as. On the hub this process's own
    login IS the hub's login — real evidence, and the setup scripts already
    take user@host. From anywhere else nothing here knows it, so the line
    carries a placeholder to fill in rather than a wrong name to paste."""
    return _u.SAT_USER if ctx.get("place") in ("hub", "degraded",
                                               "wrong-clone") else "<you>"


def _hub_addr():
    """The address to ssh to for the hub. On the hub HUB_URL stays loopback,
    which is useless in a "from another box" line, so the slot-segment
    address the hub always carries is the honest one there."""
    host = _hub_host().split(":")[0]
    return SLOT_PREFIX + "2" if host in ("127.0.0.1", "localhost", "::1",
                                         "") else host


_UNIT_OF = {"companion": "cabinet-companion", "hub": "cabinet-g2s"}


def _unit_for(kind):
    """The systemd unit a box actually runs. The hub runs cabinet-g2s;
    offering it a `cabinet-sas` journal was a page that could only ever
    answer "(journal empty or not readable here)"."""
    return _UNIT_OF.get(kind, "cabinet-sas")


# --- the prompt ------------------------------------------------------------
# The whole vocabulary lives in one function so it can never drift between
# screens: numbers pick from the list you are looking at, and b q r ? are the
# only letters there will ever be.

def _correction(raw, level, n=1, refresh=True):
    """One phrasing, parameterised by the screen. A screen with nothing to
    pick must not tell you to pick from a list that is not there — and the
    line no longer OPENS with `?`, which is also the help key and read as an
    offer of help rather than a correction. The literal substring 'pick a
    number' is the contract every list screen's correction line keeps."""
    if n <= 0:
        return ("   %r — nothing to pick on this screen. b goes back, q "
                "quits." % raw)
    outs = ["r to check again"] if (level == 0 and refresh) else []
    if level > 0:
        outs = ["b for back"]
    return ("   %r — pick a number from the list%s, q to quit."
            % (raw, "".join(", " + o for o in outs)))


def _read_choice(n, level, typed=False, refresh=True):
    """THE prompt. Returns "quit" | "back" | "redraw" | "refresh" | "help" |
    ("pick", i) | ("host", text), or None for a corrected typo — which the
    caller re-prompts on WITHOUT redrawing, so a fat finger never scrolls the
    evidence off the screen. `n` = numbered items on this screen, `level` =
    depth (0 never renders b and never exits on it), `typed` = this screen
    takes an address (the floor and the sweep summary, stated in the footer),
    `refresh` = this screen has something to re-check (false only where
    looking again is switched off, and then r is not advertised either).
    EOF is a quit, so a piped gate run can never hang here."""
    try:
        raw = input("> ").strip()
    except EOFError:
        return "quit"
    low = raw.lower()
    if low in ("q", "quit"):
        return "quit"
    if raw == "":
        return "redraw"                  # never navigation: a stray Enter is
    if low == "?":                       # always safe and always shows you
        return "help"                    # where you are
    if low == "r" and refresh:
        return "refresh"
    if low == "b":
        if level > 0:
            return "back"
        say("   You are at the top. b goes back from a screen; q quits.")
        return None
    if raw.isdigit():
        if 1 <= int(raw) <= n:
            return ("pick", int(raw))
        # A bare number is never a host — an out-of-range pick gets the
        # correction line, not a doomed ssh dial.
        say(_correction(raw, level, n, refresh))
        return None
    if typed and "." in raw and len(raw) >= 2:
        return ("host", raw)             # a typed peer/IP rides ssh
    # A stray single keystroke is never a host either.
    say(_correction(raw, level, n, refresh))
    return None


def _footer(level, typed=False, back="back to the floor", n=1, refresh=True):
    """One affordance phrasing, every screen, same `key) label` form — and
    it advertises exactly the keys THIS screen answers: no "Pick a number."
    where there is no list, no `r` where there is nothing to re-check, and
    never a silent `?`/`r` that works but is not offered."""
    keys = []
    if refresh:
        keys.append("r) check again")
    if level > 0:
        keys.append("b) %s" % (back if level == 1 else "back"))
    keys += ["?) keys", "q) quit"]
    lead = "Pick a number." if n > 0 else ""
    line = " " + ("%s   " % lead if lead else "") + "   ".join(keys)
    return line + ("\n or type an address" if typed else "")


def _keys_page(ctx):
    lines = [
        "",
        _b("   The keys"),
        "",
        "   numbers   pick from the list you are looking at",
        "   b         back one screen (not shown on the floor — nothing is",
        "             behind it)",
        "   r         check again — re-scan the floor, re-run this device's",
        "             doctor, or re-run the report you are reading",
        "   Enter     redraw this screen",
        "   q         quit — runs the finish pass on anything you touched",
        "   ?         this",
    ]
    return _page(ctx, lines)


# --- the loop --------------------------------------------------------------

def _pump(ctx, build, level, typed=False, back="back to the floor",
          on_refresh=None, refresh=True, live=False):
    """THE key loop, one implementation for every screen. `build(fresh)`
    returns (lines, actions); an action is {"label", "run"} whose run()
    returns "quit" or anything else. b never re-runs work — screens redraw
    from cache and `r` is the only re-probe.

    `live=True` means build() ALREADY put its lines on screen as it worked
    (a report that probes Pis narrates instead of stalling) — so the first
    pass prints only the footer. Enter still redraws from the kept copy."""
    state = build(False)
    shown = live
    draw = True
    while True:
        if draw:
            if not shown:
                for ln in state[0]:
                    say(ln)
            shown = False
            say("")
            say(_footer(level, typed, back, len(state[1]), refresh))
            draw = False
        got = _read_choice(len(state[1]), level, typed, refresh)
        if got == "quit":
            return "quit"
        if got is None:                  # corrected typo — no redraw
            continue
        if got == "redraw":
            draw = True
            continue
        if got == "back":
            return None
        if got == "refresh":
            # r means the same thing on every screen — "check again" — but
            # what there is to re-check differs: the floor, this device's
            # doctor, or (at the front door) whether a hub has turned up.
            if on_refresh is not None and on_refresh() == "quit":
                return "quit"
            state = build(True)
            shown = live          # a live rebuild narrated itself again
            draw = True
            continue
        if got == "help":
            if _keys_page(ctx) == "quit":
                return "quit"
            draw = True
            continue
        if got[0] == "pick":
            act = state[1][got[1] - 1]
            if act["run"]() == "quit":
                return "quit"
            # A repair re-diagnoses in place; everything else redraws from
            # cache, so nobody is ever left staring at output with no prompt.
            state = build(True) if act.get("recheck") else state
            draw = True
            continue
        if got[0] == "host":
            if device_screen(ctx, _typed_device(got[1])) == "quit":
                return "quit"
            draw = True


def _page(ctx, lines, level=2):
    """A depth-2 sub-page: print, then b/q. It opens nothing below itself,
    and there is nothing on it to check again — so `r` is not offered."""
    return _pump(ctx, lambda fresh: (lines, []), level, refresh=False)


class _Tee(object):
    """stdout that ALSO keeps a copy. Not a buffer: a report that probes a
    Pi over ssh takes tens of seconds, and swallowing its output until the
    work finished turned every progress line into an epitaph — the operator
    watched a dead screen and then got told what had already happened."""

    def __init__(self, real):
        self.real = real
        self.buf = io.StringIO()

    def write(self, s):
        self.real.write(s)
        self.buf.write(s)
        return len(s)

    def flush(self):
        self.real.flush()

    def isatty(self):
        return getattr(self.real, "isatty", lambda: False)()


def _capture(fn, *args, **kw):
    """Run an existing report LIVE and keep its lines. say() writes the
    transcript itself, so teeing here touches only what the screen shows:
    the work narrates as it happens, and the kept copy is what Enter
    redraws."""
    tee = _Tee(sys.stdout)
    with contextlib.redirect_stdout(tee):
        fn(*args, **kw)
    return tee.buf.getvalue().rstrip("\n").split("\n")


def _run_page(ctx, fn, *args, **kw):
    """A sub-page whose body is an existing report, printed verbatim. Its
    lines are kept, so Enter redraws what it says it will: a report leaf that
    answered a bare footer was the one screen where Enter lied — and every
    report leaf in the tool is one of these."""
    cache = {}

    def build(fresh):
        if fresh or "lines" not in cache:
            say("")
            cache["lines"] = [""] + _capture(fn, *args, **kw)
        return (cache["lines"], [])

    # live: build() only ever runs when there is work to do (first entry and
    # `r`), and it narrates while it works — so _pump adds just the footer
    # after it. Enter redraws from the kept copy without calling build at all.
    return _pump(ctx, build, 2, live=True)


# --- the code → sentence → action table ------------------------------------
# Menu labels are not hand-written strings; they are a lookup on the
# classification code the doctors already emit. That is what makes "no dead
# entries" mechanical instead of a matter of discipline: a code with no offer
# renders no numbered item, and a code missing from this table falls back to
# the doctor's own rendering with no numbered child — so a new code can never
# mint a menu entry that does nothing. The CODE itself still prints, trailing
# the evidence, greppable and identical to --json; it is never a label.

def _setup_block(dev):
    script = "companion_setup.sh" if dev["kind"] == "companion" \
        else "smib_setup.sh"
    return (["That is setup, not repair, and it is idempotent. From the hub:"]
            + _cmds("deploy/%s %s@%s" % (script, _login(dev), dev["peer"]))
            + ["then reboot the Pi and do the machine-side checklist."])


def _retire_block(dev):
    """Which pre-rename unit is in the way follows the Pi's duty — a reader
    and a SAS leg carry different ghosts."""
    comp = dev["kind"] == "companion"
    return (["The old unit is retired by the setup script that replaced it, "
             "and it keeps the file as .retired. From the hub:"]
            + _cmds("deploy/%s %s@%s"
                    % ("companion_setup.sh" if comp else "smib_setup.sh",
                       _login(dev), dev["peer"]))
            + ["Or, on the Pi itself, just switch it off:"]
            + _cmds("sudo systemctl disable --now casinonet-%s"
                    % ("companion" if comp else "sas")))


def _rerun_setup_block(why):
    def build(dev):
        script = "companion_setup.sh" if dev["kind"] == "companion" \
            else "smib_setup.sh"
        return ([why, ""]
                + ["Re-running the setup script rewrites it, from the hub:"]
                + _cmds("deploy/%s %s@%s" % (script, _login(dev),
                                             dev["peer"])))
    return build


def _hub_address_block(dev):
    return (["The Pi has to be told where the hub is. The setup script's "
             "default is right on the wired slot segment; the explicit form "
             "is --hub http://192.168.50.2:8081 in the unit's ExecStart."]
            + _cmds("deploy/smib_setup.sh %s@%s" % (_login(dev), dev["peer"]))
            + ["A Pi that took its address from the slot switch derives the "
               "hub by itself — that is the zero-config keystone, so a "
               "hand-set address is the fallback, not the goal."])


def _uart_block(dev):
    return (["Two lines in the Pi's config.txt, then a reboot — config.txt "
             "is read at boot, so nothing changes until it restarts:"]
            + _cmds("enable_uart=1", "dtoverlay=disable-bt")
            + ["The setup script writes both. From the hub:"]
            + _cmds("deploy/smib_setup.sh %s@%s" % (_login(dev), dev["peer"])))


def _console_block(dev):
    return (["Strip the console= entry that names the serial port out of "
             "the Pi's cmdline.txt and reboot. The setup script's sed does "
             "exactly that:"]
            + _cmds("deploy/smib_setup.sh %s@%s" % (_login(dev), dev["peer"])))


def _getty_block(dev):
    return (["On the Pi, mask the login prompt that is sitting on the wire:"]
            + _cmds("sudo systemctl mask --now serial-getty@ttyAMA0.service")
            + ["The setup script masks it too, so a re-run is the other "
               "path."])


def _dialout_block(dev):
    return (["One command on the Pi, then a reboot for it to stick:"]
            + _cmds("sudo usermod -aG dialout %s" % _login(dev)))


def _update_block(dev):
    return (["Code goes to the fleet one way — the updater, from the hub. "
             "It restarts and verifies everything it touches:"]
            + _cmds("deploy/update.py")
            + ["Never a private rsync: hub and satellite must run the same "
               "tree or the SAS import fails in the quiet retry loop."])


def _segment_block(dev):
    return (["Satellites are WIRED-ONLY on the slot segment. Plug the Pi "
             "into the slot switch and let the hub's own DHCP lease it — "
             "that IS the zero-config keystone, so never hand-configure "
             "the NIC.",
             "",
             "If it is already plugged in, the hub's DHCP service is the "
             "next suspect: open the hub from the floor."])


def _wiring_block(dev):
    return (["The reader answers on the I2C bus at 0x24, or it does not. "
             "When the bus node exists and 0x24 is silent, it is wiring or "
             "the DIP switches — never software:",
             "",
             "   · PN532 DIP switches set to I2C",
             "   · SDA to GPIO23, SCL to GPIO24, 3.3V, GND",
             "",
             "Companion/README.md has the photo and the pinout."])


def _bind_block(dev):
    return (["A reader with no machine assigned still reads cards; the taps "
             "just land nowhere. Assign it on the hub's own web page:",
             "",
             "      Switchboard ▸ Machines ▸ <machine> ▸ card reader",
             "",
             "A reader that shares a Pi with a SAS leg binds itself."])


def _host_point_block(dev):
    return (["Set the G2S host ON THE MACHINE, by hand, in its operator "
             "menu, as three separate URI segments:"]
            + _cmds("http://       192.168.50.2       :8081/G2S")
            + ["then re-enable G2S in the debug menu. There is no "
               "DHCP-delivered host apply on this family — closed, and "
               "wire-proven."])


def _debug_pads_block(dev):
    return (["On a Pi 5, /dev/serial0 is the DEBUG pads, never the machine "
             "wire. The unit has to name /dev/ttyAMA0 — the GPIO14/15 "
             "header UART. The setup script gets it right:"]
            + _cmds("deploy/smib_setup.sh %s@%s" % (_login(dev), dev["peer"])))


def _port_block(dev):
    return (["The port appears when the UART is enabled in config.txt and "
             "the Pi has rebooted:"]
            + _cmds("enable_uart=1", "dtoverlay=disable-bt")
            + ["On a board with no PL011 there is no port and no SAS."])


def _cd_block(dev):
    return (["Work in the tree the hub actually runs:"]
            + _cmds("cd %s && ./cabinetconfig"
                    % (dev.get("wd") or "<the tree the unit runs>")))


# kind: verb-smib-up | verb-comp-restart | signpost-park | signpost-bench |
#       block | journal | jump-hub. A code with no "act" renders no item.
CODE_UI = {
    "UNREACHABLE": {
        "head": "This box did not answer at all",
        "after": lambda d: [
            "",
            "   Check in this order",
            "   · power, the network cable, and that the hub leased it an "
            "address",
            "   · then the hub's ssh key on that box:",
            "         ssh-copy-id -i ~/.ssh/smib.pub %s@%s"
            % (_login(d), d["peer"])],
    },
    "NEVER-PROVISIONED": {
        "head": "This Pi has never been set up to talk to a machine",
        "after": lambda d: [""] + _pad(_setup_block(d)),
    },
    "UNIT-OFF": {
        "head": "The SAS service on this Pi is switched off",
        "act": {"kind": "verb-smib-up", "label": "Turn the SAS leg back on",
                "desc": "Runs sudo systemctl enable --now cabinet-sas on "
                        "this Pi, then watches the hub for up to 15 s. "
                        "Shows the plan and asks first."},
    },
    "HUB-PARKED": {
        "head": "This machine's SAS is switched off on purpose, from the hub",
        "act": {"kind": "signpost-park",
                "label": "Show me where to switch it back on"},
    },
    "RENAME-CONTENTION": {
        "head": "An old service from a previous version is running and "
                "fighting the new one",
        "act": {"kind": "block", "label": "Show how to retire it",
                "build": _retire_block},
    },
    "RENAME-RESIDUE": {
        "head": "An old service file from a previous version is still "
                "installed",
        "act": {"kind": "block", "label": "Show how to retire it",
                "build": _retire_block},
    },
    "UNREWRITTEN-UNIT": {
        "head": "This Pi's SAS service still has the shipped example settings",
        "act": {"kind": "block", "label": "Show how to fix it",
                "build": _rerun_setup_block(
                    "The unit still names the placeholder account and paths "
                    "the repo ships, so it fails the moment it starts.")},
    },
    "REPORTING-DISABLED": {
        "head": "This Pi polls the machine but never tells the hub",
        "act": {"kind": "block", "label": "Show how to point it at the hub",
                "build": _hub_address_block},
    },
    "SELF-REPORT": {
        "head": "This Pi reports to itself, so nothing ever reaches the hub",
        "act": {"kind": "block", "label": "Show how to point it at the hub",
                "build": _hub_address_block},
    },
    "MINI-UART-NO-PARITY": {
        "head": "This Pi's serial port cannot do the parity SAS needs",
        "act": {"kind": "block", "label": "Show the two lines and the reboot",
                "build": _uart_block},
    },
    "CONSOLE-EATS-LINE": {
        "head": "The Pi's own console is on the wire to the machine",
        "act": {"kind": "block", "label": "Show how to clear it",
                "build": _console_block},
    },
    "GETTY-CONTENTION": {
        "head": "A login prompt is being typed into your slot machine",
        "act": {"kind": "block", "label": "Show the one command",
                "build": _getty_block},
    },
    "PORT-MISSING": {
        "head": "The serial port the service wants does not exist here",
        "act": {"kind": "block", "label": "Show what makes it appear",
                "build": _port_block},
    },
    "NO-DIALOUT": {
        "head": "The service account may not open the serial port",
        "act": {"kind": "block", "label": "Show the one command",
                "build": _dialout_block},
    },
    "DEBUG-CONNECTOR": {
        "head": "This Pi 5 is pointed at its debug pads, not the machine wire",
        "act": {"kind": "block", "label": "Show the right port",
                "build": _debug_pads_block},
    },
    "TREE-MISMATCH": {
        "head": "This Pi is running different code than the hub",
        "act": {"kind": "block", "label": "Show how to get it in step",
                "build": _update_block},
    },
    "SILENT-MISMATCH": {
        "head": "This Pi is running different code than the hub",
        "act": {"kind": "block", "label": "Show how to get it in step",
                "build": _update_block},
    },
    "WRONG-SEGMENT": {
        "head": "This Pi is not on the slot network",
        "act": {"kind": "block", "label": "Show what to check",
                "build": _segment_block},
    },
    "HOME-ROUTER-DERIVED": {
        "head": "This Pi is sending its reports to your home router",
        "act": {"kind": "block", "label": "Show how to point it at the hub",
                "build": _hub_address_block},
    },
    "HUB-UNREACHABLE": {
        "head": "This Pi cannot reach the hub",
        "act": {"kind": "jump-hub", "label": "Open the hub"},
    },
    "HUB-DOWN": {
        "head": "This Pi cannot reach the hub",
        "act": {"kind": "jump-hub", "label": "Open the hub"},
    },
    "MACHINE-LEG-OFF": {
        "head": "The Pi is fine; the wire to the machine is not",
        "act": {"kind": "signpost-bench",
                "label": "Show me the first-contact bench check"},
    },
    "COMPANION-TIER": {
        "head": "This Pi reads cards only. It does not poll SAS.",
    },
    "READER-NOT-DETECTED": {
        "head": "The card reader is not detected on the wire at all",
        "act": {"kind": "block", "label": "Show the DIP switches and wiring",
                "build": _wiring_block},
    },
    "READER-WEDGED": {
        "head": "The card reader stopped answering",
        "act": {"kind": "verb-comp-restart", "label": "Restart the card "
                "reader",
                "desc": "Runs sudo systemctl restart cabinet-companion on "
                        "this Pi, then waits for the reader to come back. "
                        "Shows the plan and asks first."},
    },
    "DAEMON-CRASH-LOOP": {
        "head": "The reader service keeps crashing and restarting",
        "act": {"kind": "journal", "label": "Show the last 30 log lines — a "
                "crash loop is a stack trace"},
    },
    "NEVER-BOUND": {
        "head": "This reader is not assigned to a machine yet",
        "act": {"kind": "block", "label": "Show where to assign it",
                "build": _bind_block},
    },
    "CLOCK-SKEW": {"head": "This Pi's clock is not synced"},
    "SD-UNHEALTHY": {"head": "This box's SD card is failing"},
    "DISK-FULL": {"head": "This box's disk is full"},
    "TWO-TREE": {
        "head": "You are working in the wrong folder",
        "act": {"kind": "block", "label": "Show the folder to work in",
                "build": _cd_block},
    },
    "PARTIAL-DEPLOY": {
        "head": "The hub is running older code than this clone contains",
        "act": {"kind": "block", "label": "Show how to get it in step",
                "build": _update_block},
    },
    "EGM-HOST-POINT-UNSET": {
        "head": "A machine is still pointed at the factory address",
        "act": {"kind": "block", "label": "Show what to set on the machine",
                "build": _host_point_block},
    },
    "NO-SAS-SATELLITE-EVER-REPORTED": {
        "head": "No SAS Pi has ever reported to this hub",
    },
    "SATELLITE-SILENT": {"head": "A SAS leg has gone quiet",
                         "act": {"kind": "jump-device",
                                 "label": "Open that Pi"}},
    "MACHINE-LEG-DARK": {"head": "A SAS leg reports, but its machine "
                                 "does not",
                         "act": {"kind": "jump-device",
                                 "label": "Open that Pi"}},
}


# UNIT-OFF and NEVER-PROVISIONED are emitted by BOTH doctors and mean two
# different things depending on the Pi's duty. Telling a card reader that its
# "SAS service" is off — and offering the SAS verb — would be the same
# confidently-wrong sentence this tool exists to kill.
CODE_UI_BY_KIND = {
    "companion": {
        "UNIT-OFF": {
            "head": "The card reader's service is switched off",
            "act": {"kind": "block", "label": "Show how to switch it on",
                    "build": lambda d: (
                        ["On the Pi:"]
                        + _cmds("sudo systemctl enable --now "
                                "cabinet-companion")
                        + ["A reader that is running but not answering is a "
                           "different state — that one has a restart offered "
                           "for it."])},
        },
        "NEVER-PROVISIONED": {
            "head": "This Pi has never been set up as a card reader",
            "after": lambda d: [""] + _pad(_setup_block(d)),
        },
    },
}


def _code_ui(kind, code):
    return CODE_UI_BY_KIND.get(kind, {}).get(code) or CODE_UI.get(code)


def _rail(ctx, doc):
    """Why a repair cannot run right now — ONE sentence, printed in the
    action's place. The action comes back the moment the rail clears, and the
    verbs keep their own internal refusals regardless: the menu is never the
    only guard."""
    phase = _tournament_phase(ctx.get("status") or {})
    if phase:
        return ("Repairs are paused while the tournament is running (%s)."
                % phase)
    holder = lock_holder(ctx["root"])
    if holder:
        return "deploy/update.py is running here — repairs wait for it."
    if any(f["code"] == "SD-UNHEALTHY" and f["severity"] == "fatal"
           for f in doc.get("findings") or []):
        return ("Repairs are held: this box's SD card is failing. Image a "
                "replacement first.")
    if ctx["place"] == "wrong-clone":
        wd = os.path.dirname(ctx["identity"].get("wd") or "") or "the hub's"
        return ("Repairs are switched off in this clone — cd to %s first."
                % wd)
    return None


# --- the floor board -------------------------------------------------------

def _scan(ctx):
    """The launch board's evidence: CHEAP legs only — /api/status plus
    discover_fleet's leases, ARP and its dark-candidate probes. Never a
    doctor: ~20 ssh round-trips per box would make the one screen that has
    to be instant take a minute. UNIT-OFF / NEVER-PROVISIONED / UNREACHABLE
    are not knowable without a shell, so a row says the honest three-way
    sentence and the device screen names which."""
    say("")
    say("Looking at the floor…")
    ctx["status"] = api_status()
    fleet = discover_fleet(ctx["status"], ctx["root"], local=ctx.get("local"))
    return {"status": ctx["status"], "fleet": fleet}


def _st_of(scan):
    st = scan["status"]
    return st if isinstance(st, dict) and "__error__" not in st else {}


def _links(st):
    """Which reader and which SAS leg serve which machine. The source is
    companions[*].bindings (g2sEgmId + sasSmib) — the hub's own record of the
    link. No binding ⇒ no link is invented, ever."""
    smib_of, comp_of = {}, {}
    for cid, ent in sorted((st.get("companions") or {}).items()):
        binds = (ent or {}).get("bindings") or {}
        egm = binds.get("g2sEgmId")
        if egm:
            comp_of[cid] = egm
            if binds.get("sasSmib"):
                smib_of[binds["sasSmib"]] = egm
    return smib_of, comp_of


def _short(name, egm):
    """A machine's friendly name, or its id when the collector never gave it
    one. A cabinet is never named after a game — this only ever echoes what
    the hub was told."""
    return name if name and name != egm else egm


def _named(name, egm):
    """"the Bluebird 2" when the collector named the cabinet, plain
    "IGT_00012E492815" when they did not. A definite article in front of a
    raw factory id reads as if the tool knows something it does not."""
    who = _short(name, egm)
    return ("the %s" % who) if who != egm else who


def _dev(kind, name, peer, user=None, role="", **extra):
    d = {"kind": kind, "name": name, "peer": peer or "?",
         "user": user or _u.SAT_USER, "role": role, "sub": [],
         "key": "%s:%s" % (kind, name or peer)}
    d.update(extra)
    return d


def _typed_device(text):
    return _dev("wire", text, text, role="unknown box on the slot segment")


def _machine_of(links, kind, name):
    smib_of, comp_of = links
    return (smib_of if kind == "smib" else comp_of).get(name)


def _entries(ctx, scan):
    """Every device on the floor as one pickable row: flag, plain-language
    detail, the sub-lines a bad row needs, and what picking it opens."""
    st = _st_of(scan)
    fleet = scan["fleet"]
    links = _links(st)
    names = st.get("names") or {}
    ents = []

    if fleet["degraded"]:
        return _entries_degraded(ctx, scan)

    sas_raw = st.get("sas") or {}
    for m in fleet["machines"]:
        label = _short(m["name"], m["egmId"])
        legs = [s for s in fleet["sats"]
                if _machine_of(links, "smib", s["smibId"]) == m["egmId"]]
        dark = [s for s in legs if s["silent"]]
        sub = []
        if m["commsState"] == "onLine" and dark:
            # Dual-stack law: SAS is always the accounting authority, so a
            # silent SAS leg means the money is blind while G2S still says
            # onLine. "offline" would be the wrong word for the collector.
            s = dark[0]
            age = min((k["reportAgeSec"] or 0) for k in s["keys"])
            flag = "❌"
            detail = "the hub has stopped seeing its credits and meters"
            sub = ["Its SAS leg, %s at %s, has not reported for %s. The "
                   "machine can still show online over G2S while credits, "
                   "meters and tickets are frozen."
                   % (s["smibId"], s["peer"], _age_str(age))]
        elif m["commsState"] == "onLine":
            flag = "✅"
            detail = "online · G2S" + (" + SAS" if legs else "")
        elif m["joining"]:
            flag, detail = "⚠️", "reached the hub, not joined yet"
        else:
            flag, detail = "❌", "offline to the hub"
        ents.append({"kind": "machine", "name": label,
                     "peer": m["peer"] or "?",
                     "flag": flag, "detail": detail, "sub": sub,
                     "machine": m, "key": "machine:" + m["egmId"]})

    for s in fleet["sats"]:
        egm = _machine_of(links, "smib", s["smibId"])
        served = _named(names.get(egm) or egm, egm) if egm else None
        role = "SAS leg for %s" % served if served else "SAS leg"
        parked = [k for k in s["keys"]
                  if (sas_raw.get(k["key"]) or {}).get("sasEnabled") is False]
        sub = []
        if s["silent"]:
            age = min((k["reportAgeSec"] or 0) for k in s["keys"])
            flag = "❌"
            short = "has not reported for %s" % _age_str(age)
            detail = ("no report for %s — the Pi is down, its SAS service is "
                      "off, or its report leg is broken; open it and the "
                      "doctor will name which" % _age_str(age))
        elif parked:
            flag, short = "⚠️", "parked by the hub's own SAS switch"
            detail = short
        elif any(k["online"] for k in s["keys"]):
            flag, short = "✅", "polling, machine answering"
            detail = "%s — %s" % (role, short)
        else:
            flag, short = "⚠️", "reporting, but the machine leg is dark"
            detail = "%s — %s" % (role, short)
        ents.append({"kind": "smib", "name": s["smibId"], "peer": s["peer"],
                     "flag": flag, "detail": detail, "sub": sub,
                     "short": short,
                     "user": _u.sat_user(s), "role": role, "machine_id": egm,
                     "key": "smib:" + s["smibId"]})

    sat_users = {s["peer"]: _u.sat_user(s) for s in fleet["sats"]}
    for c in fleet["companions"]:
        egm = _machine_of(links, "companion", c["companionId"])
        served = _named(names.get(egm) or egm, egm) if egm else None
        role = "card reader on %s" % served if served \
            else "card reader — no machine bound"
        ent = (st.get("companions") or {}).get(c["companionId"]) or {}
        err = str(ent.get("lastError") or "")
        sub = []
        if not c["fresh"]:
            flag, short = "❌", "the daemon is not reporting at all"
            detail = "the reader daemon is not reporting at all"
        elif c["readerOk"] is False:
            flag, short = "⚠️", "taps are not registering"
            detail = ("card taps on %s are not registering" % served
                      if served else "card taps are not registering")
            sub = ["The reader answered once and then stopped. A restart is "
                   "the proven reset for this."]
            if err:
                sub.append('"%s"' % _clip(err, 110))
        else:
            flag, short = "✅", "reporting, reader claims OK"
            detail = "%s — %s" % (role, short)
        ents.append({"kind": "companion", "name": c["companionId"],
                     "peer": c["peer"], "flag": flag, "detail": detail,
                     "short": short,
                     "sub": sub, "role": role, "machine_id": egm,
                     "label": c.get("label") or "",
                     "user": sat_users.get(c["peer"], _u.SAT_USER),
                     "key": "companion:" + c["companionId"]})

    claimed = ({m["peer"] for m in fleet["machines"]}
               | {s["peer"] for s in fleet["sats"]}
               | {c["peer"] for c in fleet["companions"]})
    for l in fleet["leases"]:
        if l["ip"] in claimed:
            continue
        # A lease row carries no reported login — every fix page that a
        # wire row can reach prints an ssh line, so it gets this box's own
        # account rather than a KeyError the moment the row is opened.
        ents.append({"kind": "wire", "name": l["hostname"] or l["ip"],
                     "peer": l["ip"], "flag": "•", "user": _u.SAT_USER,
                     "detail": "took an address, never reported",
                     "sub": [], "role": "unknown box on the slot segment",
                     "key": "wire:" + l["ip"]})

    ents.append(_hub_entry(ctx, scan))
    return ents


def _entries_degraded(ctx, scan):
    """The hub's own floor service is dark, so the board is built from
    address leases, the wire, and ssh. The hub sorts to row 1 because in
    this state it IS the problem."""
    fleet = scan["fleet"]
    ents = [_hub_entry(ctx, scan)]
    live = _arp_live(fleet["neigh"])
    lease = {l["ip"]: l for l in fleet["leases"]}
    for ip in sorted(set(lease) | {n["ip"] for n in fleet["neigh"]}):
        who = (lease.get(ip) or {}).get("hostname") or ""
        if fleet["probes"].get(ip):
            detail = "leased as %s · answers ssh" % who if who \
                else "on the wire · answers ssh"
        elif ip in live:
            detail = "on the wire, no ssh answer (a machine, or no hub key)"
        else:
            detail = "leased once, dark on the wire now"
        ents.append({"kind": "wire", "name": who or ip, "peer": ip,
                     "flag": "•", "detail": detail, "sub": [],
                     "user": _u.SAT_USER,
                     "role": "box on the slot segment", "key": "wire:" + ip})
    return ents


_HUB_UNITS = ("cabinet-g2s", "cabinet-dhcp", "cabinet-dns", "cabinet-ntp",
              "cabinet-tftp")


def _hub_entry(ctx, scan):
    """The hub's own row. On the hub: five `systemctl is-active` answers plus
    HEAD-vs-start-time, two cheap local commands. Off it: the API's own
    account of itself — doctor_hub over a LOCAL transport would diagnose the
    laptop and shout HUB-DOWN about a hub that answered in milliseconds."""
    st = _st_of(scan)
    if ctx["place"] == "remote":
        cur = str(((st.get("hostOptions") or {}).get("updates") or {})
                  .get("current") or "")
        sas = len(st.get("sas") or {})
        comps = len(st.get("companions") or {})
        detail = "API answering" + (" · %s" % cur.split()[0] if cur else "")
        detail += " · %d SAS leg%s, %d card reader%s" % (
            sas, "" if sas == 1 else "s", comps, "" if comps == 1 else "s")
        return {"kind": "hub", "name": "hub",
                "peer": _hub_host().split(":")[0],
                "flag": "✅", "detail": detail, "sub": [],
                "role": "the floor host", "key": "hub"}

    t = ctx.get("local") or LocalTransport()
    states = _q(t, "systemctl is-active %s 2>/dev/null || true"
                % " ".join(_HUB_UNITS)).split()
    down = [u for u, s in zip(_HUB_UNITS, states) if s != "active"]
    sub = []
    if not scan["fleet"]["degraded"] and not down and len(states) == len(
            _HUB_UNITS):
        skew = _hub_skew(ctx, t)
        if skew:
            flag, detail = "⚠️", "running older code than this clone contains"
            sub = ["%s started before the last change here." % skew]
        else:
            flag, detail = "✅", "%d services up, API answering" % len(states)
    elif down:
        flag = "❌"
        detail = ("the floor service is down" if "cabinet-g2s" in down
                  else "%d service%s down" % (len(down),
                                              "" if len(down) == 1 else "s"))
        sub = ["%s %s inactive." % (", ".join(down),
                                    "is" if len(down) == 1 else "are")]
        if "cabinet-g2s" in down:
            sub = ["cabinet-g2s is inactive — no floor, no web page, no "
                   "machine can join, and it is why this screen is thin: the "
                   "machine and reader tiles all come from that service. "
                   "Open it; the hub's own checkup still works with the API "
                   "down, and it is what can say why."]
    else:
        flag, detail = "⚠️", "the floor service is not answering on :8081"
    return {"kind": "hub", "name": "hub", "peer": SLOT_PREFIX + "2",
            "flag": flag, "detail": detail, "sub": sub,
            "role": "the floor host", "key": "hub"}


def _hub_skew(ctx, t):
    """H1.4's question, cheaply: is any hub service older than HEAD? Names
    the services, or None."""
    rc, head_ct = _git(ctx["root"], "log", "-1", "--format=%ct")
    if rc != 0 or not head_ct.strip().isdigit():
        return None
    out = _q(t, 'for u in %s; do echo "$u $(systemctl show -p '
               'ExecMainStartTimestamp --value $u 2>/dev/null)"; done'
            % " ".join(_HUB_UNITS))
    old = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        m = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                      parts[1] if len(parts) > 1 else "")
        if not m:
            continue
        try:
            begun = time.mktime(time.strptime(m.group(0), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if begun < int(head_ct.strip()):
            old.append(parts[0])
    return " and ".join(old) if old else None


def _hub_host():
    return HUB_URL.split("//", 1)[-1]


# --- the root --------------------------------------------------------------

def _lead(ctx, ents):
    """The lead sentence is COMPUTED, so a healthy floor and a burning one
    can never render identically — the complaint this whole rework exists
    for."""
    bad = [e for e in ents if e["flag"] in ("❌", "⚠️")]
    if not ents:
        return []
    if not bad:
        machines = len([e for e in ents if e["kind"] == "machine"])
        legs = len([e for e in ents if e["kind"] == "smib"])
        readers = len([e for e in ents if e["kind"] == "companion"])
        bits = []
        if machines:
            bits.append("%d machine%s online" % (machines,
                                                 "" if machines == 1 else "s"))
        if legs:
            bits.append("%d SAS leg%s" % (legs, "" if legs == 1 else "s"))
        if readers:
            bits.append("%d card reader%s reporting"
                        % (readers, "" if readers == 1 else "s"))
        bits.append("the hub answering" if ctx["place"] == "remote"
                    else "hub services up")
        return textwrap.wrap("✅ Nothing needs you. " + ", ".join(bits) + ".",
                             width=_WIDTH, subsequent_indent="   ")
    worst = "❌" if any(e["flag"] == "❌" for e in bad) else "⚠️"
    return ["%s %d of %d need%s you%s."
            % (worst, len(bad), len(ents), "" if len(bad) > 1 else "s",
               ", worst first" if len(bad) > 1 else "")]


def _sorted_entries(ents):
    return sorted(ents, key=lambda e: (_FLAG_RANK.get(e["flag"], 2),
                                       _KIND_RANK.get(e["kind"], 5),
                                       e["name"]))


def _dispw(text):
    """Terminal cells, not code points. ✅ and ❌ are one code point and two
    cells; ⚠️ is two of each. len() under-measures the first two, which is
    exactly how a folded board row slipped past an 80-column rule."""
    return len(text) + text.count("✅") + text.count("❌")


def _fold_lines(cells):
    """Compact rows, in aligned COLUMNS, that still fit 80 — measured in
    display cells, so collector-given machine names cannot push a folded row
    off the right edge the way three 16-char ids nearly did."""
    w = max(_dispw(c) for c in cells)
    per = max(1, (_WIDTH + 2) // (w + 2))
    out = []
    for i in range(0, len(cells), per):
        chunk = cells[i:i + per]
        out.append("  ".join(c + " " * (w - _dispw(c))
                             for c in chunk).rstrip())
    return out


def _board_lines(ctx, ents, acts, start=1):
    """The device rows, worst first, in ONE number space. Problem rows are
    never folded; a healthy tail longer than six folds into compact numbered
    lines that stay pickable (the 14-machine tester floor). The hub is never
    folded — it is the floor host, and it is the one row carrying "5 services
    up, API answering"."""
    lines = []
    ents = _sorted_entries(ents)
    w_n = max([len(e["name"]) for e in ents] + [4])
    w_p = max([len(e["peer"]) for e in ents] + [4])
    bad = [e for e in ents if e["flag"] in ("❌", "⚠️")]
    good = [e for e in ents if e["flag"] not in ("❌", "⚠️")]
    keep = [e for e in good if e["kind"] == "hub"]
    rest = [e for e in good if e["kind"] != "hub"]
    # A lone leftover rendered compact next to six full siblings reads as a
    # different kind of thing, so the fold only starts when it saves rows.
    full, folded = rest, []
    if len(rest) > 7:
        full, folded = rest[:6], rest[6:]
    n = start

    def _emit(e):
        nonlocal n
        out = _rowlines(n, e["flag"], e["name"], e["peer"], e["detail"],
                        w_n, w_p)
        for s in e["sub"]:
            out += _indent(s)
        acts.append({"label": e["name"],
                     "run": (lambda ee=e: _open(ctx, ee))})
        n += 1
        return out

    for e in bad:
        lines += _emit(e)
    if bad and good:
        lines.append("")
        lines.append("   The rest are fine")
    for e in full + keep:
        lines += _emit(e)
    cells = []
    for e in folded:
        cells.append("%4s) %s %s" % (n, _flagcell(e["flag"]), e["name"]))
        _emit(e)
    if cells:
        lines += _fold_lines(cells)
    return lines


def _open(ctx, ent):
    if ent["kind"] == "machine":
        return machine_screen(ctx, ent)
    if ent["kind"] == "hub":
        return hub_screen(ctx)
    return device_screen(ctx, ent)


def _tools(ctx, acts, start):
    """The floor tools continue the SAME number space — a number only ever
    means "the n-th thing on this screen". Every label is a plain phrase; how
    long it takes and what it needs go on the description line every other
    timed action already uses, never padded into the label itself."""
    remote = ctx["place"] == "remote"
    lines = ["", " Floor tools"]
    items = [("Every device, in full", None,
              lambda: _run_page(ctx, lambda: fleet_table(
                  discover_fleet(api_status(), ctx["root"],
                                 local=ctx.get("local")))))]
    if not remote:
        # Leases and who-is-on-the-wire are things only the hub can see; that
        # diff would be fiction from anywhere else.
        items.append(("Who has a lease, who is on the wire, and who reports "
                      "to the hub", None,
                      lambda: _run_page(ctx, lambda: three_set_diff(
                          discover_fleet(api_status(), ctx["root"],
                                         local=ctx.get("local"))))))
    items.append(("Check every device, one after another",
                  "About 40 s. Each box gets its own checkup over ssh; "
                  "anything this computer cannot reach says so instead."
                  if remote else
                  "About 40 s. Each box gets its own checkup over ssh.",
                  lambda: sweep_screen(ctx)))
    tree = "This clone — branch, version, files that differ, hub key"
    items.append((tree if remote
                  else "This hub and this clone — branch, version, files "
                       "that differ, hub key", None,
                  lambda: _run_page(ctx, tree_facts, ctx["root"],
                                    title=("This clone" if remote
                                           else None))))
    n = start
    for label, desc, run in items:
        lines += _wrapped_item(n, label, desc)
        acts.append({"label": label, "run": run})
        n += 1
    return lines


def _wrapped_item(num, label, desc=None, pad=9):
    """A numbered menu item, wrapped, with its optional one-sentence
    description underneath."""
    parts = _wrapped(label, 6)
    out = ["%4s) %s" % (num, parts[0])] + ["      " + p for p in parts[1:]]
    if desc:
        out += [" " * pad + l for l in _wrapped(desc, pad)]
    return out


def _header(ctx):
    """One header line, always: who this floor belongs to, WHERE this tool is
    running, and against which tree."""
    st = _st_of({"status": ctx.get("status")})
    room = str((st.get("hostOptions") or {}).get("gameroomName")
               or st.get("gameroom") or "").strip()
    place = ctx["place"]
    if place == "remote":
        head = "cabinetconfig — %shub %s, reachable from here" % (
            room + " · " if room else "", _hub_host())
        note = _wrapped(
            "Reading the whole floor from here. Repairs run on the hub: the "
            "update lock that keeps this tool and deploy/update.py from "
            "colliding is a file there, and so is the key that reaches the "
            "Pis.", 0)
        return [""] + [_b(head)] + note
    if place == "wrong-clone":
        return ["", _b("cabinetconfig — on the hub, but not in the clone it "
                       "runs"),
                "%s%s" % (" " * 8, ctx["root"])]
    head = "cabinetconfig — %son the hub %s · %s" % (
        room + " · " if room else "", SLOT_PREFIX + "2", ctx["root"])
    out = [""] + _wrapped(head, 0)
    out[1] = _b(out[1])
    if place == "degraded":
        out += _wrapped("The hub's own floor service is not answering on "
                        ":8081, so the board below is built from address "
                        "leases, the wire, and ssh instead.", 0)
    return out


def render_root(ctx, scan):
    """Depth 0 — the floor. Opens with the answer, not a menu."""
    acts = []
    lines = _header(ctx)
    for extra in _standing_warnings(ctx):
        lines += _wrapped(extra, 0)
    if ctx["place"] == "wrong-clone":
        return _render_wrong_clone(ctx, lines, acts)
    ents = _entries(ctx, scan)
    # The board is the session's shared truth: the machine screen routes off
    # it and a device screen names its Pi-mate from it, so neither has to
    # re-derive anything (or re-probe) to answer.
    ctx["ui"]["ents"], ctx["ui"]["fleet"] = ents, scan["fleet"]
    lines.append("")
    if ctx["place"] == "hub" and not [e for e in ents if e["kind"] != "hub"]:
        lines += _empty_floor_lines()
    else:
        lines += _lead(ctx, ents)
    lines.append("")
    lines += _board_lines(ctx, ents, acts)
    lines += _tools(ctx, acts, len(acts) + 1)
    return lines, acts


def _standing_warnings(ctx):
    """What the old banner stack carried, reduced to the lines that genuinely
    change what a repair will do tonight. Everything else moved into the row
    it belongs to."""
    out = []
    phase = _tournament_phase(ctx.get("status") or {})
    if phase:
        out.append("⚠️ A tournament is running — repairs that restart "
                   "anything will refuse until it finishes.")
    if lock_holder(ctx["root"]):
        out.append("⚠️ deploy/update.py is running here — repairs wait for "
                   "it.")
    # The adopt marker genuinely blocks repair, and it survived the rework
    # only inside a submenu leaf. It belongs where it was: on every launch.
    if os.path.exists(os.path.join(ctx["root"], _u.ADOPT_MARKER)):
        out.append("❌ The last adoption never completed an update — run "
                   "deploy/update.py before repairing anything.")
    return out


def _empty_floor_lines():
    """A brand-new hub is the other first impression, and it looks exactly
    like a broken one unless the screen says so."""
    return (_indent("•  Nothing has reported to this hub yet, and no "
                     "addresses are on file. A brand-new hub looks exactly "
                     "like this. Helper Pis are wired-only on the slot "
                     "network; the hub gives them an address and they "
                     "identify themselves:", 3)
            + ["         deploy/smib_setup.sh <user>@<pi>        a Pi wired "
               "to a machine",
               "         deploy/companion_setup.sh <user>@<pi>   a Pi with a "
               "card reader"]
            + _indent("Cabinets join by pointing their G2S host at "
                       "http://192.168.50.2:8081/G2S.", 3))


def _render_wrong_clone(ctx, lines, acts):
    """Diagnosing one tree while the units run another wastes the night, so
    this stays loud — and the menu shrinks to what genuinely works. The fix
    is one printed `cd`, so it is printed and NOT also offered as a number:
    an item whose whole page is a command already on screen teaches nothing,
    and this is the screen whose entire job is one instruction."""
    wd = os.path.dirname(ctx["identity"].get("wd") or "") or "<the hub's tree>"
    lines += ["", "❌ 1 of 1 needs you.", ""]
    lines += _indent("❌ This is not the folder your hub actually runs", 3)
    lines += _indent("The installed cabinet-g2s unit runs %s — this clone is "
                     "%s. Repairs from here would read one tree and change "
                     "another, so they are switched off until you move:"
                     % (wd, ctx["root"]), 6)
    lines += _cmds("cd %s && ./cabinetconfig" % wd)
    lines.append("   Everything below still only looks, never changes.")
    lines += _tools(ctx, acts, 1)
    return lines, acts


def root_screen(ctx):
    """Depth 0. b is never rendered here and never exits — quitting is q's
    job, so a stray keystroke can never dump someone past the finish pass."""
    return _pump(ctx, lambda fresh: render_root(ctx, _scan(ctx)), 0,
                 typed=True)


# --- the machine screen ----------------------------------------------------

def machine_screen(ctx, ent):
    """"What is wrong with my BB2" — the router. Comms state, at most one
    last event, the devices that serve it, and the sentence that keeps this
    page from growing into a second config system."""
    def build(fresh):
        return render_machine(ctx, ent)
    return _pump(ctx, build, 1)


def render_machine(ctx, ent):
    st = _st_of({"status": ctx.get("status")})
    m = ent["machine"]
    acts = []
    title = "%s — %s · %s" % (ent["name"], m["egmId"], m["peer"] or "?") \
        if ent["name"] != m["egmId"] else "%s · %s" % (m["egmId"],
                                                       m["peer"] or "?")
    lines = ["", _b(title), ""]
    if m["commsState"] == "onLine":
        lines.append("   ✅ online to the hub over G2S")
    elif m["joining"]:
        lines.append("   ⚠️ reached the hub, not joined yet")
    else:
        lines.append("   ❌ offline to the hub")

    ents = [e for e in ctx["ui"]["ents"]
            if e.get("machine_id") == m["egmId"]]
    for e in ents:
        if e["kind"] == "smib" and e["flag"] != "✅":
            lines += _indent("%s its SAS leg — %s %s"
                             % (e["flag"], e["name"], e["short"]), 3)
    ev = _last_event(st, m["egmId"])
    if ev:
        lines += _indent("• last event: %s" % ev, 3)
    lines += ["", "   What serves this machine"]
    if not ents:
        lines += _indent("No SAS leg and no card reader report from this "
                         "machine's address.", 3)
    else:
        w_n = max(len(e["name"]) for e in ents)
        w_p = max(len(e["peer"]) for e in ents)
        for i, e in enumerate(ents, 1):
            # The board sentence already names the machine; here the machine
            # is the page, so only the state half of it is news.
            lines += _rowlines(i, e["flag"], e["name"], e["peer"],
                               ("SAS leg — " if e["kind"] == "smib"
                                else "card reader — ") + e["short"],
                               w_n, w_p)
            acts.append({"label": e["name"],
                         "run": (lambda ee=e: device_screen(ctx, ee, back=
                                                            "back"))})
    lines.append("")
    # Without this sentence the page grows credits, meters and denoms — the
    # exact drift the tool's doctrine forbids. The cap is load-bearing.
    lines += _indent("A machine's own settings — denominations, games, "
                     "ticket header, its host address — are never changed "
                     "from here. The web page and the proven ceremonies own "
                     "those.", 3)
    return lines, acts


def _last_event(st, egm):
    """The newest activity-tape entry for this machine, or None. The tape is
    newest-first and every entry is stamped when it lands."""
    for e in (st.get("activity") or [])[:40]:
        if not isinstance(e, dict) or e.get("egmId") != egm:
            continue
        label = str(e.get("label") or "").strip()
        code = str(e.get("code") or "").strip()
        when = str(e.get("seenAt") or "")[11:16]
        text = ("%s %s" % (code, label)).strip()
        return "%s%s" % (text, " at %s" % when if when else "")
    return None


# --- the device screen -----------------------------------------------------

def _transport_for(ctx, dev):
    """A device's carrier. The hub's own box (and its loopback enrollment
    reader) run locally; everything else rides ssh as the login the satellite
    REPORTED for itself — never a guessed account."""
    if dev["peer"] in ("127.0.0.1", SLOT_PREFIX + "2", "", "?"):
        return LocalTransport()
    return SshTransport(dev["peer"], user=dev.get("user") or _u.SAT_USER)


def _run_doctor(fn, *a, **kw):
    """A doctor prints its whole probe table through say(); the screen shows
    the findings and a count instead. Redirecting stdout ONLY still tees
    every line into the transcript, so the full evidence and hints are on
    disk and in --json — exactly where the spec keeps them."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*a, **kw)


def _doctor_for(ctx, dev, fresh=False):
    """One doctor per device per session, cached: without it, b and every
    redraw would re-run ~20 ssh probes. `r` is the only thing that
    re-probes."""
    cache = ctx["ui"]["docs"]
    key = dev["key"]
    if not fresh and key in cache:
        return cache[key]
    t = _transport_for(ctx, dev)
    kind = dev["kind"]
    say("")
    say("Checking %s over %s%s — about %d s…"
        % (dev["name"], "ssh" if isinstance(t, SshTransport) else "this box",
           " (%s)" % dev["peer"] if isinstance(t, SshTransport) else "",
           10 if kind != "companion" else 8))
    if kind == "companion":
        doc = _run_doctor(doctor_companion, t, dev["name"], ctx.get("status"))
    else:
        doc = _run_doctor(doctor_smib, t, dev["name"], ctx.get("status"))
        if kind == "wire" and _is_companion_only(doc):
            # A typed address has no declared duty. doctor_smib's own census
            # is what tells the two apart, and it hands the companion tier
            # over by name — so re-ask the right doctor rather than report a
            # reader as a SAS leg that was never built.
            doc = _run_doctor(doctor_companion, t, dev["name"],
                              ctx.get("status"))
            dev["kind"] = "companion"
    cache[key] = doc
    return doc


def _is_companion_only(doc):
    f = next((f for f in doc["findings"]
              if f["code"] == "NEVER-PROVISIONED"), None)
    return bool(f and _C11_BANNER in (f.get("hint") or ""))


def device_screen(ctx, dev, back="back to the floor"):
    """Depth 1 — one box. Its doctor runs, the findings print, and ONLY the
    actions those findings imply are offered, in plain language."""
    if ctx["place"] == "remote":
        t = _transport_for(ctx, dev)
        if isinstance(t, SshTransport) and not _reachable_cached(ctx, dev, t):
            return _pump(ctx, lambda fresh: render_hub_view(ctx, dev), 1,
                         back=back)
    return _pump(ctx, lambda fresh: render_device(
        ctx, dev, _doctor_for(ctx, dev, fresh)), 1, back=back)


def _reachable_cached(ctx, dev, t):
    hit = ctx["ui"]["reach"].get(dev["key"])
    if hit is None:
        say("")
        say("Trying %s over ssh…" % dev["peer"])
        hit = t.reachable()
        ctx["ui"]["reach"][dev["key"]] = hit
    return hit


def _doc_port(doc):
    """The SAS port and interpreter this leg's unit actually uses, read back
    out of the doctor's own probe rows — install facts always come from the
    target, never a guess. Falls back to what the doctor itself defaults
    to."""
    port, venv = "/dev/ttyAMA0", "~/venvs/cabinet/bin/python"
    for c in doc.get("checks") or []:
        if c["probe"].endswith("port") and c["detail"].startswith("/dev/"):
            port = c["detail"].split()[0]
        if c["probe"].endswith("venv") and c["detail"].startswith("/"):
            venv = c["detail"].split()[0].rstrip(":")
    return port, venv


def render_device(ctx, dev, doc):
    """The screen the whole design is built around: what is wrong, in one
    sentence, with its fix numbered directly underneath it."""
    acts = []
    # A bare parenthesised login read as noise and vanished on the other
    # route to the same box; labelled, it says what the doctors ssh as.
    title = "%s%s · %s%s%s" % (
        dev["name"], " — %s" % dev["role"] if dev["role"] else "",
        dev["peer"], "" if dev["kind"] == "hub" else " · ssh as %s"
        % _login(dev), ' · "%s"' % dev["label"] if dev.get("label") else "")
    lines = [""] + _wrapped(title, 0)
    lines[1] = _b(lines[1])
    peer_of = [e for e in ctx["ui"]["ents"]
               if e["peer"] == dev["peer"] and e["key"] != dev["key"]
               and e["kind"] in ("smib", "companion")]
    if peer_of:
        lines += _indent("This Pi is also the %s %s."
                          % ("card reader" if peer_of[0]["kind"] == "companion"
                             else "SAS leg", peer_of[0]["name"]), 0)
    if doc["verdict"] == "UNREACHABLE":
        lines += _indent("Nothing on this box answered. Not one probe got "
                          "through, so nothing below it was actually "
                          "examined.", 0)
    else:
        checks = doc.get("checks") or []
        # "right" means "not wrong": a probe that could not look is not a
        # failure, and counting it as one would overstate every degraded box.
        ok = len([c for c in checks if c["ok"] is not False])
        lines.append("Looked at %d things. %s"
                     % (len(checks), "All %d are right." % ok
                        if ok == len(checks) else "%d are right." % ok))
    ribbon = ctx["ui"]["touched"].get(dev["key"])
    if ribbon:
        lines.append("This session: %s" % ribbon)

    lines += render_findings(ctx, dev, doc, acts)
    lines.append("")
    unit = _unit_for(dev["kind"])
    tail = []
    if doc["verdict"] != "UNREACHABLE":
        # A box that answered nothing has nothing to page through, and a
        # journal read that cannot run is exactly the dead entry this menu
        # does not have.
        tail = [("Show all %d checks" % len(doc.get("checks") or []),
                 lambda: _page(ctx, render_checks(dev, doc))),
                ("Show the last 30 log lines from %s" % unit,
                 lambda: _run_page(ctx, _journal_tail,
                                   _transport_for(ctx, dev), unit))]
    if peer_of and doc["verdict"] != "UNREACHABLE":
        other = peer_of[0]
        tail.append(("Put its %s on the bench instead — %s"
                     % ("card reader" if other["kind"] == "companion"
                        else "SAS leg", other["name"]),
                     lambda: device_screen(ctx, other, back="back")))
    tail.append(("The one-line commands for this screen",
                 lambda: _page(ctx, render_oneliners(ctx, dev))))
    for label, run in tail:
        lines += _wrapped_item(len(acts) + 1, label)
        acts.append({"label": label, "run": run})
    return lines, acts


def render_findings(ctx, dev, doc, acts):
    """Findings cap at THREE (fatal, then warn, then info). A cascading
    never-provisioned box produces a wall of consequences, and the wall must
    never push the cause off the screen."""
    lines = []
    findings = sorted(doc.get("findings") or [],
                      key=lambda f: _SEV_RANK.get(f["severity"], 3))
    rail = _rail(ctx, doc)
    if not findings:
        lines += ["", "✅ Nothing is wrong with this %s."
                  % ("reader" if dev["kind"] == "companion" else "box")]
        if dev["kind"] == "companion":
            lines += _indent("The reader claims OK — but a reader can claim "
                              "OK and still not read. A real card tap is the "
                              "only judge.", 3)
        return lines
    for f in findings[:3]:
        ui = _code_ui(dev["kind"], f["code"])
        flag = _SEV_FLAG[f["severity"]]
        lines.append("")
        if not ui:
            # An unknown code degrades to the doctor's own rendering, with no
            # numbered child — a new code can never mint a dead entry.
            lines += _wrapped("%s %s — %s"
                              % (flag, f["code"], _plain(f["evidence"])), 0)
            if f.get("hint"):
                lines += _indent("↳ %s" % _plain(f["hint"]), 6)
            continue
        if f["severity"] == "info":
            # An informational finding is one plain sentence; its evidence is
            # the sentence's own restatement, and the transcript still has it.
            lines += _wrapped("%s %s  %s" % (flag, ui["head"], f["code"]), 0)
            if ui.get("act"):
                lines += _wrapped_item(len(acts) + 1,
                                       _act_label(ctx, ui["act"]),
                                       _act_desc(ctx, ui["act"]))
                acts.append({"label": _act_label(ctx, ui["act"]),
                             "run": _act_run(ctx, dev, doc, ui["act"], f)})
            continue
        lines += _wrapped("%s %s" % (flag, ui["head"]), 0)
        ev = _wrapped(_plain(f["evidence"]), 6)[:2]
        if len(_wrapped(_plain(f["evidence"]), 6)) > 2:
            ev[-1] = ev[-1][:_WIDTH - 8] + "…"
        for i, e in enumerate(ev):
            lines.append(" " * 6 + e)
        if len(ev[-1]) + 8 + len(f["code"]) <= _WIDTH:
            lines[-1] = lines[-1] + "  " + f["code"]
        else:
            lines.append(" " * 6 + f["code"])
        act = ui.get("act")
        if act and not _act_available(ctx, act, f):
            act = None          # no target ⇒ no item; never a dead entry
        if act and rail and _mutating(act):
            # A rail up means the ACTION is not rendered at all; its slot
            # carries one sentence saying why, and it comes back the
            # moment the rail clears.
            lines += _indent(rail, 6)
        elif act:
            lines += _wrapped_item(len(acts) + 1,
                                   _act_label(ctx, act),
                                   _act_desc(ctx, act))
            acts.append({"label": _act_label(ctx, act),
                         "recheck": _mutating(act),
                         "run": _act_run(ctx, dev, doc, act, f)})
        elif ui.get("after"):
            lines += ui["after"](dev)
        elif f.get("hint") and f["severity"] != "info":
            lines += _indent("↳ %s" % _plain(f["hint"]), 6)
    if len(findings) > 3:
        lines += ["", "   +%d more finding%s — see all %d checks below."
                  % (len(findings) - 3, "" if len(findings) == 4 else "s",
                     len(doc.get("checks") or []))]
    return lines


def _act_available(ctx, act, finding):
    """The last gate before an item is drawn: an offer that could not do
    anything from here is not rendered at all."""
    if act["kind"] != "jump-device":
        return True
    who = str(finding.get("evidence") or "").split(":")[0].split("/")[0]
    return any(e["name"] == who.strip() for e in ctx["ui"]["ents"])


def _mutating(act):
    return act["kind"] in ("verb-smib-up", "verb-comp-restart")


def _act_label(ctx, act):
    """Off the hub a repair is not refused, it is RELOCATED — and the label
    says so before it is pressed."""
    if ctx["place"] == "remote" and _mutating(act):
        return act["label"] + " — runs on the hub"
    return act["label"]


def _act_desc(ctx, act):
    if ctx["place"] == "remote" and _mutating(act):
        return "Prints the exact command for the hub, and why it lives there."
    if act["kind"] == "block":
        return "(prints commands, changes nothing)"
    return act.get("desc")


def _act_run(ctx, dev, doc, act, finding):
    """Every offer is one of four things: one of the two frozen verbs, an
    existing signpost, a copy-paste block, or a jump to another screen."""
    kind = act["kind"]
    if kind == "block":
        return lambda: _page(ctx, ["", _b("   " + act["label"]), ""]
                             + _pad(act["build"](dev)))
    if kind == "signpost-park":
        return lambda: _run_page(ctx, signpost_hub_park, dev["name"])
    if kind == "signpost-bench":
        port, venv = _doc_port(doc)
        return lambda: _run_page(ctx, signpost_bench_dance, port, venv)
    if kind == "journal":
        unit = _unit_for(dev["kind"])
        return lambda: _run_page(ctx, _journal_tail,
                                 _transport_for(ctx, dev), unit)
    if kind == "jump-hub":
        return lambda: hub_screen(ctx)
    if kind == "jump-device":
        return lambda: _jump_device(ctx, finding)
    if ctx["place"] == "remote":
        return lambda: _page(ctx, render_relay(ctx, dev, kind))
    return lambda: _do_verb(ctx, dev, kind)


def _jump_device(ctx, finding):
    """A hub-side tile finding names its Pi in the evidence it already
    carries ("smib-2831b6/1: stale — …"), so the jump needs no new probe. No
    match on the board ⇒ no jump: an invented device is worse than none."""
    who = str(finding.get("evidence") or "").split(":")[0].split("/")[0]
    hit = next((e for e in ctx["ui"]["ents"] if e["name"] == who.strip()),
               None)
    if not hit:
        say("")
        say("   %r is not on the floor list — open it from there, or type "
            "its address." % who.strip())
        return None
    return device_screen(ctx, hit, back="back")


def _do_verb(ctx, dev, kind):
    """The verb runs verbatim — probe, literal plan, confirm, mutate,
    wire-verify, one outcome line. The menu only contributes the title and
    the automatic re-check afterwards.

    ⚖️ THE SAFETY LAW: a NUMBER never changes anything — only an explicit `y`
    at the confirm prompt does. So the menu ALWAYS asks, even under --yes.
    That flag exists so a scripted one-liner (`cabinetconfig smib X up --yes`)
    can run unattended; letting it also arm every menu digit would mean a
    mistyped number repairs a machine, and the rows renumber worst-first as
    the floor changes. Scripting convenience must never disarm the UI."""
    t = _transport_for(ctx, dev)
    a = ctx["args"]
    say("")
    if kind == "verb-smib-up":
        ok = run_mutating(ctx, verb_smib_up, t, dev["name"], ctx["session"],
                          False, a.dry_run)
        what = "SAS leg turned on"
    else:
        ok = run_mutating(ctx, verb_companion_restart, t, dev["name"],
                          ctx["session"], False, a.dry_run)
        what = "card reader restarted"
    ctx["ui"]["touched"][dev["key"]] = (
        "%s at %s — %s" % (what, time.strftime("%H:%M"),
                           "verified on the wire" if ok is True
                           else "it did not verify"))
    ctx["ui"]["docs"].pop(dev["key"], None)
    say("")
    say("Checking %s again…" % dev["name"])


def render_checks(dev, doc):
    """Depth 2 — the corroboration you ask for. Plan-code prefixes are
    stripped at RENDER TIME ONLY: checks[].probe stays byte-identical inside
    the doctor dict, so --json and the gate's probe-command transcript are
    untouched."""
    checks = doc.get("checks") or []
    lines = ["", _b("All %d checks — %s · %s" % (len(checks), dev["name"],
                                                 dev["peer"])), ""]
    if not checks:
        return lines + ["   (nothing was examined — the box did not answer)"]
    names = [_PLAN_PREFIX.sub("", c["probe"]) for c in checks]
    w = max(len(n) for n in names)
    for c, n in zip(checks, names):
        flag = "✅" if c["ok"] else ("•" if c["ok"] is None else "❌")
        col = 3 + 2 + 1 + w + 2
        parts = _wrapped(_plain(c["detail"]), col)
        lines.append("   %s %-*s  %s" % (_flagcell(flag), w, n, parts[0]))
        lines += [" " * col + p for p in parts[1:]]
    return lines


def render_oneliners(ctx, dev):
    """Depth 2 — the CLI on-ramp, rendered from the NOUNS table so it can
    never list a verb that does not exist. The hub is its OWN noun with one
    verb and no target: rendering it as `smib hub up --yes` handed a reader a
    copy-pasteable mutation aimed at a host literally named "hub"."""
    if dev["kind"] == "hub":
        noun, target = "hub", ""
    else:
        noun = "companion" if dev["kind"] == "companion" else "smib"
        target = " " + dev["name"]
    lines = ["", _b("   The one-line commands for this screen"), "",
             "   Everything you just saw is also a command, for scripts and "
             "for pasting:", ""]
    for verb in sorted(NOUNS[noun]):
        suffix = " --yes" if (noun, verb) in MUTATING else ""
        lines.append("      ./cabinetconfig %s%s %s%s"
                     % (noun, target, verb, suffix))
    lines.append("      ./cabinetconfig %s%s doctor --json" % (noun, target))
    # "From another box" means the hub's own address on the slot segment —
    # on the hub HUB_URL is loopback, and a loopback address is unusable in
    # the one block on the page that is for pasting somewhere else.
    lines += ["", "   From another box:"] + _cmds(
        "ssh %s@%s 'cd ~/CabiNet && ./cabinetconfig %s%s doctor'"
        % (_hub_login(ctx), _hub_addr(), noun, target))
    return lines


# --- off the hub, with a hub in reach --------------------------------------

def render_hub_view(ctx, dev):
    """This computer routes to the hub but not to the Pi. Say so TWICE — a
    hurried reader taking a green status line for a clean bill is the failure
    mode this screen must not have."""
    st = _st_of({"status": ctx.get("status")})
    acts = []
    title = "%s%s · %s%s" % (
        dev["name"], " — %s" % dev["role"] if dev["role"] else "",
        dev["peer"], ' · "%s"' % dev["label"] if dev.get("label") else "")
    lines = [""] + _wrapped(title, 0)
    lines[1] = _b(lines[1])
    lines += _indent("This computer cannot reach %s over ssh, so the Pi's "
                      "own checkup did not run. Routing to the hub does not "
                      "always mean routing to the slot segment. This is the "
                      "hub's view — not a doctor." % dev["peer"], 0)
    lines.append("")
    trouble = False
    if dev["kind"] == "companion":
        ent = (st.get("companions") or {}).get(dev["name"]) or {}
        if ent.get("readerOk") is False:
            trouble = True
            lines += _indent("⚠️ the reader has stopped answering — taps do "
                              "nothing", 3)
            if ent.get("lastError"):
                lines += _indent('"%s"' % _clip(str(ent["lastError"]), 110), 6)
        lines += _indent("✅ the daemon itself is reporting" if not
                          ent.get("stale") else
                          "❌ the daemon is not reporting at all", 3)
        trouble = trouble or bool(ent.get("stale"))
        binds = ent.get("bindings") or {}
        if binds.get("g2sEgmId"):
            lines += _indent("• bound to %s" % binds["g2sEgmId"], 3)
        verb, unit = "restart", "companion"
    else:
        # SENTENCES, the same as the companion branch eight lines up. `tile`
        # is hub-internal jargon and `online=False stale=True` is a dict
        # printed at a person — the exact defect the front door was rebuilt
        # to kill, and one function must not speak two languages.
        legs = [(k, e or {}) for k, e in sorted((st.get("sas") or {}).items())
                if (e or {}).get("peer") == dev["peer"]]
        parked = False
        for key, e in legs:
            where = ("SAS address %s: " % (e.get("address")
                                           or key.rsplit("/", 1)[-1])
                     if len(legs) > 1 else "")
            age = e.get("reportAgeSec")
            if e.get("stale"):
                trouble = True
                lines += _indent(
                    "⚠️ %sthe hub has not heard from this leg%s" %
                    (where, " for %s" % _age_str(age)
                     if isinstance(age, (int, float)) else ""), 3)
            elif e.get("sasEnabled") is False:
                trouble, parked = True, True
                lines += _indent("⚠️ %sthe hub is telling this leg to park — "
                                 "that is a switch, not a fault" % where, 3)
            elif e.get("online"):
                lines += _indent("✅ %sthe hub hears it, and the machine is "
                                 "answering" % where, 3)
            else:
                trouble = True
                lines += _indent("⚠️ %sthe hub hears it, but the machine leg "
                                 "is dark" % where, 3)
        if not legs:
            trouble = True
            lines += _indent("⚠️ the hub has no SAS leg on file for this "
                             "address at all", 3)
        # A parked leg is not brought up by the repair verb — the hub's own
        # switch owns it, and `smib … up` would only refuse.
        verb, unit = ("park" if parked else "up"), "smib"
    lines.append("")
    # No finding, no repair offered — the same law as a device screen: an
    # action appears because something is wrong, never because a box exists.
    if not trouble:
        lines += _indent("Nothing in the hub's view is wrong here. The Pi's "
                          "own checkup is the next word, and it needs a shell "
                          "on the hub:", 3)
        verb = "doctor"
    else:
        lines += _indent("Repairs run on the hub. From there:", 3)
    lines += _cmds("ssh %s@%s" % (_hub_login(ctx), _hub_addr()),
                   "cd ~/CabiNet && ./cabinetconfig %s %s %s"
                   % (unit, dev["name"], verb))
    label = "Print that command on one line, for a clean copy"
    lines += _wrapped_item(1, label)
    acts.append({"label": label, "run": lambda: _page(ctx, _relay_lines(
        ctx, dev, unit, verb))})
    return lines, acts


def _relay_lines(ctx, dev, noun, verb):
    return ["", _b("   Run it on the hub")] + _cmds(
        "ssh %s@%s 'cd ~/CabiNet && ./cabinetconfig %s %s %s%s'"
        % (_hub_login(ctx), _hub_addr(), noun, dev["name"], verb,
           " --yes" if (noun, verb) in MUTATING else ""))


def render_relay(ctx, dev, kind):
    """State 2's relocated mutation. Not a refusal: the exact command, on the
    hub that owns the lock and the key."""
    noun = "companion" if kind == "verb-comp-restart" else "smib"
    verb = "restart" if noun == "companion" else "up"
    lines = ["", _b("   %s — from the hub"
                    % ("Restart the card reader" if noun == "companion"
                       else "Turn the SAS leg back on"))]
    lines += ["", "   Copy this, or ssh in and use the menu:"]
    lines += _cmds("ssh %s@%s 'cd ~/CabiNet && ./cabinetconfig %s %s %s "
                   "--yes'" % (_hub_login(ctx), _hub_addr(), noun,
                               dev["name"], verb))
    lines += _indent("Why there and not here: the one-writer lock (%s) is a "
                      "file on the hub, shared with deploy/update.py. A "
                      "repair started from a laptop could not take it, so two "
                      "writers could hit one Pi at once. The hub is also "
                      "where the key that reaches the Pis lives."
                      % LOCK_NAME, 3)
    return lines


def hub_screen(ctx):
    """The hub's own device screen. Off the hub it is the API's account of
    itself plus the relay — NEVER doctor_hub, whose git, lock, adopt-marker
    and identity legs all evaluate against the LOCAL path and would diagnose
    the laptop."""
    if ctx["place"] == "remote":
        return _pump(ctx, lambda fresh: render_hub_api_view(ctx), 1)

    def build(fresh):
        cache = ctx["ui"]["docs"]
        if fresh or "hub" not in cache:
            say("")
            say("Checking the hub — about 10 s…")
            cache["hub"] = _run_doctor(doctor_hub, ctx["root"],
                                       ctx.get("local") or LocalTransport(),
                                       ctx.get("status"))
        dev = _dev("hub", "hub", SLOT_PREFIX + "2", role="the floor host",
                   key="hub")
        return render_device(ctx, dev, cache["hub"])
    return _pump(ctx, build, 1)


def render_hub_api_view(ctx):
    st = _st_of({"status": ctx.get("status")})
    acts = []
    cur = str(((st.get("hostOptions") or {}).get("updates") or {})
              .get("current") or "")
    lines = ["", _b("hub — the floor host · %s (over the API, no shell from "
                    "here)" % _hub_host()), ""]
    lines.append("   ✅ answering on %s" % _hub_host())
    if cur:
        # The short hash, not the whole commit SUBJECT: a changelog headline
        # is developer prose, not a hub status line. The board row for the
        # same hub already does exactly this.
        lines += _indent("✅ running %s" % cur.split()[0], 3)
    lines += _indent("✅ tournament idle" if not _tournament_phase(st)
                      else "⚠️ tournament %s" % _tournament_phase(st), 3)
    machines = len([k for k, v in st.items()
                    if isinstance(v, dict) and "commsState" in v])
    lines += _indent("• hearing %d machine%s, %d SAS leg%s, %d card reader%s"
                      % (machines, "" if machines == 1 else "s",
                         len(st.get("sas") or {}),
                         "" if len(st.get("sas") or {}) == 1 else "s",
                         len(st.get("companions") or {}),
                         "" if len(st.get("companions") or {}) == 1 else "s"),
                      3)
    lines.append("")
    lines += _indent("The hub's own checks read unit files, ports, the "
                      "database and the journal — all of which need a shell "
                      "on the hub itself:", 3)
    host = _hub_addr()
    lines += _cmds("ssh %s@%s" % (_hub_login(ctx), host),
                   "cd ~/CabiNet && ./cabinetconfig hub doctor")
    label = "Print that command on one line, for a clean copy"
    lines += _wrapped_item(1, label)
    acts.append({"label": label, "run": lambda: _page(ctx, [
        "", _b("   Run it on the hub")] + _cmds(
            "ssh %s@%s 'cd ~/CabiNet && ./cabinetconfig hub doctor'"
            % (_hub_login(ctx), host)))})
    return lines, acts


# --- the sweep -------------------------------------------------------------

# A doctor prints its probe table with the plan code that ORGANISES the
# source file in front of each label ("S1.1 unit census"). render_checks
# strips those; the sweep printed each doctor straight through say() and so
# never met the stripper. Stripping a printed table means re-aligning it —
# S1.1 and S1.10 are not the same width — so the rows are re-laid rather than
# re-flowed, and every other line is left byte-for-byte alone.
# group 1 is the WHOLE flag cell, verbatim — •  and ✅ occupy different cell
# counts, and rebuilding it from the glyph is how the • rows stopped matching
# (and so kept their plan codes) in the first place.
_PROBE_ROW = re.compile(r"^(   (?:✅|❌|•) +)(\S.*?)(  +)(.*)$")


def _plain_report(lines):
    """A captured report with its plan codes out and its columns intact.

    It does NOT re-lay the table. The doctor already aligned it once, and a
    detail that wrapped is indented to THAT detail column — so re-flowing to
    a fresh per-block width orphaned every continuation line and gave one
    table five different column positions. Instead the stripped label is
    re-padded to the width it vacated, which holds the detail column exactly
    where the doctor put it and leaves wrapped lines lined up under it."""
    out = []
    for raw in lines:
        m = _PROBE_ROW.match(_ANSI.sub("", raw))
        if not m:
            out.append(_PLAN_WORD.sub("", raw).rstrip()
                       if _PLAN_WORD.search(raw) else raw)
            continue
        head, label, gap, detail = m.group(1), m.group(2), m.group(3), \
            m.group(4)
        short = _PLAN_PREFIX.sub("", label)
        # give back exactly the cells the prefix used, never less than the
        # two-space gap that makes it a table
        pad = max(2, len(label) + len(gap) - len(short))
        out.append(("%s%s%s%s" % (head, short, " " * pad,
                                  _plain(detail))).rstrip())
    return out


def sweep_screen(ctx):
    """The fleet sweep, with its summary rows made pickable — the box you
    just read about is one keystroke away. The rows are NUMBERED here, on
    this screen, in the order they are printed: a number means "the n-th
    thing you are looking at", and a numbered pick list underneath an
    UNNUMBERED table in a different order breaks exactly that."""
    def build(fresh):
        got = {}
        # The doctors print their probe tables straight through say(); this
        # is the only path where they never met the plan-code stripper.
        report = _plain_report(_capture(
            lambda: got.setdefault("r", sweep(ctx, summary=False))))
        picks = []
        for r in got.get("r") or []:
            kind = r.get("kind")
            if kind not in ("smib", "companion", "hub"):
                continue
            key = "%s:%s" % (kind, r.get("target"))
            known = next((e for e in ctx["ui"]["ents"]
                          if e["key"] == key), None)
            dev = known or _dev(kind, r.get("target") or r.get("peer"),
                                r.get("peer") or "?", role="", key=key)
            if r.get("checks"):
                ctx["ui"]["docs"][dev["key"]] = r
            picks.append((_sweep_flag(r), dev, r))
        picks.sort(key=lambda p: (_FLAG_RANK.get(p[0], 2), p[1]["kind"],
                                  p[1]["name"]))
        # Only a row that actually produced checks is already done — an
        # unreachable one has nothing cached, and saying "its checkup is
        # already done" over a table of them was simply false.
        done = [bool(r.get("checks")) for _f, _d, r in picks]
        head = "Pick a number to open that box:"
        if picks and not any(done):
            head = ("Pick a number to open that box — nothing here was "
                    "checked, so opening one checks it:")
        lines = report + [""] + _indent(head, 3)
        acts = []
        w_n = max([len(d["name"]) for _f, d, _r in picks] + [4])
        w_p = max([len(str(r.get("peer") or "?")) for _f, _d, r in picks]
                  + [4])
        for i, (flag, dev, r) in enumerate(picks, 1):
            detail = (r.get("verdict") or "?") + (
                "" if r.get("checks") or not any(done)
                else " — opening it checks again")
            lines += _rowlines(i, flag, dev["name"], r.get("peer") or "?",
                               detail, w_n, w_p)
            acts.append({"label": dev["name"],
                         "run": (lambda d=dev: hub_screen(ctx)
                                 if d["kind"] == "hub"
                                 else device_screen(ctx, d, back="back"))})
        return lines, acts
    return _pump(ctx, build, 1, typed=True)


def _sweep_flag(r):
    v = r.get("verdict") or "?"
    if v == "OK":
        return "⚠️" if any(f.get("severity") == "warn"
                           for f in r.get("findings") or []) else "✅"
    if v.startswith("("):
        return "•"
    return "❌" if r.get("codes") else "⚠️"


# --- the front door: off the hub, no hub in reach --------------------------

def render_front_door(ctx):
    """The most-seen screen in a public repo: anyone who clones this and
    types ./cabinetconfig out of curiosity lands here. It teaches, it never
    scolds, and it leads with purpose — never with what it failed to find."""
    acts = []
    lines = ["", _b("CabiNet — fleet repair"), ""]
    lines += _wrapped(
        "CabiNet runs a room of real slot machines from one small Linux box. "
        "This tool is that room's repair bench: when a machine or a helper Pi "
        "will not come up, it names in plain words which of the look-alike "
        "broken states you are actually in, then performs the few repairs "
        "that have no other path. A healthy floor never needs it.", 0)
    lines.append("")
    lines += _wrapped(
        "It runs ON THE HUB — the Raspberry Pi that serves the slot network, "
        "hands out its addresses, and hosts the floor at "
        "http://192.168.50.2:8081. The machines, the helper Pis and the "
        "repair authority all live there.", 0)
    lines.append("")
    lines.append("To use it, get onto the hub and run it from your clone:")
    lines += _cmds("ssh <you>@<your-hub>", "cd ~/CabiNet", "./cabinetconfig")
    lines += _wrapped(
        "If your hub IS reachable from this computer on some address, say so "
        "and the whole floor opens up here, read-only:", 0)
    lines += _cmds("./cabinetconfig --hub http://<hub-address>:8081")
    lines.append("From this computer, these work without a hub:")
    items = [
        ("What this tool can and cannot fix",
         lambda: _page(ctx, render_scope())),
        ("This clone — branch, version, files that differ, hub key",
         lambda: _run_page(ctx, tree_facts, ctx["root"], title="This clone")),
        ("The one-line commands, for scripts and for pasting over ssh",
         lambda: _page(ctx, render_cli_map())),
    ]
    # `--hub none` switches discovery off by design, so looking again can
    # never succeed — and an item that cannot succeed is a dead entry on the
    # one screen more people will see than any other.
    if _can_relocate(ctx):
        items.append(("Look for a hub again", lambda: _relocate(ctx)))
    for i, (label, run) in enumerate(items, 1):
        lines += _wrapped_item(i, label)
        # `r` and this item are the SAME action, so this one rebuilds the
        # screen too — one action, one outcome, whichever way you reach it.
        acts.append({"label": label, "run": run,
                     "recheck": label.startswith("Look for")})
    tried = ctx["ui"].get("tried") or []
    if tried:
        lines.append("")
        lines += _indent("(Looked for a hub at %s — %s answered, which is "
                         "normal off the slot network: it usually sits behind "
                         "its own switch.)"
                         % (", ".join(t[0].split("//")[-1] for t in tried),
                            "neither" if len(tried) == 2 else "none"), 3)
    return lines, acts


def render_scope():
    """The first thing a curious stranger opens, one keystroke after a screen
    that wraps correctly — so it wraps through the same machinery as
    everything else instead of being hand-broken to its own margin."""
    return ["", _b("   What this tool can and cannot fix"), ""] + _pad(
        ["It fixes the boxes AROUND your machines — the hub, and the small "
         "Pis that sit in the cabinets:",
         "   · a Pi that should be talking to a slot machine over SAS but is "
         "not, and which of the three look-alike reasons it is",
         "   · a card reader that stopped answering",
         "   · a hub whose own services are down, stale, or fighting a "
         "squatter",
         "   · a floor where something took an address and then vanished",
         "",
         "It does NOT touch the machines themselves. Denominations, game "
         "enables, ticket headers, the host address inside a machine's "
         "operator menu — those belong to the machine and to the CabiNet web "
         "page. This tool only ever tells you where to go.",
         "",
         "It changes exactly two things, ever, and asks first both times:",
         "   · switching a Pi's SAS service back on",
         "   · restarting a card reader's service",
         "",
         "Everything else it does is looking.",
         "",
         "Setting a NEW box up is a different job, and it lives outside this "
         "tool:",
         "   deploy/smib_setup.sh <user>@<pi>        a Pi wired to a machine",
         "   deploy/companion_setup.sh <user>@<pi>   a Pi with a card reader",
         "   sudo ./deploy/hub_setup.sh              a new hub",
         "Getting new code onto everything:  deploy/update.py"])


def render_cli_map():
    """Rendered from NOUNS, so it can never name a verb that does not
    exist."""
    lines = ["", _b("   The one-line commands"), "",
             "   Grammar:  cabinetconfig <noun> [<target>] <verb>", ""]
    for noun in sorted(NOUNS):
        for verb in sorted(NOUNS[noun]):
            target = "" if noun in NO_TARGET else " <target>"
            mut = "   (asks first; --yes skips)" \
                if (noun, verb) in MUTATING else ""
            lines.append("      cabinetconfig %s%s %s%s"
                         % (noun, target, verb, mut))
    lines += ["", "   --json adds a machine-readable dict as the last line;",
              "   --dry-run shows every plan and changes nothing."]
    return lines


def _can_relocate(ctx):
    """`--hub none` is the documented "do not go looking" knob (and the one
    the gate runs every walk with). With it in effect there is nothing to
    look for, so neither the item nor `r` is offered."""
    return getattr(ctx.get("args"), "hub", None) != "none"


def _relocate(ctx):
    """One keystroke, and the three places become visible: plug into the slot
    switch, press it, and the front door becomes the floor."""
    say("")
    say("Looking for a hub…")
    found = hub_locate(ctx["args"].hub)
    ctx["ui"]["tried"] = found["tried"]
    if not found["url"]:
        # An empty candidate list formatted into a sentence reads "No hub
        # answered at ." — the tool talking to itself.
        where = ", ".join(t[0].split("//")[-1] for t in found["tried"])
        say("   No hub answered at %s." % where if where else
            "   Nothing was tried — hub discovery is switched off here.")
        return None
    _adopt_hub(ctx, found)
    say("   ✅ found the hub at %s (%s)." % (_hub_host(), found["how"]))
    return root_screen(ctx)


def _adopt_hub(ctx, found):
    global HUB_URL
    HUB_URL = found["url"]
    ctx["status"] = found["status"]
    ctx["place"] = "remote"


def front_door(ctx):
    look = _can_relocate(ctx)
    return _pump(ctx, lambda fresh: render_front_door(ctx), 0,
                 on_refresh=(lambda: _relocate(ctx)) if look else None,
                 refresh=look)


# --- entry -----------------------------------------------------------------

def _fresh_ui(ctx):
    """Menu-only session state: cached doctor dicts (so b and every redraw
    are instant instead of 20 more ssh probes), ssh reachability, the board
    the screens route off, and what this session has already touched.
    Nothing in here is a shape any other layer consumes."""
    return {"docs": {}, "reach": {}, "touched": {}, "ents": [],
            "fleet": {"degraded": True, "sats": [], "companions": [],
                      "machines": [], "leases": [], "neigh": [],
                      "probes": {}},
            "tried": ctx.get("tried") or []}


def interactive(ctx):
    """The bare `./cabinetconfig` run. Three places, and each one is the
    whole screen: on the hub, off it with a hub in reach, off it with none."""
    ctx["ui"] = _fresh_ui(ctx)
    if ctx["place"] == "away":
        return front_door(ctx)
    return root_screen(ctx)


def run_mutating(ctx, fn, *args, **kw):
    """Every mutating leaf funnels through here: read-only refusal first,
    then the one-writer lock for exactly the verb's duration."""
    if ctx["mode"] == "read-only":
        # read-only covers TWO identities. In the wrong-clone case the
        # operator IS on the hub and the lock and the key ARE here — telling
        # them to ssh to the box they are sitting on is flatly wrong.
        if (ctx.get("identity") or {}).get("verdict") == "wrong-clone":
            wd = os.path.dirname(ctx["identity"].get("wd") or "")
            for ln in _indent("❌ read-only: this is not the clone your hub "
                              "runs — repairs here would read one tree and "
                              "change another. cd %s and run it from there. "
                              "Nothing was changed."
                              % (wd or "to the tree the hub runs"), 0):
                say(ln)
            return None
        for ln in _indent("❌ repairs run ON THE HUB — this is a read-only "
                          "place (the update lock and the key that reaches "
                          "the Pis live there). Nothing was changed.", 0):
            say(ln)
        return None
    lock = take_lock(ctx["root"])
    if not lock:
        return None
    try:
        return fn(*args, **kw)
    finally:
        release_lock(lock)


def print_banners(ctx, noun=None):
    """The standing warnings a run must not start without — the CLI's copy.

    Every menu screen carries these in its own header, in its own row, or in
    the sentence that replaces a blocked action. The `<noun> <verb>`
    one-liners have no screen at all, so without this they ran with nothing
    said about a dark hub API, a wrong clone, a live tournament, a held
    update lock or a half-finished adoption. Reads keep their exit code;
    this only prints."""
    ident = ctx.get("identity") or {}
    root = ctx["root"]
    out = []
    if ident.get("verdict") == "wrong-clone":
        wd = os.path.dirname(ident.get("wd") or "") or "the tree it runs"
        out.append("⚠️ on a hub box, but not in the clone your hub runs (it "
                   "runs %s, this is %s) — this run can only look." % (wd,
                                                                       root))
    elif ident.get("verdict") != "hub":
        out.append("⚠️ this computer is not the hub — this run can only "
                   "look. Repairs run on the hub, which holds the update "
                   "lock and the key that reaches the Pis.")
        if noun == "hub":
            # doctor_hub reads unit files, ports, the database and the
            # journal — all LOCAL. Aimed at a laptop it says HUB-DOWN about
            # a hub that answers in milliseconds.
            out.append("⚠️ `hub doctor` reads THIS box's unit files, ports, "
                       "database and journal — run it on the hub, or read "
                       "its verdict as a verdict about this computer.")
    elif ctx.get("mode") == "degraded":
        out.append("⚠️ the hub's own floor service is not answering on :8081 "
                   "— reads fall back to leases, the wire and ssh; repairs "
                   "still run and verify PARTIAL.")
    phase = _tournament_phase(ctx.get("status") or {})
    if phase:
        out.append("⚠️ a tournament is %s — repairs that restart anything "
                   "will refuse until it finishes." % phase)
    if lock_holder(root):
        out.append("⚠️ deploy/update.py is running here — repairs wait for "
                   "it.")
    if os.path.exists(os.path.join(root, _u.ADOPT_MARKER)):
        out.append("❌ %s present — the last adoption never completed an "
                   "update; run deploy/update.py before repairing anything."
                   % _u.ADOPT_MARKER)
    for line in out:
        for ln in _indent(line, 0):
            say(ln)


def _journal_tail(t, unit, n=30):
    """The last N journal lines for a unit, verbatim. The doctors GREP
    (edge-logged lines punish tail-and-wait); this leaf is the human read —
    stack traces and startup lines in their own order."""
    step("journal tail — %s on %s" % (unit, t.name))
    out = _q(t, "journalctl -u %s --no-pager -n %d "
                "2>/dev/null || true" % (unit, n)).strip()
    for line in out.splitlines():
        say("   %s" % line)
    if not out:
        say("   (journal empty or not readable here)")
    return {"unit": unit, "lines": out.splitlines()}


# ---------------------------------------------------------------------------
# CLI — <noun> [<target>] <verb>
# ---------------------------------------------------------------------------

def _target_transport(ctx, target):
    """No target means "here" (the hub / loopback reader). A named target:
    a smibId/companionId is NOT a hostname on the floor, so it resolves to
    its reported peer + login through the hub's status first (registered
    tiles persist, so even a stale leg resolves); anything unmatched rides
    ssh as a bare peer/host, and the doctors' reachability gate then names
    a bad guess honestly instead of misdiagnosing it."""
    if not target:
        return LocalTransport()
    st = ctx.get("status")
    st = st if isinstance(st, dict) and "__error__" not in st else {}
    for s in _u.find_satellites(st):
        if target in (s["smibId"], s["peer"]):
            if s["peer"] in ("127.0.0.1", SLOT_PREFIX + "2"):
                return LocalTransport()          # a co-located hub leg
            return SshTransport(s["peer"], user=_u.sat_user(s))
    for c in _u.find_companions(st):
        if target in (c["companionId"], c["peer"]):
            if c["peer"] in ("127.0.0.1", SLOT_PREFIX + "2"):
                return LocalTransport()          # the enrollment reader
            return SshTransport(c["peer"], user=_u.sat_user(c))
    return SshTransport(target)


def _mut(verb_fn):
    """Wrap a mutating verb as a handler: read-only refusal + the one-writer
    lock live in run_mutating, so no leaf can forget them."""
    def handler(ctx, target):
        return run_mutating(ctx, verb_fn, _target_transport(ctx, target),
                            target, ctx["session"], ctx["args"].yes,
                            ctx["args"].dry_run)
    return handler


# handler(ctx, target) -> result dict or None. --json prints the result as
# the LAST stdout line (human lines still flow above it and into the tee).
NOUNS = {
    "fleet": {
        "overview": lambda c, t: fleet_table(
            discover_fleet(api_status(), c["root"])),
        "diff": lambda c, t: three_set_diff(
            discover_fleet(api_status(), c["root"])),
        "facts": lambda c, t: tree_facts(c["root"]),
    },
    "companion": {
        "doctor": lambda c, t: doctor_companion(_target_transport(c, t), t),
        "restart": _mut(verb_companion_restart),
        "journal": lambda c, t: _journal_tail(_target_transport(c, t),
                                              "cabinet-companion"),
    },
    "smib": {
        "doctor": lambda c, t: doctor_smib(_target_transport(c, t), t),
        "up": _mut(verb_smib_up),
        "park": lambda c, t: signpost_hub_park(t),
        "journal": lambda c, t: _journal_tail(_target_transport(c, t),
                                              "cabinet-sas"),
    },
    "hub": {
        "doctor": lambda c, t: doctor_hub(c["root"], LocalTransport(),
                                          api_status()),
    },
    "diag": {"sweep": lambda c, t: sweep(c)},
}
# THE complete mutation surface, as data — the gate asserts over this set
# (frozen-at-two until a failure class earns a verb in) and _mut() is the
# enforcement: nothing else routes through run_mutating.
MUTATING = {("companion", "restart"), ("smib", "up")}
# These always answer about THIS hub — a supplied target would be silently
# ignored, and "I just doctored .60" must never be the hub's answer in
# disguise, so it is refused instead.
NO_TARGET = ("fleet", "hub", "diag")


def dispatch(ctx, noun, words):
    """`<noun> [<target>] <verb>` — one optional target between noun and
    verb, nothing fancier, so every menu leaf stays a copy-pasteable
    one-liner."""
    verbs = NOUNS[noun]
    target = None
    if len(words) == 1:
        verb = words[0]
    elif len(words) == 2:
        target, verb = words
    else:
        say("usage: cabinetconfig %s [<target>] <verb>   verbs: %s"
            % (noun, " ".join(sorted(verbs))))
        return 2
    if verb not in verbs:
        say("❌ unknown verb %r for %r — verbs: %s"
            % (verb, noun, " ".join(sorted(verbs))))
        return 2
    if target and noun in NO_TARGET:
        say("❌ `%s %s` takes no target — it answers about THIS hub, and "
            "ignoring %r would let its answer masquerade as one about "
            "that box." % (noun, verb, target))
        return 2
    result = verbs[verb](ctx, target)
    if ctx["args"].json:
        # None = the verb refused before starting (read-only mode, a held
        # lock) — never silent under --json.
        say(json.dumps(result if result is not None
                       else {"refused": True}))
    # Mutating one-liners report through the exit code too (the
    # `ssh hub cabinetconfig … && …` contract): True = verified on the
    # wire, anything else — refused, failed verify, read-only — is
    # nonzero. A completed --dry-run is a success OF the dry-run, and
    # doctors stay rc 0: they answered the question, whatever the verdict.
    if (noun, verb) in MUTATING and not ctx["args"].dry_run:
        return 0 if result is True else 1
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="cabinetconfig",
        description="CabiNet fleet repair — diagnose first, then the few "
                    "repairs with no other path. A healthy floor never "
                    "needs this tool.",
        epilog="Bare run = the menu. Grammar: cabinetconfig <noun> "
               "[<target>] <verb>. %s. fleet/hub/diag take no target."
               % " · ".join("%s: %s" % (n, " ".join(sorted(NOUNS[n])))
                            for n in sorted(NOUNS)))
    ap.add_argument("noun", nargs="?", choices=sorted(NOUNS),
                    help="what to ask about (omit for the menu)")
    ap.add_argument("words", nargs="*", metavar="target/verb",
                    help="[<target>] <verb> — e.g. `smib smib-bb2 doctor`")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompts")
    ap.add_argument("--dry-run", action="store_true",
                    help="show every plan, change nothing")
    ap.add_argument("--json", action="store_true",
                    help="doctors also emit a machine-readable dict as the "
                         "last stdout line")
    ap.add_argument("--hub", metavar="URL",
                    help="off the hub: read the floor from this hub instead "
                         "of looking for one (`--hub none` skips looking)")
    a = ap.parse_args()

    _open_transcript()
    root = _u.repo_root()

    # TWO independent questions, in this order. IDENTITY decides whether
    # repairs may run (dead-state evidence only, so it still answers on the
    # night cabinet-g2s is dead); REACHABILITY decides what can be SEEN.
    # Conflating them is what made a laptop with a routed VLAN — the whole
    # floor one hop away — declare "no hub evidence" and show nothing.
    ident = hub_identity(root, LocalTransport())
    found = {"url": None, "tried": []}
    if ident["verdict"] != "hub":
        found = hub_locate(a.hub)
        if found["url"]:
            # Intended side effect: off-hub CLI reads (fleet overview, --json)
            # now see the real floor instead of an empty one. No shape change,
            # no exit-code change. On the hub HUB_URL stays loopback, so hub
            # behaviour is byte-identical.
            globals()["HUB_URL"] = found["url"]
    status = found["status"] if found["url"] else api_status()

    if ident["verdict"] != "hub":
        mode = "read-only"
    elif "__error__" in status:
        mode = "degraded"
    else:
        mode = "full"
    place = {"wrong-clone": "wrong-clone",
             "not-hub": "remote" if found["url"] else "away"}.get(
                 ident["verdict"], "degraded" if mode == "degraded" else "hub")
    ctx = {"root": root, "identity": ident, "mode": mode, "args": a,
           "status": status, "session": {"touched": []}, "place": place,
           "tried": found["tried"]}

    if a.noun:
        if place == "remote":
            say("Reading the floor from the hub at %s — repairs run there."
                % _hub_host())
        # The one-liners have no screen to carry the standing warnings.
        print_banners(ctx, a.noun)
        rc = dispatch(ctx, a.noun, a.words)
    else:
        if a.words:
            raise Fail("a verb needs its noun first — try `cabinetconfig "
                       "%s`" % " ".join(["<noun>"] + a.words))
        interactive(ctx)
        rc = 0
    # --json promises the dict as the LAST stdout line; a read-only doctor
    # call touched nothing, so its finish pass would only be noise after the
    # machine-readable tail. Anything touched still gets the full pass.
    if not (a.json and not ctx["session"]["touched"]):
        finish_pass(ctx["session"])
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        say("\n❌ %s" % e)
        sys.exit(1)
    except KeyboardInterrupt:
        say("\nInterrupted — nothing further was changed.")
        sys.exit(130)
