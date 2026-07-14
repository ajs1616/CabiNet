#!/usr/bin/env python3
"""
CabiNet BRING-UP COCKPIT (evidence-ladder build)
==================================================

A dependency-free (stdlib curses) full-screen "bring-up cockpit" for the SMIB's
7" DSI framebuffer console (tty1), running as user 'aj' with NO sudo. Its job:
when the operator powers on an IGT AVP slot machine, everything needed to SEE it
join (or diagnose why it won't) is visible at a glance — passively, with no
keyboard attached.

Top-to-bottom:
  ROW 0  VERDICT BANNER   — one glance-answer word (READY / NO-GO / JOINING /
                            ONLINE / DURABLE / FAULT), reverse, colour = state.
  ROW 1  SERVICES         — six unit dots, up/boot counts, listening ports,
                            PRE-FLIGHT chip.
  ROW 2  NETWORK+CLOCK+PI  — the five GO/NO-GO facts + Pi health.
  (FAULT band)            — 2 red rows below row 2 only while a fault holds.
  BODY (two columns)      — LEFT: join ladder + link vitals.  RIGHT: DHCP
                            host-discovery evidence (upper) + APX011/clock-skew
                            watch & diagnosis (lower).
  EVIDENCE                — merged colour-coded journal (all five units) with a
                            wire-tail toggle, auto-following.
  FOOTER                  — key hints + LIVE/PAUSED + api age + journal state +
                            a persistent red alert pip while any fault holds.

Everything degrades on small/narrow screens and never crashes on resize: every
write goes through the bounds-checked safe() helper; every curses call is
wrapped. Threaded readers + locks own all I/O so the UI thread never blocks.

Data (all read-only, no sudo — 'aj' is in group adm):
  journalctl -u casinonet-{dhcp,g2s,dns,tftp,ntp} -f -o json   (live evidence)
  systemctl is-active/is-enabled <unit>                         (service health)
  ip -br -4 addr show eth0 + /sys/class/net/eth0/carrier        (slot-net link)
  http://127.0.0.1:8081/api/status                              (G2S engine)
  timedatectl                                                   (clock/NTP)
  vcgencmd / df / free                                          (Pi health, opt)
  logs/g2s_wire_*.log                                           (raw wire bytes)

Passive by default; keys are a bonus:
  q quit · p pause · w wire/journal · c clear · ↑↓/PgUp-Dn scroll · End follow
  h/? legend · r force re-poll
"""

import collections
import curses
import datetime
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DHCP_UNIT = "casinonet-dhcp"
G2S_UNIT = "casinonet-g2s"
STACK_UNITS = ["casinonet-dhcp", "casinonet-g2s", "casinonet-dns",
               "casinonet-tftp", "casinonet-ntp"]      # the 5 readiness units
ALL_UNITS = STACK_UNITS + ["casinonet-console"]        # console = the display
UNIT_SHORT = {"casinonet-dhcp": "dhcp", "casinonet-g2s": "g2s",
              "casinonet-dns": "dns", "casinonet-tftp": "tftp",
              "casinonet-ntp": "ntp", "casinonet-console": "con"}

API_URL = "http://127.0.0.1:8081/api/status"
NIC = "eth0"
SERVER_CIDR = "192.168.50.2/24"
CARRIER_PATH = "/sys/class/net/eth0/carrier"
HOST_URI = "http://192.168.50.2:8081/G2S"
WIRE_GLOB = "/home/aj/CasinoNet/G2S/logs/g2s_wire_*.log"
WATCH_PORTS = [67, 8081, 53, 69, 123]                  # for the listen line
MAXLOG = 3000
MAXWIRE = 400

# ---------------------------------------------------------------------------
# Shared state (guarded by locks; the UI thread only ever reads snapshots)
# ---------------------------------------------------------------------------
events = collections.deque(maxlen=MAXLOG)     # (ts, src, level, text)
events_lock = threading.Lock()
wire_lines = collections.deque(maxlen=MAXWIRE)
wire_lock = threading.Lock()

state_lock = threading.Lock()
state = {
    "svc": {u: {"active": "?", "enabled": "?"} for u in ALL_UNITS},
    "eth0_ip": "?",
    "carrier": None,
    "port8081": False,
    "ports": {p: False for p in WATCH_PORTS},
    "clock_synced": False,
    "ntp_active": False,
    "clock_offset": "",
    "temp": None,
    "throttled": None,
    "mem_pct": None,
    "disk_pct": None,
    "api": None,
    "api_ok": False,
    "status_age": None,          # monotonic of last status poll
    "journal_connected": False,
    # journal-derived
    "ms": {},                    # rung -> "HH:MM:SS"
    "rehs_count": 0,
    "apx_journal": False,
    "attempt_start": None,       # monotonic of first commsOnLine this attempt
    "online_since": None,        # monotonic since commsState onLine (durable clk)
    "rehs_at_join": 0,
    # dhcp evidence
    "dhcp": {"seen": False, "mac": None, "opt55": None, "opt60": None,
             "opt43": None, "opt125": None, "leased_ip": None},
    # wire scan
    "wire_name": None,
    "wire_size": 0,
    "apx_wire_count": 0,
    "apx_wire_last": None,
}

stop = threading.Event()
poll_now = threading.Event()     # 'r' forces an immediate status re-poll

# ---------------------------------------------------------------------------
# Glyphs (unicode with automatic ASCII fallback) + text sanitising
# ---------------------------------------------------------------------------
GLYPHS_U = {
    "dot": "●", "half": "◐", "ring": "○", "pend": "·", "ok": "✓", "bad": "✗",
    "up": "▲", "down": "▽", "here": "▶", "star": "★", "cycle": "↻", "fill": "▓",
    "plus": "⊕", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
}
GLYPHS_A = {
    "dot": "#", "half": ">", "ring": ".", "pend": ".", "ok": "+", "bad": "x",
    "up": "^", "down": "v", "here": ">", "star": "*", "cycle": "@", "fill": "#",
    "plus": "+", "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
}
USE_UNICODE = True
USE_BOX = True                 # box-drawing chars ok even when symbols aren't
BOX_KEYS = ("tl", "tr", "bl", "br", "h", "v")
BOX_CHARS = frozenset(GLYPHS_U[k] for k in BOX_KEYS)
G = dict(GLYPHS_U)


def probe_glyphs(force_ascii=False):
    """Choose the glyph table. Unicode symbol glyphs are used only when the
    stdout encoding can carry them AND the console font is likely to render
    them. On a bare framebuffer console (fbcon, TERM=linux or empty) or with
    --ascii, that font shows the filled/symbol glyphs (● ◐ ✓ ✗ ▲ ▶ ★ ▓ ⊕) as
    blank boxes, so force the ASCII table there — but keep the box-drawing
    chars (┌─│), which fbcon does render, whenever the encoding can carry them."""
    global USE_UNICODE, USE_BOX, G
    term = (os.environ.get("TERM") or "").lower()
    enc = (sys.stdout.encoding or "").lower()

    def _encodable(chars):
        try:
            "".join(chars).encode(enc or "ascii")
            return True
        except Exception:
            return False

    if force_ascii or term in ("", "linux"):
        USE_UNICODE = False
        G = dict(GLYPHS_A)
        USE_BOX = _encodable(BOX_CHARS)
        if USE_BOX:                        # restore just the box-drawing glyphs
            for k in BOX_KEYS:
                G[k] = GLYPHS_U[k]
        return
    if _encodable(GLYPHS_U.values()):
        USE_UNICODE = True
        USE_BOX = True
        G = dict(GLYPHS_U)
    else:
        USE_UNICODE = False
        USE_BOX = False
        G = dict(GLYPHS_A)


