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
  * Hub-only. Identity is judged on DEAD-STATE evidence (this tree is the one
    the installed cabinet-g2s unit points at, hub unit files exist, .50.2 on a
    NIC) — never on API liveness, because the tool must work on exactly the
    nights cabinet-g2s is dead or :8081 has an imposter. API down = DEGRADED
    mode (discovery falls back to leases + `ip neigh` + SSH probes), not exit.
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
import glob
import json
import os
import re
import shlex
import sys
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
    print(msg, flush=True)


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


def print_banners(ctx):
    """The header truths, worst first. Mode banners (degraded / read-only),
    then the one-writer lock, then the adopt-pending marker."""
    if ctx["mode"] == "degraded":
        say("⚠️  DEGRADED — the hub API (%s/api/status) is not answering. "
            "Doctors still run; discovery falls back to DHCP leases + "
            "`ip neigh` + SSH probes; status-dependent rows gray out."
            % HUB_URL)
    elif ctx["mode"] == "read-only":
        why = {"wrong-clone":
               "this clone is NOT the one the installed cabinet-g2s unit "
               "runs (%s)" % (ctx["identity"]["wd"] or "unit unreadable"),
               "not-hub": "this box shows no hub evidence (no cabinet-* "
                          "units, no 192.168.50.2)"}[ctx["identity"]["verdict"]]
        say("⚠️  READ-ONLY — %s. Doctors and tables work; every repair verb "
            "is refused." % why)
    ph = _tournament_phase(ctx.get("status") or {})
    if ph:
        say("⚠️  tournament is %s — restart verbs will refuse until it "
            "finishes (verbs re-check the live phase themselves)." % ph)
    holder = lock_holder(ctx["root"])
    if holder:
        say("⚠️  %s holds %s — repair verbs will refuse until it finishes."
            % (holder, LOCK_NAME))
    if os.path.exists(os.path.join(ctx["root"], _u.ADOPT_MARKER)):
        say("⚠️  %s present — the last adoption never completed an update; "
            "run deploy/update.py before repairing anything."
            % _u.ADOPT_MARKER)


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
    for r in rows:
        say("   %s %-*s  %-*s  %-*s  %s" % (r["flag"], w_n, r["name"],
                                            w_k, r["kind"], w_p, r["peer"],
                                            r["detail"]))


def fleet_table(fleet):
    """F1 [ro] — the floor table: one aligned row per machine / SMIB Pi /
    companion (+ leases nothing claims), broken rows sorted first. Degraded
    mode renders the lease × ARP × probe fallback table instead, clearly
    marked. Args: `fleet` = discover_fleet() dict. Prints through say();
    returns the row list ([{name, kind, peer, flag, detail}]) for --json."""
    step("F1 — floor table")
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

    step("F2 — three-set diff: lease × ARP × reporting")
    if fleet["degraded"]:
        say("   ⚠️  status dark — the reporting set is empty, so every "
            "wire-live box shows below as 'silent to the hub'. Rerun when "
            "the hub API is back for the real diff.")
    say("   ✅ healthy — hub and wire agree:        %s" % _fmt(rep & wire))
    say("   ⚠️ heard by the hub, no live ARP entry: %s" % _fmt(rep - wire))
    say("   ⚠️ wire-live, but silent to the hub:    %s" % _fmt(wire - rep))
    say("   • leased once, dark now:                %s"
        % _fmt(lease_ips - wire - rep))
    for cl in cloned:
        say("   ❌ CLONED SD CARD — smibId %r reports from %s: the same "
            "identity on two boards flaps its tile and looks like network "
            "trouble. Re-image one board (or pin a registry name on it) "
            "before chasing the network." % (cl["id"], ", ".join(cl["peers"])))
    return {"healthy": sorted(rep & wire),
            "reportingOnly": sorted(rep - wire),
            "arpOnly": sorted(wire - rep),
            "leaseOnly": sorted(lease_ips - wire - rep),
            "cloned": cloned, "degraded": fleet["degraded"]}


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