def _wide(o):
    """Approximate East-Asian-wide test (framebuffer font has no wcwidth)."""
    return (0x1100 <= o <= 0x115F or 0x2329 <= o <= 0x232A or
            0x2E80 <= o <= 0x303E or 0x3041 <= o <= 0x33FF or
            0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or
            0xA000 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or
            0xF900 <= o <= 0xFAFF or 0xFE10 <= o <= 0xFE19 or
            0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60 or
            0xFFE0 <= o <= 0xFFE6)


def sanitize(s):
    """Strip control/astral/wide chars so a stray glyph can't shift a column
    or raise an encode error. 1:1 replacements keep widths honest."""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 9:                       # tab -> space
            out.append(" ")
            continue
        if o < 0x20 or o == 0x7F:        # other control chars
            continue
        if o > 0xFFFF:                   # emoji / astral (e.g. 🎰📋🎉)
            out.append("?")
            continue
        if USE_BOX and ch in BOX_CHARS:  # box-drawing renders on fbcon; keep it
            out.append(ch)
            continue
        if not USE_UNICODE and o > 0x7E:
            out.append("?")
            continue
        if _wide(o):
            out.append("?")
            continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Bounds-checked writer — the proven "never crash on a small console" helper
# ---------------------------------------------------------------------------
def safe(scr, y, x, s, attr=0):
    """Bounds-checked addstr. Returns the x after the written text."""
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return x
    s = sanitize(s)
    s = s[:w - x]
    if not s:
        return x
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass
    return x + len(s)


# ---------------------------------------------------------------------------
# Log classification (kept from the base console)
# ---------------------------------------------------------------------------
def classify(text):
    t = text.lower()
    if "apx011" in t or any(k in t for k in (
            "fatal", "error", "traceback", "refus", "msx004",
            "exception", "failed", "denied", "outbound fail")):
        return "err"
    if any(k in t for k in ("joined", "online", "onlineack", "was accepted",
                            "lease", "offer", " ack", "harvest", "pinned",
                            "started", "success", "bound", "synchronized")):
        return "ok"
    if any(k in t for k in ("re-handshake", "msx003", "expired", "retry",
                            "retr ", "warn", "disconnect", "stale", "timeout")):
        return "warn"
    return "info"


def fmt_ts(realtime_us):
    try:
        return datetime.datetime.fromtimestamp(
            int(realtime_us) / 1_000_000).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


# ---------------------------------------------------------------------------
# Journal parsing -> shared milestones / flags / DHCP evidence
# ---------------------------------------------------------------------------
_re_rehs = re.compile(r"re-handshake\s+#(\d+)", re.I)
_re_opt55 = re.compile(r"\[DHCP\]\s+client\s+(\S+)\s+opt55\s+param-request:\s*(.+)", re.I)
_re_opt60 = re.compile(r"\[DHCP\]\s+client\s+(\S+)\s+opt60\s+vendor-class:\s*(.+)", re.I)
_re_verdict = re.compile(
    r"host-discovery\s+verdict:\s*opt43_requested=(yes|no)\s+opt125_requested=(yes|no)", re.I)
_re_mac = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")
_re_ip50 = re.compile(r"(192\.168\.50\.\d{1,3})")


def record_journal(ts, src, sub):
    """Parse one journal line for milestones, faults and DHCP evidence.
    Called under no lock; acquires state_lock internally."""
    low = sub.lower()
    with state_lock:
        ms = state["ms"]
        # ---- G2S join milestones ----
        if "re-handshake #" in low:
            m = _re_rehs.search(low)
            if m:
                state["rehs_count"] = max(state["rehs_count"], int(m.group(1)))
            ms["r1"] = ts                       # a re-commsOnLine restarts rung 1
            if state["attempt_start"] is None:
                state["attempt_start"] = time.monotonic()
        elif "commsonline #" in low:
            ms["r1"] = ts
            if re.search(r"#1(\D|$)", low):     # fresh attempt (#1 only, not #1x)
                for k in ("r2", "r3", "r4", "r5"):
                    ms.pop(k, None)
            if state["attempt_start"] is None:
                state["attempt_start"] = time.monotonic()
        if "was accepted" in low or "in sync" in low:
            ms["r2"] = ms.get("r2", ts)
            ms["r3"] = ts
        if "commsdisabledack" in low:
            ms["r3"] = ts
        if "setcommsstate enable=true" in low:
            ms["r4"] = ts
        if ("machine joined" in low or "g2s_online" in low or
                "commsstate=g2s_online" in low):
            ms["r5"] = ts
        if "apx011" in low:
            state["apx_journal"] = True
        # ---- DHCP host-discovery contract lines ----
        if "[dhcp]" in low or src == "DHCP":
            d = state["dhcp"]
            m = _re_opt55.search(sub)
            if m:
                d["mac"], d["opt55"], d["seen"] = m.group(1), m.group(2).strip(), True
            m = _re_opt60.search(sub)
            if m:
                d["mac"] = d["mac"] or m.group(1)
                d["opt60"] = m.group(2).strip().strip("'\"")
                d["seen"] = True
            m = _re_verdict.search(sub)
            if m:
                d["opt43"], d["opt125"], d["seen"] = \
                    m.group(1).lower(), m.group(2).lower(), True
            if "lease" in low or "leased" in low or "bound" in low:
                ip = _re_ip50.search(sub)
                mac = _re_mac.search(sub)
                if ip:
                    d["leased_ip"] = ip.group(1)
                if mac:
                    d["mac"] = d["mac"] or mac.group(1)


def journal_reader():
    with events_lock:
        events.append((datetime.datetime.now().strftime("%H:%M:%S"),
                       "SYS", "info", "connecting to journal…"))
    cmd = ["journalctl"]
    for u in STACK_UNITS:
        cmd += ["-u", u]
    cmd += ["-f", "-o", "json", "-n", "200", "--no-pager"]
    while not stop.is_set():
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
            with state_lock:
                state["journal_connected"] = True
            for line in p.stdout:
                if stop.is_set():
                    p.terminate()
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                msg = e.get("MESSAGE", "")
                if isinstance(msg, list):
                    try:
                        msg = bytes(msg).decode("utf-8", "replace")
                    except Exception:
                        msg = str(msg)
                unit = e.get("_SYSTEMD_UNIT", "")
                src = "DHCP" if DHCP_UNIT in unit else (
                    "G2S" if G2S_UNIT in unit else "SYS")
                ts = fmt_ts(e.get("__REALTIME_TIMESTAMP", "0"))
                for sub in (str(msg).splitlines() or [""]):
                    sub = sanitize(sub)
                    record_journal(ts, src, sub)
                    with events_lock:
                        events.append((ts, src, classify(sub), sub))
        except FileNotFoundError:
            with events_lock:
                events.append((fmt_ts("0"), "SYS", "err", "journalctl not found"))
            with state_lock:
                state["journal_connected"] = False
            return
        except Exception:
            pass
        with state_lock:
            state["journal_connected"] = False
        stop.wait(1.0)               # journalctl exited; reconnect


# ---------------------------------------------------------------------------
# Service / network / clock / api poller
# ---------------------------------------------------------------------------
def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def probe_ports():
    """Best-effort set of locally-listening ports (ss, /proc/net fallback)."""
    found = set()
    out = _run(["ss", "-H", "-lntu"])
    if out:
        for ln in out.splitlines():
            f = ln.split()
            if len(f) < 5:
                continue
            local = f[4]
            if ":" in local:
                try:
                    found.add(int(local.rsplit(":", 1)[1]))
                except ValueError:
                    pass
        return found
    # /proc/net fallback (hex local address:port; tcp listen == state 0A)
    for path, listen_only in (("/proc/net/tcp", True), ("/proc/net/tcp6", True),
                              ("/proc/net/udp", False), ("/proc/net/udp6", False)):
        try:
            with open(path) as fh:
                next(fh, None)
                for ln in fh:
                    f = ln.split()
                    if len(f) < 4:
                        continue
                    if listen_only and f[3] != "0A":
                        continue
                    try:
                        found.add(int(f[1].rsplit(":", 1)[1], 16))
                    except Exception:
                        pass
        except Exception:
            pass
    return found


def status_reader():
    def isstate(u, verb):
        return (_run(["systemctl", verb, u]).strip() or "?")
    while not stop.is_set():
        svc = {}
        for u in ALL_UNITS:
            svc[u] = {"active": isstate(u, "is-active"),
                      "enabled": isstate(u, "is-enabled")}
        # network
        parts = _run(["ip", "-br", "-4", "addr", "show", NIC]).split()
        eth0_ip = parts[2] if len(parts) >= 3 else "no-ip"
        try:
            with open(CARRIER_PATH) as fh:
                carrier = int(fh.read().strip())
        except Exception:
            carrier = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.4)
            port8081 = (s.connect_ex(("127.0.0.1", 8081)) == 0)
            s.close()
        except Exception:
            port8081 = False
        ports = {p: (p in probe_ports()) for p in WATCH_PORTS}
        ports[8081] = port8081
        # clock
        td = _run(["timedatectl"])
        synced = "system clock synchronized: yes" in td.lower()
        ntp_act = "ntp service: active" in td.lower()
        # api
        api = None
        api_ok = False
        try:
            with urllib.request.urlopen(API_URL, timeout=2) as r:
                api = json.loads(r.read().decode())
                api_ok = True
        except Exception:
            api = None
            api_ok = False
        # commit + derive durable/attempt tracking
        with state_lock:
            state["svc"] = svc
            state["eth0_ip"] = eth0_ip
            state["carrier"] = carrier
            state["port8081"] = port8081
            state["ports"] = ports
            state["clock_synced"] = synced
            state["ntp_active"] = ntp_act
            state["api"] = api
            state["api_ok"] = api_ok
            state["status_age"] = time.monotonic()
            egm = pick_egm(api)
            if egm is None:
                # A transient api/status blip can momentarily drop the EGM entry.
                # Only reset our own timers here; leave the journal-derived
                # evidence (ms / rehs_count / apx_journal) to be driven by the
                # journal reader so one blip can't erase the APX011 / re-handshake
                # flags.
                state["attempt_start"] = None
                state["online_since"] = None
            elif egm.get("commsState") == "onLine":
                if state["online_since"] is None:
                    state["online_since"] = time.monotonic()
                    state["rehs_at_join"] = state["rehs_count"]
                elif state["rehs_count"] > state["rehs_at_join"]:
                    state["online_since"] = time.monotonic()   # hiccup: restart
                    state["rehs_at_join"] = state["rehs_count"]
            else:
                state["online_since"] = None
        poll_now.wait(2.0)
        poll_now.clear()


# ---------------------------------------------------------------------------
# Pi health poller (heavier; slower cadence; all best-effort)
# ---------------------------------------------------------------------------
def health_reader():
    while not stop.is_set():
        temp = throttled = mem_pct = disk_pct = None
        t = _run(["vcgencmd", "measure_temp"])
        m = re.search(r"temp=([\d.]+)", t)
        if m:
            temp = float(m.group(1))
        th = _run(["vcgencmd", "get_throttled"])
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", th)
        if m:
            throttled = m.group(1)
        fr = _run(["free"])
        for ln in fr.splitlines():
            if ln.lower().startswith("mem:"):
                f = ln.split()
                try:
                    total = float(f[1])
                    avail = float(f[6]) if len(f) >= 7 else float(f[3])
                    mem_pct = int(round((total - avail) / total * 100))
                except Exception:
                    pass
                break
        dfout = _run(["df", "-P", "/"])
        lines = dfout.splitlines()
        if len(lines) >= 2:
            m = re.search(r"(\d+)%", lines[1])
            if m:
                disk_pct = int(m.group(1))
        with state_lock:
            state["temp"] = temp
            state["throttled"] = throttled
            state["mem_pct"] = mem_pct
            state["disk_pct"] = disk_pct
        stop.wait(5.0)


# ---------------------------------------------------------------------------
# Wire-log reader: newest g2s_wire_*.log — tail scan for APX011 + 'w' view
# ---------------------------------------------------------------------------
def wire_reader():
    while not stop.is_set():
        name = None
        try:
            files = glob.glob(WIRE_GLOB)
            if files:
                name = max(files, key=os.path.getmtime)
        except Exception:
            name = None
        size = 0
        apx_count = 0
        apx_last = None
        tail_lines = []
        if name:
            try:
                size = os.path.getsize(name)
                with open(name, "rb") as fh:
                    if size > 65536:
                        fh.seek(-65536, os.SEEK_END)
                    data = fh.read().decode("utf-8", "replace")
                lines = data.splitlines()
                tail_lines = lines[-MAXWIRE:]
                for ln in lines:
                    if "apx011" in ln.lower():
                        apx_count += 1
                        m = re.match(r"\S+\s+(\d{2}:\d{2}:\d{2})", ln)
                        if m:
                            apx_last = m.group(1)
            except Exception:
                pass
        with state_lock:
            state["wire_name"] = name
            state["wire_size"] = size
            state["apx_wire_count"] = apx_count
            state["apx_wire_last"] = apx_last
        with wire_lock:
            wire_lines.clear()
            for ln in tail_lines:
                wire_lines.append(sanitize(ln))
        stop.wait(2.0)


# ---------------------------------------------------------------------------
# Derived view helpers
# ---------------------------------------------------------------------------
def pick_egm(api):
    if not isinstance(api, dict):
        return None
    egms = [v for k, v in api.items() if k != "_engine" and isinstance(v, dict)]
    if not egms:
        return None
    return max(egms, key=lambda a: a.get("lastSeen") or 0)


def short_loc(loc):
    if not loc:
        return "?"
    m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", str(loc))
    if m:
        return "." + m.group(1).split(".")[-1] + ":" + m.group(2)
    return str(loc)[:16]


STATE_IDX = {"closed": 0, "opening": 1, "sync": 3, "onLine": 5}