def tree_facts(root):
    """F3 [ro] — LOCAL facts only, deliberately no network (the product
    never phones home; a remote check is its own explicit action with a
    timeout, and it is not this one): HEAD short-hash + subject, branch,
    dirty-tree count + diffstat tail, update-transcript age
    (G2S/data/update_last.log mtime, read-only), hub key state (~/.ssh/smib
    + .pub), adopt-pending marker. Args: repo root. Prints; returns the
    fact dict."""
    step("F3 — hub/tree facts (local only)")
    facts = {"root": root}
    rc, out = _git(root, "log", "-1", "--format=%h %s")
    if rc == 0 and out.strip():
        facts["head"] = out.strip().splitlines()[0]
        _rc, br = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        facts["branch"] = br.strip() if _rc == 0 else "?"
        say("   ✅ HEAD %s (branch %s)" % (facts["head"], facts["branch"]))
        _rc, porc = _git(root, "status", "--porcelain")
        dirty = [ln for ln in porc.splitlines() if ln.strip()]
        facts["dirty"] = len(dirty)
        if dirty:
            # Diffstat tail, not a judgment: hot dev edits may be deliberate
            # (the floor is the dev floor); update.py owns the refusal table.
            _rc, stat = _git(root, "diff", "--stat")
            tail = stat.strip().splitlines()[-1].strip() if stat.strip() else ""
            say("   ⚠️ dirty tree — %d path(s)%s" %
                (len(dirty), " · %s" % tail if tail else ""))
        else:
            say("   ✅ tree clean")
    else:
        facts["head"], facts["dirty"] = None, None
        say("   ⚠️ not a git clone — update.py's adopt turns this install "
            "into one, in place")
    log_p = os.path.join(root, "G2S/data/update_last.log")
    try:
        facts["updateLogAgeSec"] = int(time.time() - os.path.getmtime(log_p))
        say("   ✅ last update transcript: %s old (G2S/data/update_last.log)"
            % _age_str(facts["updateLogAgeSec"]))
    except OSError:
        facts["updateLogAgeSec"] = None
        say("   • no update transcript yet — update.py has not run here")
    facts["hubKey"] = os.path.exists(DEFAULT_KEY)
    facts["hubKeyPub"] = os.path.exists(DEFAULT_KEY + ".pub")
    if facts["hubKey"]:
        say("   ✅ hub key present (%s%s)"
            % (DEFAULT_KEY, " + .pub" if facts["hubKeyPub"]
               else " — but its .pub is MISSING"))
    else:
        say("   ❌ no %s — satellite ssh (probes, doctors, update.py) has "
            "no key" % DEFAULT_KEY)
    facts["adoptPending"] = os.path.exists(os.path.join(root, _u.ADOPT_MARKER))
    if facts["adoptPending"]:
        say("   ⚠️ %s present — the last adoption never completed an update"
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
    purpose must never become the whole hub's verdict."""
    fatal = [f["code"] for f in doc["findings"] if f["severity"] == "fatal"]
    doc["verdict"] = next((s for s in states if s in doc["codes"]),
                          fatal[0] if fatal else "OK")
    if doc["checks"]:
        w = max(len(c["probe"]) for c in doc["checks"])
        for c in doc["checks"]:
            flag = "✅" if c["ok"] else ("•" if c["ok"] is None else "❌")
            say("   %s %-*s  %s" % (flag, w, c["probe"], c["detail"]))
    if doc["findings"]:
        say("")
    for f in doc["findings"]:
        say("   %s %s — %s" % (_SEV_FLAG[f["severity"]], f["code"],
                               f["evidence"]))
        if f.get("hint"):
            say("      ↳ %s" % f["hint"])
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
        _check(doc, "clock", False, "NTPSynchronized=no")
        _finding(doc, "CLOCK-SKEW", "warn",
                 "this box is not NTP-synchronized",
                 "the hub serves NTP to the slot segment (cabinet-ntp) — "
                 "check it on the hub; a skewed clock scrambles journal "
                 "order and lease/voucher timestamps.")


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
    step("C1 — companion doctor: %s" % doc["target"])
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
    step("S1 — SAS-leg doctor: %s" % doc["target"])
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
                     "a pre-rename casinonet-sas unit file is still installed "
                     "(disabled and inactive — dormant, but one `enable` away "
                     "from fighting cabinet-sas for the tty)",
                     "a smib_setup.sh re-run retires it (keeps it as "
                     ".retired); nothing is polling twice today.")
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
                     "journal: 'SAS DISABLED by hub — parking the poll "
                     "loop' — the hub's own sasEnabled preference parks "
                     "this leg; the satellite then reports online=false "
                     "HONESTLY",
                     "a switch, not a breakage: Switchboard ▸ Machines ▸ "
                     "<machine> ▸ SAS toggle in the web UI (`cabinetconfig "
                     "smib park` prints the full signpost; this tool's "
                     "hub-API write whitelist is EMPTY).")
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
        d = ("online=%s stale=%s reportAgeSec=%s sasEnabled=%s"
             % (e.get("online"), e.get("stale"), e.get("reportAgeSec"),
                e.get("sasEnabled")))
        if e.get("sasEnabled") is False:
            _check(doc, "tile %s" % key, False, d)
            _finding(doc, "HUB-PARKED", "warn",
                     "hub prefs sasEnabled=false for %s — the hub parks "
                     "this leg on every report; online=false is HONEST"
                     % key,
                     "a switch, not a breakage: Switchboard ▸ Machines ▸ "
                     "<machine> ▸ SAS toggle (`cabinetconfig smib park` "
                     "prints the full signpost).")
        elif e.get("stale"):
            _check(doc, "tile %s" % key, False,
                   d + " — the hub stopped hearing us (our report leg, "
                       "or this Pi was down)")
        elif e.get("online"):
            online_any = True
            _check(doc, "tile %s" % key, True, d)
        else:
            _check(doc, "tile %s" % key, False, d)
            _finding(doc, "MACHINE-LEG-OFF", "warn",
                     "%s reports fresh but online=false — the Pi is fine; "
                     "OUR leg isn't reaching the machine (EGM powered "
                     "off, operator-menu channel/address, or wiring)" % key,
                     "the S1.7 bench dance below settles machine-side vs "
                     "wiring in one pass.")
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
        say("   S1.7 first-contact bench dance (signpost — run it YOURSELF "
            "on the Pi; this tool never touches the port):")
        say("      sudo systemctl stop cabinet-sas")
        say("      cd ~/CabiNet/SAS && %s tools/sas_bench_poll.py %s "
            "--credits" % (venv_py, port))
        say("      sudo systemctl start cabinet-sas")
        say("   Reading it, per the hard law (the machine acts as intended; "
            "OUR chain is the variable):")
        say("      SILENCE = our leg isn't reaching the machine — operator "
            "menu (SAS channel enabled? address matches --address?) or "
            "TX/RX not crossed.")
        say("      CRC-BAD = GOOD news: the machine is alive and framing — "
            "swap TX/RX, check GND, capture the frames for "
            "COMPATIBILITY.md.")
    return doc


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
    step("H1 — hub doctor")
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
        d = ("online=%s stale=%s reportAgeSec=%s sasEnabled=%s peer=%s"
             % (e.get("online"), e.get("stale"), e.get("reportAgeSec"),
                e.get("sasEnabled"), e.get("peer")))
        if e.get("stale"):
            _check(doc, "tile %s" % key, False, d)
            _finding(doc, "SATELLITE-SILENT", "warn",
                     "%s: stale — no report for %ss (satellite power, "
                     "network, or its report leg)"
                     % (key, e.get("reportAgeSec")),
                     "ssh the Pi and run the smib doctor — or it is "
                     "simply powered off; a registered tile never "
                     "vanishes, by design.")
        elif e.get("sasEnabled") is False:
            _check(doc, "tile %s" % key, False, d)
            _finding(doc, "HUB-PARKED", "warn",
                     "%s is parked by hub prefs (sasEnabled=false) — the "
                     "satellite polls, the hub tells it to park, and "
                     "online=false is HONEST" % key,
                     "a switch, not a breakage: Switchboard ▸ Machines ▸ "
                     "<machine> ▸ SAS toggle (never leave the BB2 enabled "
                     "at 0 credits; never re-enable a LOCKED machine).")
        elif not e.get("online"):
            _check(doc, "tile %s" % key, False, d)
            _finding(doc, "MACHINE-LEG-DARK", "warn",
                     "%s reports fresh but online=false — the Pi is fine; "
                     "OUR leg isn't reaching the machine (EGM powered "
                     "off, operator-menu channel/address, or wiring)" % key,
                     "run the smib doctor on %s — its S1.7 bench dance "
                     "settles machine-side vs wiring in one pass."
                     % (e.get("peer") or "the Pi"))
        else:
            _check(doc, "tile %s" % key, True, d)

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