def build_view():
    """Snapshot shared state under lock into a plain dict for rendering."""
    with state_lock:
        v = {
            "svc": {u: dict(d) for u, d in state["svc"].items()},
            "eth0_ip": state["eth0_ip"], "carrier": state["carrier"],
            "port8081": state["port8081"], "ports": dict(state["ports"]),
            "clock_synced": state["clock_synced"], "ntp_active": state["ntp_active"],
            "temp": state["temp"], "throttled": state["throttled"],
            "mem_pct": state["mem_pct"], "disk_pct": state["disk_pct"],
            "api": state["api"], "api_ok": state["api_ok"],
            "status_age": state["status_age"],
            "journal_connected": state["journal_connected"],
            "ms": dict(state["ms"]), "rehs_count": state["rehs_count"],
            "apx_journal": state["apx_journal"],
            "attempt_start": state["attempt_start"],
            "online_since": state["online_since"],
            "dhcp": dict(state["dhcp"]),
            "wire_name": state["wire_name"], "wire_size": state["wire_size"],
            "apx_wire_count": state["apx_wire_count"],
            "apx_wire_last": state["apx_wire_last"],
        }
    api = v["api"]
    egm = pick_egm(api)
    v["egm"] = egm
    v["engine"] = api.get("_engine") if isinstance(api, dict) else None
    now = time.time()          # epoch — for lastSeen/lastKeepAlive ages
    mono = time.monotonic()    # uptime — for our own attempt/durable timers
    # readiness (the five bench_preflight checks)
    svc = v["svc"]
    svc_down = [u for u in STACK_UNITS if svc.get(u, {}).get("active") != "active"]
    net_ok = (v["eth0_ip"] == SERVER_CIDR and v["carrier"] == 1)
    clk_ok = v["clock_synced"] and v["ntp_active"]
    checks = [
        ("5 services active", not svc_down, f"{5 - len(svc_down)}/5 up"),
        ("eth0 .2/24 + carrier", net_ok, "eth0"),
        (":8081 listening", v["port8081"], ":8081"),
        ("clock synced", clk_ok, "clock"),
        ("engine reachable", v["api_ok"], "api"),
    ]
    v["checks"] = checks
    v["failing"] = [c for c in checks if not c[1]]
    # reached rung (0..6) — commsState is authoritative; keepAlive drives rung 6
    reached = 0
    ka_age = None
    if egm:
        # commsState may be a transient string g2s_host emits ('sync(expected)',
        # 'closing', or a 'G2S_'-stripped raw state), so map it robustly — a bare
        # STATE_IDX.get() would return 0 for those and collapse the ladder back
        # to rung 0 right after commsOnLineAck.
        cs = egm.get("commsState") or ""
        reached = (5 if cs == "onLine"
                   else (3 if cs.startswith("sync") else STATE_IDX.get(cs, 0)))
        lka = egm.get("lastKeepAlive") or 0
        if lka:
            ka_age = now - lka
        if reached >= 5 and lka and ka_age is not None and ka_age < 60:
            reached = 6
    v["reached"] = reached
    v["ka_age"] = ka_age
    # fault predicates
    fault = None
    if egm:
        cs = egm.get("commsState")
        of = egm.get("outboundFail") or 0
        rehs = v["rehs_count"]
        eng = v["engine"] or {}
        ka_ms = eng.get("keepaliveMs") or 0
        lka = egm.get("lastKeepAlive") or 0
        apx = v["apx_journal"] or v["apx_wire_count"] > 0
        silent = (cs == "onLine" and ka_ms > 0 and lka and
                  (now - lka) > 3 * (ka_ms / 1000.0))
        stalled = (rehs >= 2 and cs in ("opening", "closed", None))
        if apx:
            fault = ("APX011", "CLOCK SKEW (G2S_APX011"
                     + (f" ×{v['apx_wire_count']}" if v['apx_wire_count'] else "")
                     + ")",
                     "AVP clock runs >30s AHEAD -> it rejects our TTL=30s commands",
                     "set the AVP clock = or BEHIND the server, then power-cycle "
                     "the AVP to re-join.")
        elif silent:
            fault = ("SILENT", "LINK DIED (keepAlive silent)",
                     "keepAlive pulses stopped arriving -> the link went quiet "
                     "after join",
                     "check cable/power to the AVP; it re-handshakes within ~30s.")
        elif of > 0 and cs != "onLine" and rehs >= 1:
            # outboundFail is CUMULATIVE and never reset in g2s_host, so treat it
            # as a live fault only when the link is NOT currently onLine and a
            # re-handshake stall is concurrent — a historical/transient POST
            # failure must not latch over a live-good (onLine) link.
            fault = ("OUT-FAIL", "CAN'T REACH AVP :8080 (outbound fail)",
                     "our POSTs to the AVP's :8080 endpoint can't connect",
                     "check cabling/VLAN; curl -v http://<egm-ip>:8080/ from the "
                     "server box; firewall on the path.")
        elif stalled:
            fault = ("STALLED", "ACK REJECTED (re-handshake loop)",
                     "AVP receives but rejects our commsOnLineAck",
                     "diff our wire bytes vs debug-captures/ + AVP Comm Analyzer.")
    v["fault"] = fault
    # durable timer (monotonic — online_since is set with time.monotonic())
    durable_secs = None
    if v["online_since"] is not None and fault is None:
        durable_secs = mono - v["online_since"]
    v["durable_secs"] = durable_secs
    # elapsed since first commsOnLine (monotonic)
    v["elapsed"] = (mono - v["attempt_start"]) if v["attempt_start"] else None
    # overall banner state
    if v["failing"]:
        st = "BOOT"
    elif egm is None:
        st = "READY"
    elif fault is not None:
        st = "FAULT"
    elif egm.get("commsState") == "onLine":
        st = ("DURABLE" if (durable_secs is not None and durable_secs >= 300
                            and ka_age is not None and ka_age < 45) else "ONLINE")
    else:
        st = "JOINING"
    v["st"] = st
    return v


def mmss(sec):
    if sec is None:
        return "--:--"
    sec = int(sec)
    return f"{sec // 60:d}:{sec % 60:02d}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
C = {}          # colour-pair table (filled in draw())


def col(name, bold=False, rev=False, blink=False):
    a = C.get(name, C.get("info", 0))
    if bold:
        a |= curses.A_BOLD
    if rev:
        a |= curses.A_REVERSE
    if blink:
        a |= curses.A_BLINK
    return a


def state_color(st):
    if st in ("READY", "ONLINE", "DURABLE"):
        return "ok"
    if st == "JOINING":
        return "warn"
    return "err"           # BOOT / FAULT