def sweep(ctx):
    """D3 [ro] — fleet-wide doctor sweep: doctor_hub first, then doctor_smib
    per satellite and doctor_companion per companion from discover_fleet.
    Unreachable boxes are REPORTED rows (verdict UNREACHABLE), never silent
    skips. Degraded mode still sweeps: the hub doctor runs on __error__
    status by contract, and each ssh-reachable lease/ARP candidate gets BOTH
    doctors — their own unit-census probes (S1.1/C1.1) say which duty the
    box actually has. Prints each doctor then a flagged-first summary table.
    Args: ctx (ctx["local"] overrides the hub-side transport for the gate).
    Returns the list of doctor dicts (hub first) for --json."""
    local = ctx.get("local") or LocalTransport()
    status = api_status()
    fleet = discover_fleet(status, ctx["root"], local=local)
    users = {s["peer"]: _u.sat_user(s) for s in fleet["sats"]}

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

    step("D3 — fleet-wide doctor sweep")
    results = [_row("hub", "hub", SLOT_PREFIX + "2",
                    doctor_hub(ctx["root"], local, status))]
    for s in fleet["sats"]:
        user = users[s["peer"]]
        if not _reach(s["peer"], user):
            results.append({"target": s["smibId"], "kind": "smib",
                            "peer": s["peer"], "codes": ["UNREACHABLE"],
                            "verdict": "UNREACHABLE"})
            continue
        results.append(_row(s["smibId"], "smib", s["peer"],
                            doctor_smib(SshTransport(s["peer"], user=user),
                                        s["smibId"], status)))
    for c in fleet["companions"]:
        user = users.get(c["peer"], _u.SAT_USER)
        if not _reach(c["peer"], user):
            results.append({"target": c["companionId"], "kind": "companion",
                            "peer": c["peer"], "codes": ["UNREACHABLE"],
                            "verdict": "UNREACHABLE"})
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
                results.append({"target": name.get(peer, peer),
                                "kind": "wire", "peer": peer,
                                "codes": ["UNREACHABLE"],
                                "verdict": "UNREACHABLE"})
                continue
            t = SshTransport(peer, user=_u.SAT_USER)
            results.append(_row(name.get(peer, peer), "smib", peer,
                                doctor_smib(t, name.get(peer, peer),
                                            status)))
            results.append(_row(name.get(peer, peer), "companion", peer,
                                doctor_companion(t, name.get(peer, peer),
                                                 status)))

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
        elif v.startswith("("):         # not-built-yet — informational
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
    step("S2 — bring SAS leg up: %s" % (sat or t.name))
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
            "not be yanked — stop the holder first (the S1.2/S1.4 doctor "
            "rows name the usual suspects).")
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
               "; machine leg still dark — the S1.7 bench dance settles "
               "machine-side vs wiring"))
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
    step("C2 — restart cabinet-companion: %s" % (comp or t.name))
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
            "'PN532 ready' (unit %s) — run the companion doctor (C1): "
            "wiring/DIP + i2c probes." % (name, act or "?"))
        return False
    _c, e = _comp_entry(last, comp, peer)
    if e and not e.get("stale") and e.get("readerOk") is False:
        say("❌ %s: restarted and reporting, but readerOk is STILL false — "
            "a restart was not the fix; run the companion doctor (C1): "
            "wiring/DIP, i2c node, 0x24." % name)
    else:
        say("❌ %s: no fresh companions row within 15 s after restart — "
            "run the companion doctor (crash loop? report leg?)." % name)
    return False


def signpost_hub_park(target=None):
    """S3 [signpost] — HUB-PARKED is fixed in the web UI, never here."""
    step("S3 — hub-side park (HUB-PARKED)")
    say("   This SAS leg is parked by the HUB's own preference (sasEnabled ="
        "\n   false in hub.db prefs): the satellite polls, the hub tells it to"
        "\n   park, and the tile honestly shows online=false. That is not a"
        "\n   broken leg — it is a switch, and the web UI owns it, with its"
        "\n   own confirm + reconcile logic. This tool's hub-API write"
        "\n   whitelist is EMPTY, so it will never flip it for you:")
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
# MENU — numbered REPL, three levels max
# ---------------------------------------------------------------------------

def menu(title, entries):
    """One menu level. `entries` = [(label, thunk)]; a thunk returning "quit"
    unwinds the whole tree to the finish pass. Enter/0 = back, q = quit.
    EOF = quit too, so a piped gate run can never hang here."""
    while True:
        say("")
        say("\033[1m%s\033[0m" % title)
        for i, (label, _) in enumerate(entries, 1):
            say(" %d) %s" % (i, label))
        say(" 0) back    q) quit")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return "quit"
        if choice in ("q", "quit"):
            return "quit"
        if choice in ("", "0"):
            return "back"
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            if entries[int(choice) - 1][1]() == "quit":
                return "quit"
        else:
            say("   ? %r — pick a number, Enter/0 for back, q to quit."
                % choice)