def banner(scr, w, v):
    st = v["st"]
    base = state_color(st)
    attr = col(base, bold=True, rev=True)
    safe(scr, 0, 0, " " * w, attr)
    left = " CabiNet BRING-UP COCKPIT"
    safe(scr, 0, 0, left, attr)
    # right: carrier / NTP / clock
    cg = (G["up"] if v["carrier"] == 1 else G["down"])
    ng = (G["ok"] if (v["clock_synced"] and v["ntp_active"]) else G["bad"])
    clock = datetime.datetime.now().strftime("%H:%M:%S")
    right = f"eth0 {cg}  NTP {ng}  {clock} "
    rx = max(0, w - len(right))
    safe(scr, 0, rx, right, attr)
    # centre: the glance-answer
    egm = v["egm"]
    egid = egm.get("egmId", "?") if egm else "?"
    blink_word = None
    if st == "BOOT":
        first = v["failing"][0][0] if v["failing"] else "?"
        centre = f"{G['bad']} NO-GO — {len(v['failing'])}: {first}"
    elif st == "READY":
        centre = f"{G['dot']} READY — POWER ON THE AVP"
    elif st == "JOINING":
        cs = (egm.get("commsState") or "?").upper()
        k = min(v["reached"] + 1, 6)
        centre = f"{G['here']} AVP JOINING — {cs} (rung {k}/6)"
    elif st == "ONLINE":
        age = "—" if v["ka_age"] is None else f"{int(v['ka_age'])}s"
        centre = f"{G['dot']} MACHINE ONLINE — {egid} — kA {age}"
    elif st == "DURABLE":
        centre = f"{G['dot']} DURABLE {mmss(v['durable_secs'])} — {egid}"
    else:   # FAULT
        blink_word = v["fault"][0]
        centre = f"{G['fill']} FAULT — "
    lw = len(left)
    cx = max(lw + 2, (w - len(centre) - (len(blink_word) if blink_word else 0)) // 2)
    x = safe(scr, 0, cx, centre, attr)
    if blink_word:
        safe(scr, 0, x, blink_word, col(base, bold=True, rev=True, blink=True))


def services_line(scr, w, v, row):
    svc = v["svc"]
    x = safe(scr, row, 0, "SERVICES ", col("hdr", bold=True))
    up = boot = 0
    for u in ALL_UNITS:
        a = svc.get(u, {}).get("active", "?")
        e = svc.get(u, {}).get("enabled", "?")
        if u in STACK_UNITS:
            if a == "active":
                up += 1
            if e == "enabled":
                boot += 1
        if a == "active":
            gl, cn = G["dot"], "ok"
        elif a == "activating":
            gl, cn = G["half"], "warn"
        else:
            gl, cn = G["ring"], "err"
        x = safe(scr, row, x, gl, col(cn, bold=True))
        x = safe(scr, row, x, UNIT_SHORT[u] + " ", col("info"))
    x = safe(scr, row, x, f" {up}/5 up·{boot}/5 boot  ", col("info"))
    x = safe(scr, row, x, "listen ", col("hdr"))
    for p in WATCH_PORTS:
        lit = v["ports"].get(p, False)
        x = safe(scr, row, x, f":{p} ", col("ok" if lit else "info",
                                            bold=lit) | (0 if lit else curses.A_DIM))
    # PRE-FLIGHT chip, right-justified
    nfail = len(v["failing"])
    if nfail == 0:
        chip = f"PRE-FLIGHT: all {G['ok']} "
        ca = col("ok", bold=True)
    else:
        chip = f"PRE-FLIGHT: {nfail} {G['bad']} "
        ca = col("err", bold=True)
    safe(scr, row, max(x + 1, w - len(chip)), chip, ca)


def net_line(scr, w, v, row):
    # NETWORK group
    x = safe(scr, row, 0, "NETWORK eth0 ", col("hdr", bold=True))
    ip = v["eth0_ip"]
    if ip == SERVER_CIDR:
        ipc = col("ok")
    elif ip in ("no-ip", "?"):
        ipc = col("err", bold=True)
    else:
        ipc = col("warn")
    x = safe(scr, row, x, ip + " ", ipc)
    if v["carrier"] == 1:
        x = safe(scr, row, x, G["up"] + "carrier ", col("ok"))
    else:
        x = safe(scr, row, x, G["down"] + "carrier ", col("err", bold=True))
    if v["port8081"]:
        x = safe(scr, row, x, f":8081 LISTEN{G['ok']}", col("ok"))
    else:
        x = safe(scr, row, x, f":8081 {G['bad']}", col("err", bold=True))
    # CLOCK group
    x = safe(scr, row, x, "   CLOCK ", col("hdr", bold=True))
    if v["clock_synced"]:
        x = safe(scr, row, x, f"synced{G['ok']} ", col("ok"))
    else:
        x = safe(scr, row, x, f"unsynced{G['bad']} ", col("warn", bold=True))
    x = safe(scr, row, x, "NTP " + ("active " if v["ntp_active"] else "off "),
             col("ok" if v["ntp_active"] else "warn"))
    # PI group
    x = safe(scr, row, x, "  PI ", col("hdr", bold=True))
    t = v["temp"]
    if t is None:
        x = safe(scr, row, x, "—°C ", col("info") | curses.A_DIM)
    else:
        tc = "err" if t > 80 else ("warn" if t > 70 else "ok")
        x = safe(scr, row, x, f"{t:.0f}°C ", col(tc))
    thr = v["throttled"]
    if thr is None:
        x = safe(scr, row, x, "thr— ", col("info") | curses.A_DIM)
    else:
        x = safe(scr, row, x, f"thr{thr} ",
                 col("ok" if thr == "0x0" else "warn", bold=(thr != "0x0")))
    if v["mem_pct"] is not None:
        x = safe(scr, row, x, f"mem{v['mem_pct']}% ",
                 col("warn" if v["mem_pct"] > 90 else "info"))
    if v["disk_pct"] is not None:
        x = safe(scr, row, x, f"disk{v['disk_pct']}%",
                 col("warn" if v["disk_pct"] > 90 else "info"))


def fault_band(scr, row, w, v):
    f = v["fault"]
    attr = col("err", bold=True, rev=True)
    fill = G["fill"]
    safe(scr, row, 0, " " * w, attr)
    safe(scr, row + 1, 0, " " * w, attr)
    l1 = f"{fill} FAULT — {f[1]}: {f[2]} {fill}"
    l2 = f"{fill} FIX: {f[3]} {fill}"
    safe(scr, row, 0, l1, attr)
    safe(scr, row + 1, 0, l2, attr)


def draw_box(scr, top, left, height, width, title, bcol, tcol):
    if height < 2 or width < 3:
        return
    tl, tr, bl, br = G["tl"], G["tr"], G["bl"], G["br"]
    hl, vl = G["h"], G["v"]
    # top border with inline title
    safe(scr, top, left, tl + hl, bcol)
    tx = safe(scr, top, left + 2, " " + title + " ", tcol | curses.A_BOLD)
    if tx < left + width - 1:
        safe(scr, top, tx, hl * (left + width - 1 - tx), bcol)
    safe(scr, top, left + width - 1, tr, bcol)
    for r in range(1, height - 1):
        safe(scr, top + r, left, vl, bcol)
        safe(scr, top + r, left + width - 1, vl, bcol)
    safe(scr, top + height - 1, left, bl + hl * (width - 2) + br, bcol)


def rung_lines(v, width):
    """Return list of (glyph, gcol, text, tcol) for the six ladder rungs."""
    egm = v["egm"]
    reached = v["reached"]
    fault = v["fault"]
    ms = v["ms"]
    names = [
        ("commsOnLine", "r1"),
        ("commsOnLineAck", "r2"),
        ("SYNC", "r3"),
        ("setCommsState", "r4"),
        ("G2S_onLine", "r5"),
        ("keepAlive~15s", None),
    ]
    details = ["", "cid=1 → :8080", "commsDisabledAck", "enable=true", "JOINED", "liveness"]
    if egm:
        flags = egm.get("lastFlags") or {}
        truef = [k for k, val in flags.items() if val]
        details[0] = f"#{egm.get('commsOnLineSeen', 0)} " + (truef[0] if truef else "")
        details[1] = f"cid=1 → :8080  accepted" if reached >= 2 else "cid=1 → :8080"
    stalled_rung = reached + 1 if fault else -1
    out = []
    for i, (nm, mk) in enumerate(names, start=1):
        ts = ms.get(mk, "") if mk else ""
        det = details[i - 1]
        if egm is None:
            gl, gc, tc = G["pend"], "info", "info"
            det = det
            dim = True
        elif fault and i == stalled_rung:
            gl, gc, tc = G["bad"], "err", "err"
            det = f"{det}  STALLED — {fault[0]}"
            dim = False
        elif i <= reached:
            gl, gc, tc = G["ok"], "ok", "ok"
            dim = False
        elif i == reached + 1:
            gl, gc, tc = G["half"], "warn", "warn"
            det = f"{det}  ·· waiting ··"
            dim = False
        else:
            gl, gc, tc = G["pend"], "info", "info"
            dim = True
        # rung 6 special text
        if i == 6:
            if v["ka_age"] is not None and reached >= 6:
                gl, gc, tc = G["dot"], "ok", "ok"
                det = f"pulse {int(v['ka_age'])}s ago"
                dim = False
            elif egm and reached == 5:
                det = "·· not yet ··"
        text = f"{i} {nm:<14}{det}"
        if ts:
            text = f"{text}"
        out.append((gl, gc, text, tc, dim, ts))
    return out


def render_ladder_box(scr, v, top, left, height, width):
    draw_box(scr, top, left, height, width,
             "JOIN LADDER · " + (v["egm"].get("egmId", "?") if v["egm"]
                                 else "no EGM (AVP appears OFF)"),
             col("hdr"), col("hdr"))
    cw = width - 4
    r = top + 1
    rungs = rung_lines(v, cw)
    for gl, gc, text, tc, dim, ts in rungs:
        if r >= top + height - 1:
            break
        attr = col(tc, bold=(tc == "err"))
        if dim:
            attr = col("info") | curses.A_DIM
        x = safe(scr, r, left + 2, gl + " ", col(gc, bold=(gc in ("ok", "err"))))
        # right-align the timestamp inside the box
        body = text
        if ts:
            avail = (left + width - 2) - x
            body = text[:max(0, avail - 9)]
            safe(scr, r, x, body, attr)
            safe(scr, r, left + width - 2 - len(ts), ts, col("info") | curses.A_DIM)
        else:
            safe(scr, r, x, body, attr)
        r += 1
    # vitals footer
    egm = v["egm"]
    eng = v["engine"] or {}
    if r < top + height - 1 and egm:
        loc = short_loc(egm.get("egmLocation"))
        cmd = egm.get("hostCommandId", 0)
        seen = egm.get("commsOnLineSeen", 0)
        ok = egm.get("outboundOk", 0)
        fail = egm.get("outboundFail", 0)
        descr = egm.get("descriptorCount", 0)
        ka = "—" if v["ka_age"] is None else f"{int(v['ka_age'])}s"
        line1 = f"LINK {loc} {egm.get('commsState', '?')} onLineSeen={seen} cmd={cmd}"
        safe(scr, r, left + 2, line1, col("g2s"))
        r += 1
        if r < top + height - 1:
            x = safe(scr, r, left + 2, "  out ", col("info"))
            x = safe(scr, r, x, f"ok{ok}/", col("ok" if ok else "info"))
            x = safe(scr, r, x, f"fail{fail}",
                     col("err" if fail else "ok", bold=(fail > 0)))
            x = safe(scr, r, x, f"  RE-HS×{v['rehs_count']}",
                     col("warn" if v["rehs_count"] else "info",
                         bold=(v["rehs_count"] > 0)))
            kac = "info"
            if v["ka_age"] is not None:
                kac = ("ok" if v["ka_age"] < 20 else
                       ("warn" if v["ka_age"] < 45 else "err"))
            x = safe(scr, r, x, f"  kA {ka}", col(kac, bold=(kac == "err")))
            x = safe(scr, r, x, f"  descr{descr}", col("info"))
            r += 1
        if r < top + height - 1:
            el = mmss(v["elapsed"])
            kams = eng.get("keepaliveMs", 0)
            eline = (f"elapsed {el}   engine ka{kams // 1000 if kams else 0}s "
                     f"harvest {'ON' if eng.get('harvest') else 'off'} "
                     f"auto {'ON' if eng.get('autoEnable') else 'off'}")
            safe(scr, r, left + 2, eline, col("info"))
            r += 1
        if r < top + height - 1:
            inline = eng.get("inline")
            safe(scr, r, left + 2,
                 f"inline {'ON — last-resort mode' if inline else 'OFF (POST app-responses to :8080)'}",
                 col("warn" if inline else "info", bold=bool(inline)))
    elif r < top + height - 1:
        safe(scr, r, left + 2, "waiting for AVP power-on…",
             col("info") | curses.A_DIM)


def render_dhcp_box(scr, v, top, left, height, width):
    draw_box(scr, top, left, height, width,
             "DHCP HOST-DISCOVERY · on-wire proof", col("dhcp"), col("dhcp"))
    d = v["dhcp"]
    cw = width - 4
    lines = []
    if not d["seen"]:
        lines.append(("no DHCP request from AVP yet — AVP off or already leased", "info"))
        lines.append(("host is a MANUAL URI (NOT DHCP):", "hdr"))
        lines.append(("  " + HOST_URI, "dhcp"))
        lines.append(("  127.0.0.1 on the AVP screen = host unset", "info"))
    else:
        ip = d["leased_ip"]
        if ip:
            lines.append((f"AVP leased its OWN IP  → {ip}  {G['ok']}", "ok"))
        else:
            lines.append(("AVP lease: awaiting…", "warn"))
        mac = d["mac"] or "?"
        ven = d["opt60"] or "?"
        lines.append((f"MAC {mac}   vendor '{ven}'", "info"))
        if d["opt55"]:
            lines.append((f"opt55 param-req {d['opt55']}", "info"))
        o43, o125 = d["opt43"], d["opt125"]
        surprise = (o43 == "yes" or o125 == "yes")
        lines.append((f"opt43? {(o43 or '?').upper()}   opt125? {(o125 or '?').upper()}",
                      "warn" if surprise else "ok"))
        if surprise:
            lines.append(("↑ AMBER: AVP asked for a host option (NEW knowledge)", "warn"))
        else:
            lines.append((f"→ host is MANUAL-URI only (expected). Type on AVP:", "hdr"))
            lines.append((f"  {HOST_URI}  (127.0.0.1=unset)", "dhcp"))
    r = top + 1
    for text, cn in lines:
        if r >= top + height - 1:
            break
        attr = col(cn, bold=(cn in ("ok",)))
        if cn == "info":
            attr = col("info")
        safe(scr, r, left + 2, text[:cw], attr)
        r += 1


def render_apx_box(scr, v, top, left, height, width):
    draw_box(scr, top, left, height, width,
             "CLOCK-SKEW WATCH (APX011) · DIAGNOSIS", col("hdr"), col("hdr"))
    cw = width - 4
    r = top + 1
    apx = v["apx_journal"] or v["apx_wire_count"] > 0
    pi_ok = v["clock_synced"] and v["ntp_active"]
    if r < top + height - 1:
        if apx:
            n = v["apx_wire_count"]
            safe(scr, r, left + 2,
                 f"APX011 SEEN{f' ×{n}' if n else ''} in wire — CLOCK SKEW"[:cw],
                 col("err", bold=True))
        else:
            safe(scr, r, left + 2,
                 f"Pi clock {'synced ' + G['ok'] if pi_ok else 'UNSYNCED ' + G['bad']}"
                 f"   wire-scan: APX011 not seen {G['ok']}"[:cw],
                 col("ok" if pi_ok else "warn"))
        r += 1
    if r < top + height - 1:
        f = v["fault"]
        if f:
            safe(scr, r, left + 2, f"→ {f[3]}"[:cw], col("err", bold=True))
        else:
            safe(scr, r, left + 2,
                 "OK climbing normally · watch: APX011·RE-HS·out-fail·kA-silence"[:cw],
                 col("ok"))
        r += 1


def render_evidence(scr, top, bottom, w, v, ui):
    """Evidence header rule + auto-following log (journal or wire tail)."""
    src = ui["source"]
    if src == "wire":
        name = os.path.basename(v["wire_name"]) if v["wire_name"] else "(none)"
        head = f"EVIDENCE · WIRE tail {name} ({v['wire_size']}B) "
        toggles = f"[w]journal [p]ause [c]lear "
    else:
        head = "EVIDENCE · journal ⊕ wire "
        toggles = "[w]ire [p]ause [c]lear "
    x = safe(scr, top, 0, head, col("hdr", bold=True))
    rx = max(x + 1, w - len(toggles) - 1)
    fillw = rx - x
    if fillw > 0:
        safe(scr, top, x, G["h"] * fillw, col("hdr") | curses.A_DIM)
    safe(scr, top, rx, toggles, col("info"))
    logtop = top + 1
    rows = max(0, bottom - logtop)
    if src == "wire":
        with wire_lock:
            snap = list(wire_lines)
        total = len(snap)
        end = total if not ui["paused"] else max(0, total - ui["scroll"])
        start = max(0, end - rows)
        r = logtop
        for ln in snap[start:end]:
            lvl = classify(ln)
            safe(scr, r, 0, ln, col(lvl, bold=(lvl == "err")))
            r += 1
            if r >= bottom:
                break
    else:
        with events_lock:
            snap = list(events)
        total = len(snap)
        end = total if not ui["paused"] else max(0, total - ui["scroll"])
        start = max(0, end - rows)
        r = logtop
        for ts, source, level, text in snap[start:end]:
            srccol = (col("dhcp") if source == "DHCP" else
                      (col("g2s") if source == "G2S" else col("hdr")))
            low = text.lower()
            mark = " "
            markcol = col("info")
            if "was accepted" in low or "in sync" in low:
                mark, markcol = G["star"], col("ok", bold=True)
            elif "re-handshake" in low:
                mark, markcol = G["cycle"], col("warn", bold=True)
            elif "apx011" in low:
                mark, markcol = G["bad"], col("err", bold=True)
            elif "[dhcp]" in low and ("opt55" in low or "opt60" in low or "verdict" in low):
                mark, markcol = G["plus"], col("dhcp", bold=True)
            txtcol = col(level, bold=(level == "err"))
            safe(scr, r, 0, ts + " ", col("info"))
            safe(scr, r, 9, "%-4s" % source, srccol | curses.A_BOLD)
            safe(scr, r, 13, mark, markcol)
            safe(scr, r, 15, text, txtcol)
            r += 1
            if r >= bottom:
                break


def render_footer(scr, h, w, v, ui):
    attr = col("hdr", bold=True, rev=True)
    safe(scr, h - 1, 0, " " * w, attr)
    left = " q quit·p pause·w wire·c clear·↑↓ scroll·End follow"
    safe(scr, h - 1, 0, left, attr)
    # right cluster
    age = "?"
    if v["status_age"] is not None:
        age = f"{time.monotonic() - v['status_age']:.1f}s"
    jr = "live" if v["journal_connected"] else "reconnecting"
    fault = v["fault"] is not None
    pip = f"{G['dot']}" + ("!" if fault else "OK")
    live = "PAUSED" if ui["paused"] else f"LIVE {G['dot']}"
    right = f"{live} · api {age} · journal {jr} · {pip} "
    rx = max(len(left) + 1, w - len(right))
    safe(scr, h - 1, rx, right, attr)
    # colourise the pip separately
    pipx = rx + len(right) - len(pip) - 1
    safe(scr, h - 1, pipx, pip,
         (col("err", bold=True, rev=True) if fault else col("ok", bold=True, rev=True)))
    if ui["paused"]:
        safe(scr, h - 1, rx, "PAUSED", col("warn", bold=True, rev=True))


# ---- compact renderers for small screens -----------------------------------
def render_medium(scr, h, w, v, ui):
    banner(scr, w, v)
    services_line(scr, w, v, 1)
    net_line(scr, w, v, 2)
    row = 3
    if v["fault"]:
        fault_band(scr, row, w, v)
        row += 2
    # JOIN LADDER (2-line grid)
    safe(scr, row, 0, ("─ JOIN LADDER — " +
         (v["egm"].get("egmId", "?") if v["egm"] else "no EGM (AVP appears OFF)")
         + " ").ljust(w, G["h"]), col("hdr", bold=True))
    row += 1
    rungs = rung_lines(v, w)
    seg = w // 3
    for grp in (rungs[:3], rungs[3:]):
        if row >= h - 3:
            break
        x = 0
        for gl, gc, text, tc, dim, ts in grp:
            a = col("info") | curses.A_DIM if dim else col(tc, bold=(tc == "err"))
            safe(scr, row, x, gl + " ", col(gc, bold=(gc in ("ok", "err"))))
            safe(scr, row, x + 2, text[:seg - 3], a)
            x += seg
        row += 1
    # DHCP verdict (keep the manual-URI truth)
    if row < h - 3:
        safe(scr, row, 0, "─ DHCP HOST-DISCOVERY ".ljust(w, G["h"]),
             col("dhcp", bold=True))
        row += 1
    d = v["dhcp"]
    if row < h - 3:
        if d["seen"]:
            o43 = (d["opt43"] or "?").upper()
            o125 = (d["opt125"] or "?").upper()
            ipt = d["leased_ip"] or "awaiting"
            safe(scr, row, 0,
                 f"leased {ipt}  opt43?{o43} opt125?{o125} → MANUAL URI {HOST_URI}",
                 col("info"))
        else:
            safe(scr, row, 0,
                 f"no request yet — host is a MANUAL URI (NOT DHCP): {HOST_URI}",
                 col("dhcp"))
        row += 1
    # APX + diagnosis on one line
    if row < h - 3:
        apx = v["apx_journal"] or v["apx_wire_count"] > 0
        if v["fault"]:
            safe(scr, row, 0, f"DIAG: {v['fault'][1]} → {v['fault'][3]}"[:w],
                 col("err", bold=True))
        else:
            safe(scr, row, 0,
                 f"APX011 {'SEEN' if apx else 'none ' + G['ok']} · "
                 f"RE-HS×{v['rehs_count']} · OK climbing normally"[:w], col("ok"))
        row += 1
    render_evidence(scr, row, h - 1, w, v, ui)
    render_footer(scr, h, w, v, ui)


def render_minimal(scr, h, w, v, ui):
    # banner (verdict word only)
    st = v["st"]
    attr = col(state_color(st), bold=True, rev=True)
    safe(scr, 0, 0, " " * w, attr)
    verdict = {"BOOT": "NO-GO", "READY": "READY: POWER ON AVP",
               "JOINING": "AVP JOINING", "ONLINE": "MACHINE ONLINE",
               "DURABLE": "DURABLE", "FAULT": "FAULT " + (v["fault"][0] if v["fault"] else "")}[st]
    safe(scr, 0, 0, " CabiNet · " + verdict, attr)
    clk = datetime.datetime.now().strftime("%H:%M:%S")
    safe(scr, 0, max(0, w - len(clk) - 1), clk, attr)
    # one readiness line
    svc = v["svc"]
    x = 0
    for u in ALL_UNITS:
        a = svc.get(u, {}).get("active", "?")
        gl, cn = ((G["dot"], "ok") if a == "active" else
                  (G["half"], "warn") if a == "activating" else (G["ring"], "err"))
        x = safe(scr, 1, x, gl, col(cn, bold=True))
    x = safe(scr, 1, x, f" {v['eth0_ip']} ", col("ok" if v["eth0_ip"] == SERVER_CIDR else "err"))
    x = safe(scr, 1, x, (G["up"] if v["carrier"] == 1 else G["down"]) + " ",
             col("ok" if v["carrier"] == 1 else "err"))
    x = safe(scr, 1, x, ":8081" + (G["ok"] if v["port8081"] else G["bad"]) + " ",
             col("ok" if v["port8081"] else "err"))
    x = safe(scr, 1, x, "clk" + (G["ok"] if (v["clock_synced"] and v["ntp_active"]) else G["bad"]),
             col("ok" if (v["clock_synced"] and v["ntp_active"]) else "warn"))
    # collapsed join/APX/RE-HS line
    apx = v["apx_journal"] or v["apx_wire_count"] > 0
    cs = v["egm"].get("commsState", "—") if v["egm"] else "no EGM"
    safe(scr, 2, 0,
         f"JOIN {cs} · APX011 {'SEEN' if apx else 'ok'} · RE-HS×{v['rehs_count']}"[:w],
         col("err", bold=True) if (apx or v["fault"]) else col("info"))
    render_evidence(scr, 3, h - 1, w, v, ui)
    render_footer(scr, h, w, v, ui)


def render_help(scr, h, w):
    lines = [
        " CabiNet Cockpit — legend ",
        "",
        f" {G['dot']} up/done   {G['half']} in-progress   {G['ring']}/{G['pend']} pending",
        f" {G['ok']} pass   {G['bad']} fail   {G['up']} carrier-up   {G['down']} carrier-down",
        f" {G['here']} you-are-here   {G['star']} milestone   {G['cycle']} re-handshake",
        "",
        " GREEN good/ready · AMBER in-progress/degraded · RED fault/NO-GO",
        " CYAN DHCP · MAGENTA G2S",
        "",
        " q quit · p pause · w wire/journal · c clear",
        " ↑↓ scroll · PgUp/PgDn · End/g follow · r re-poll · h/? this",
        "",
        " (press any key to close) ",
    ]
    bw = min(w - 4, max(len(x) for x in lines) + 4)
    bh = min(h - 2, len(lines) + 2)
    top = max(0, (h - bh) // 2)
    left = max(0, (w - bw) // 2)
    for r in range(bh):
        safe(scr, top + r, left, " " * bw, col("hdr", rev=True))
    draw_box(scr, top, left, bh, bw, "HELP", col("hdr"), col("hdr"))
    for i, ln in enumerate(lines):
        if i + 1 >= bh - 1:
            break
        safe(scr, top + 1 + i, left + 2, ln, col("hdr"))


# ---------------------------------------------------------------------------
# Main draw loop
# ---------------------------------------------------------------------------
def draw(scr):
    global C
    curses.curs_set(0)
    scr.nodelay(True)
    try:
        curses.start_color()
        curses.use_default_colors()
    except Exception:
        pass
    names = ["ok", "err", "warn", "info", "dhcp", "g2s", "hdr"]
    cols = [curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, -1,
            curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_WHITE]
    C = {}
    for i, (n, c) in enumerate(zip(names, cols), start=1):
        try:
            curses.init_pair(i, c, -1)
            C[n] = curses.color_pair(i)
        except Exception:
            C[n] = 0

    ui = {"paused": False, "scroll": 0, "source": "journal", "help": False}
    last = 0.0
    while True:
        ch = scr.getch()
        if ch in (ord('q'), ord('Q')):
            break
        elif ch in (ord('p'), ord('P')):
            ui["paused"] = not ui["paused"]
        elif ch in (ord('w'), ord('W')):
            ui["source"] = "wire" if ui["source"] == "journal" else "journal"
            ui["scroll"] = 0
        elif ch in (ord('c'), ord('C')):
            if ui["source"] == "wire":
                with wire_lock:
                    wire_lines.clear()
            else:
                with events_lock:
                    events.clear()
            ui["scroll"] = 0
        elif ch in (ord('h'), ord('H'), ord('?')):
            ui["help"] = not ui["help"]
        elif ch in (ord('r'), ord('R')):
            poll_now.set()
        elif ch == curses.KEY_UP:
            ui["scroll"] += 1
            ui["paused"] = True
        elif ch == curses.KEY_DOWN:
            ui["scroll"] = max(0, ui["scroll"] - 1)
        elif ch == curses.KEY_PPAGE:
            ui["scroll"] += 10
            ui["paused"] = True
        elif ch == curses.KEY_NPAGE:
            ui["scroll"] = max(0, ui["scroll"] - 10)
        elif ch in (curses.KEY_END, ord('g')):
            ui["scroll"] = 0
            ui["paused"] = False

        now = time.time()
        if ch == -1 and now - last < 0.25:
            time.sleep(0.05)
            continue
        last = now

        try:
            scr.erase()
            h, w = scr.getmaxyx()
            v = build_view()

            if ui["help"]:
                render_help(scr, h, w)
                scr.refresh()
                continue

            if w < 70 or h < 20:
                render_minimal(scr, h, w, v, ui)
            elif w < 100 or h < 28:
                render_medium(scr, h, w, v, ui)
            else:
                # ---- full two-column cockpit ----
                banner(scr, w, v)
                services_line(scr, w, v, 1)
                net_line(scr, w, v, 2)
                body_top = 3
                if v["fault"]:
                    fault_band(scr, 3, w, v)
                    body_top = 5
                # size the two-column body
                reserve = 1 + 1 + 3      # footer + evidence header + min log rows
                avail = h - body_top - reserve
                body_h = max(8, min(12, avail))
                mid = w // 2
                left_w = mid - 1
                right_x = mid
                right_w = w - mid
                render_ladder_box(scr, v, body_top, 0, body_h, left_w)
                apx_h = 4 if body_h >= 9 else max(3, body_h // 2)
                dhcp_h = body_h - apx_h
                render_dhcp_box(scr, v, body_top, right_x, dhcp_h, right_w)
                render_apx_box(scr, v, body_top + dhcp_h, right_x, apx_h, right_w)
                ev_top = body_top + body_h
                render_evidence(scr, ev_top, h - 1, w, v, ui)
                render_footer(scr, h, w, v, ui)

            scr.refresh()
        except curses.error:
            pass


def main():
    probe_glyphs(force_ascii=("--ascii" in sys.argv[1:]))
    threading.Thread(target=journal_reader, daemon=True).start()
    threading.Thread(target=status_reader, daemon=True).start()
    threading.Thread(target=health_reader, daemon=True).start()
    threading.Thread(target=wire_reader, daemon=True).start()
    try:
        curses.wrapper(draw)
    finally:
        stop.set()
        poll_now.set()


if __name__ == "__main__":
    main()