def run_mutating(ctx, fn, *args, **kw):
    """Every mutating leaf funnels through here: read-only refusal first,
    then the one-writer lock for exactly the verb's duration."""
    if ctx["mode"] == "read-only":
        say("❌ read-only mode (see the banner) — repair verbs are refused "
            "here. Nothing was changed.")
        return None
    lock = take_lock(ctx["root"])
    if not lock:
        return None
    try:
        return fn(*args, **kw)
    finally:
        release_lock(lock)


def _journal_tail(t, unit, n=30):
    """[ro] The last N journal lines for a unit, verbatim. The doctors GREP
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


def _planned(*items):
    """A deliberately-unbuilt menu leaf. Phasing law: a repair
    verb is admitted ONE AT A TIME, only after its failure class actually
    occurs on a real floor — that is what keeps this tool from growing into
    a second config system. Until then the leaf names itself and stops."""
    def _leaf():
        say("")
        say("   (planned — not built yet; a verb earns its way in when its "
            "failure class actually occurs on a floor)")
        for it in items:
            say("      %s" % it)
    return _leaf


def _pick_device(ctx, kind):
    """The list-pick between menu level and device page (top → list-pick,
    broken sorted first → device). Candidates come from LIVE discovery —
    never a stored inventory; degraded mode simply lists nothing and takes
    a typed peer/IP instead. Returns (transport, name), None on
    back/EOF/a corrected typo, or "quit" (q unwinds here exactly like
    every other prompt — 2am muscle memory must never get DIALED)."""
    fleet = discover_fleet(api_status(), ctx["root"], local=ctx.get("local"))
    rows = []
    if kind == "smib":
        for s in fleet["sats"]:
            flag = ("❌" if s["silent"] else
                    "✅" if any(k["online"] for k in s["keys"]) else "⚠️")
            rows.append([flag, s["smibId"], s["peer"], _u.sat_user(s)])
    else:
        for c in fleet["companions"]:
            flag = ("✅" if c["fresh"] and c.get("readerOk") else
                    "⚠️" if c["fresh"] else "❌")
            rows.append([flag, c["companionId"], c["peer"], _u.SAT_USER])
    rows.sort(key=lambda r: (_FLAG_RANK.get(r[0], 2), r[1]))
    say("")
    for i, r in enumerate(rows, 1):
        say(" %d) %s %s  (%s)" % (i, r[0], r[1], r[2]))
    if not rows:
        say("   (no %s rows in status — hub API dark, or none has ever "
            "reported)" % kind)
    say(" or type a peer/IP · Enter/0 = back · q = quit")
    try:
        choice = input("pick> ").strip()
    except EOFError:
        return None
    if choice.lower() in ("q", "quit"):
        return "quit"
    if choice in ("", "0"):
        return None
    if choice.isdigit():
        if 1 <= int(choice) <= len(rows):
            _f, name, peer, user = rows[int(choice) - 1]
            if peer in ("127.0.0.1", SLOT_PREFIX + "2", ""):
                return LocalTransport(), name    # the hub's own box
            return SshTransport(peer, user=user), name
        # A bare number is never a host — an out-of-range pick gets the
        # correction line, not a doomed ssh dial.
        say("   ? %r — pick a number from the list, Enter/0 for back, "
            "q to quit." % choice)
        return None
    if len(choice) < 2:
        # A stray single keystroke is never a host either.
        say("   ? %r — pick a number, type a peer/IP, Enter/0 for back, "
            "q to quit." % choice)
        return None
    return SshTransport(choice), None            # a typed peer rides ssh


def top_menu(ctx):
    a = ctx["args"]

    def _picked(kind, fn):
        # pick → run; backing out of the pick is a no-op, never an error,
        # and a q at the pick unwinds to the finish pass like any other q
        def _leaf():
            picked = _pick_device(ctx, kind)
            if picked == "quit":
                return "quit"
            if picked:
                return fn(*picked)
        return _leaf

    fleet_m = [
        ("F1 Floor table [ro]", lambda: fleet_table(
            discover_fleet(api_status(), ctx["root"]))),
        ("F2 Three-set diff: lease × ARP × reporting [ro]",
         lambda: three_set_diff(
             discover_fleet(api_status(), ctx["root"]))),
        ("F3 Hub/tree facts [ro]", lambda: tree_facts(ctx["root"])),
    ]
    comp_m = [
        ("C1 Diagnose [ro]",
         _picked("companion", lambda t, n: doctor_companion(t, n))),
        ("C2 Restart cabinet-companion — the wedged-PN532 reset [y/N]",
         _picked("companion", lambda t, n: run_mutating(
             ctx, verb_companion_restart, t, n, ctx["session"],
             a.yes, a.dry_run))),
        ("C9 Tail journal [ro]",
         _picked("companion",
                 lambda t, n: _journal_tail(t, "cabinet-companion"))),
        ("Everything else (C3-C8, C10-C11) — planned", _planned(
            "C3 retire zombie casinonet-companion unit",
            "C4 strip stale baked flags (co-location gated)",
            "C5 fix I2C plumbing / C6 full companion_setup.sh re-drive",
            "C7 add SAS role to this Pi (smib_setup.sh is the path today)",
            "C8 authorize hub key", "C10 support bundle",
            "C11 wiring/DIP triage")),
    ]
    smib_m = [
        ("S1 Diagnose — name the SAS-leg state [ro]",
         _picked("smib", lambda t, n: doctor_smib(t, n))),
        ("S2 Bring SAS leg up (state UNIT-OFF) [y/N]",
         _picked("smib", lambda t, n: run_mutating(
             ctx, verb_smib_up, t, n, ctx["session"], a.yes, a.dry_run))),
        ("S3 Hub-side park (state HUB-PARKED) [signpost]",
         lambda: signpost_hub_park()),
        ("Tail journal [ro]",
         _picked("smib", lambda t, n: _journal_tail(t, "cabinet-sas"))),
        ("Everything else (S4-S13) — planned", _planned(
            "S4 provision the SAS role (state NEVER-PROVISIONED — "
            "smib_setup.sh is the path today)",
            "S5 pin legacy smib-id / S6 fix ExecStart knobs",
            "S7 UART repair / S8 venv repair",
            "S9 first-contact bench dance (the S1 doctor already "
            "signposts it)",
            "S10 reboot satellite / S11 player glass repair",
            "S12 restart cabinet-sas (S2's enable --now covers "
            "bring-up today) / S13 stale-tree check")),
    ]
    hub_m = [
        ("H1 Hub doctor [ro]",
         lambda: doctor_hub(ctx["root"], LocalTransport(), api_status())),
        ("Everything else (H2-H14) — planned", _planned(
            "H2 restart a hub unit (mid-config law + tournament refusal)",
            "H3 half-hub / port squatter · H4 rogue :8081",
            "H5 slot NIC repair · H6 sudoers repair",
            "H7 clear stale update lock · H8 KillMode drift repair",
            "H9 snapshots/restore · H10 enrollment reader",
            "H11 kiosk/console repair · H12 update health",
            "H13 foreign-DHCP probe · H14 authorize hub key")),
    ]
    diag_m = [
        ("D3 Fleet-wide doctor sweep [ro]", lambda: sweep(ctx)),
        ("Everything else (D1-D2, D4-D5) — planned", _planned(
            "D1 machine-join troubleshooter", "D2 support bundles",
            "D4 RAM-clear fingerprint", "D5 journal helpers")),
    ]
    top = [
        ("Fleet overview", lambda: menu("Fleet overview", fleet_m)),
        ("Companion maintenance", lambda: menu("Companion maintenance", comp_m)),
        ("SMIB (SAS) maintenance", lambda: menu("SMIB (SAS) maintenance", smib_m)),
        ("Hub maintenance", lambda: menu("Hub maintenance", hub_m)),
        ("Diagnostics & bundles", lambda: menu("Diagnostics & bundles", diag_m)),
    ]
    while True:
        # Top level: "back" just redraws — quitting is q's job, so a stray
        # Enter can't dump someone out past the finish pass.
        if menu("cabinetconfig — CabiNet fleet repair", top) == "quit":
            return


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
                    help="menu section (omit for the interactive menu)")
    ap.add_argument("words", nargs="*", metavar="target/verb",
                    help="[<target>] <verb> — e.g. `smib smib-bb2 doctor`")
    ap.add_argument("--yes", action="store_true",
                    help="skip [y/N] confirmations")
    ap.add_argument("--dry-run", action="store_true",
                    help="show every plan, change nothing")
    ap.add_argument("--json", action="store_true",
                    help="doctors also emit a machine-readable dict as the "
                         "last stdout line")
    a = ap.parse_args()

    _open_transcript()
    root = _u.repo_root()
    say("cabinetconfig — %s" % root)

    ident = hub_identity(root, LocalTransport())
    status = api_status()
    if ident["verdict"] != "hub":
        mode = "read-only"
    elif "__error__" in status:
        mode = "degraded"
    else:
        mode = "full"
    ctx = {"root": root, "identity": ident, "mode": mode, "args": a,
           "status": status, "session": {"touched": []}}
    print_banners(ctx)

    if a.noun:
        rc = dispatch(ctx, a.noun, a.words)
    else:
        if a.words:
            raise Fail("a verb needs its noun first — try `cabinetconfig "
                       "%s`" % " ".join(["<noun>"] + a.words))
        top_menu(ctx)
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
