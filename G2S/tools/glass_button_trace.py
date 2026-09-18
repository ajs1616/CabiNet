#!/usr/bin/env python3
"""glass_button_trace.py — measure the SERVICE button -> glass menu latency.

Run ON the hub (read-only: it only reads the host's own rotating logs):

    python3 G2S/tools/glass_button_trace.py                 # logs/ under G2S/
    python3 G2S/tools/glass_button_trace.py --since 2h      # last two hours
    python3 G2S/tools/glass_button_trace.py --log-dir /home/owner/CabiNet/G2S/logs

Why this exists: "the service menu is sometimes instant, sometimes slow,
sometimes nothing" is an impression. The hub already writes every step of
the press to disk, it just never lines them up. This tool does, per press:

    CBE301 event seen  ->  hook decision  ->  OUT >>> show/hide POSTed
                       ->  OUT <<< EGM answered  (+ the g2sAck verdict)

and names which BRANCH each press took, because the branches have wildly
different costs by design (see _glass_service_button in g2s_host.py):

    show / hide      the resident toggle — one showMediaDisplay or
                     hideMediaDisplay POST. Should be sub-second.
    recovery         the hub did not believe the page was resident, so the
                     press became a full load->activate->show push (5-8 s),
                     and it is throttled to one per 60 s.
    debounced        a CBE301 landed inside the 2 s debounce window after
                     another one — silently ignored by the hook.
    ignored          CBE301 seen but no hook line at all and not inside a
                     debounce window (cabinet not onLine, or the event was a
                     dedupe/backfill — the hub only fires hooks for live,
                     first-seen events).

Columns:
    press      when the hub logged the CBE301 eventReport
    egm-delay  eventDateTime (the cabinet's own stamp) -> hub receipt, if the
               wire log carried the report body. Both clocks are hub-set
               (setDateTime), so this is the EGM's delivery lag, roughly.
    branch     see above
    queue      hook decision -> the POST left the hub (time spent waiting in
               the per-EGM FIFO behind keepAlives / eventAcks / meter acks)
    egm        POST left -> HTTP response came back (the cabinet's own time)
    total      press -> EGM answered the show/hide (for a recovery press
               this is only the loadContent leg — the menu appears several
               seconds later, after activate + show; see the 🪟 lines)
    ack        the g2sAck verdict on that POST (ok / errorCode / no ack)

Then a summary: counts per branch, median/p90/max of total, and the
suspicious patterns worth a look — a show followed by a hide within a few
seconds with only one physical press (a late chirp re-toggling the menu),
and presses that answered nothing.

Stdlib only. Never touches the live host, never writes anything.
"""

import argparse
import datetime as dt
import glob
import html
import os
import re
import statistics
import sys

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) ")
# host log: "<ts> <LEVEL>   <message>"
HOST_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<lvl>[A-Z]+)\s+(?P<msg>.*)$")
EVENT_LINE = re.compile(
    r"🔔 \[(?P<egm>[^\]]+)\] EVENT #(?P<n>\d+) (?P<code>G2S_\w+|IGT_\w+) "
    r"\[[^\]]*\] from (?P<dev>\S+) \(eventId=(?P<eid>[^ )]*)")
HOOK_LINE = re.compile(
    r"🛎️ \[(?P<egm>[^\]]+)\] service button: (?P<what>.*)$")
GLASS_LINE = re.compile(r"🪟 \[(?P<egm>[^\]]+)\] (?P<msg>.*)$")
ACK_OK = re.compile(r"\[(?P<egm>[^\]]+)\] EGM acked (?P<label>\S+)")
ACK_ERR = re.compile(
    r"\[(?P<egm>[^\]]+)\] EGM g2sAck for (?P<label>\S+) carried "
    r"errorCode=(?P<err>\S+)")
# wire log: "<ts> OUT >>> label to url" / "<ts> OUT <<< HTTP 200 for label"
WIRE_OUT = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) OUT >>> "
    r"(?P<label>\S+) to (?P<url>\S+)")
WIRE_IN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) OUT <<< HTTP "
    r"(?P<status>\S+) for (?P<label>\S+)")
WIRE_POST = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) IN  <<< POST ")
EVT_BODY = re.compile(
    r'eventCode="(?P<code>G2S_CBE30[12])"[^>]*?eventDateTime="(?P<edt>[^"]+)"'
    r'|eventDateTime="(?P<edt2>[^"]+)"[^>]*?eventCode="(?P<code2>G2S_CBE30[12])"')
EVT_ID = re.compile(r'eventId="(?P<eid>[^"]+)"')

GLASS_VERBS = ("showMediaDisplay(", "hideMediaDisplay(", "loadContent(",
               "setActiveContent(", "releaseContent(")
DEBOUNCE_SEC = 2.0          # GLASS_BUTTON_DEBOUNCE_SEC in g2s_host.py
RECOVERY_THROTTLE_SEC = 60  # GLASS_RECOVERY_THROTTLE_SEC in g2s_host.py
RETOGGLE_WINDOW_SEC = 6.0   # a show then a hide this close = one press flapping


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


def parse_iso(s):
    """The cabinet's eventDateTime (xs:dateTime, may carry an offset)."""
    s = s.strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)   # to hub-local naive
    return d


def since_to_datetime(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if m:
        n, u = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
        return dt.datetime.now() - dt.timedelta(seconds=n * mult)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        sys.exit(f"--since: use 90m / 2h / 1d or an ISO timestamp, not {s!r}")


def log_files(log_dir, prefix):
    """Every rotating file of one family, oldest first (`.5` .. `.1`, then
    the live file), across every run stamp in the directory."""
    files = glob.glob(os.path.join(log_dir, f"{prefix}_*.log*"))

    def key(p):
        base = os.path.basename(p)
        m = re.match(rf"{prefix}_(\d{{8}}_\d{{6}})\.log(?:\.(\d+))?$", base)
        if not m:
            return ("", 0)
        n = int(m.group(2)) if m.group(2) else 0
        return (m.group(1), -n)
    return sorted(files, key=key)


def read_records(path):
    """Yield (ts, first_line, body) per timestamped record; the body is the
    continuation lines (the wire log writes the XML under the header)."""
    cur = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = TS_RE.match(line)
                if m:
                    if cur:
                        yield cur
                    cur = [parse_ts(m.group(1)), line, []]
                elif cur:
                    cur[2].append(line)
    except OSError as e:
        print(f"  (skip {path}: {e})", file=sys.stderr)
        return
    if cur:
        yield cur


def collect(log_dir, since):
    ev = []            # CBE301/302 events from the host log
    hooks = []         # 🛎️ decisions
    glass = []         # 🪟 narration (recovery pushes, sequencer stages)
    acks = []          # EGM acked / g2sAck errorCode lines
    outs = []          # wire OUT >>> for glass verbs
    ins = []           # wire OUT <<< for glass verbs
    edts = {}          # eventId -> cabinet eventDateTime (from wire bodies)

    for path in log_files(log_dir, "g2s_host"):
        for ts, first, _body in read_records(path):
            if since and ts < since:
                continue
            m = HOST_LINE.match(first)
            if not m:
                continue
            msg = m.group("msg")
            me = EVENT_LINE.search(msg)
            if me and me.group("code") in ("G2S_CBE301", "G2S_CBE302"):
                ev.append({"ts": ts, "egm": me.group("egm"),
                           "code": me.group("code"), "eid": me.group("eid")})
                continue
            mh = HOOK_LINE.search(msg)
            if mh:
                hooks.append({"ts": ts, "egm": mh.group("egm"),
                              "what": mh.group("what")})
                continue
            mg = GLASS_LINE.search(msg)
            if mg:
                glass.append({"ts": ts, "egm": mg.group("egm"),
                              "msg": mg.group("msg")})
                continue
            ma = ACK_OK.search(msg)
            if ma and ma.group("label").startswith(GLASS_VERBS):
                acks.append({"ts": ts, "label": ma.group("label"), "ok": True})
                continue
            ma = ACK_ERR.search(msg)
            if ma and ma.group("label").startswith(GLASS_VERBS):
                acks.append({"ts": ts, "label": ma.group("label"),
                             "ok": False, "err": ma.group("err")})

    for path in log_files(log_dir, "g2s_wire"):
        for ts, first, body in read_records(path):
            if since and ts < since:
                continue
            mo = WIRE_OUT.match(first)
            if mo and mo.group("label").startswith(GLASS_VERBS):
                outs.append({"ts": ts, "label": mo.group("label")})
                continue
            mi = WIRE_IN.match(first)
            if mi and mi.group("label").startswith(GLASS_VERBS):
                ins.append({"ts": ts, "label": mi.group("label"),
                            "status": mi.group("status")})
                continue
            if WIRE_POST.match(first) and body:
                text = html.unescape("\n".join(body))
                if "G2S_CBE30" not in text:
                    continue
                # one eventReport per POST in practice; tolerate several
                for chunk in text.split("<g2s:eventReport")[1:]:
                    mb = EVT_BODY.search(chunk)
                    mid = EVT_ID.search(chunk)
                    if mb and mid:
                        edt = mb.group("edt") or mb.group("edt2")
                        edts[mid.group("eid")] = parse_iso(edt)
    return ev, hooks, glass, acks, outs, ins, edts


def nearest_after(items, ts, key=None, within=None):
    """First item at/after ts (optionally filtered), not beyond `within` s."""
    best = None
    for it in items:
        if it["ts"] < ts:
            continue
        if key and not key(it):
            continue
        if within is not None and (it["ts"] - ts).total_seconds() > within:
            break
        best = it
        break
    return best


def secs(a, b):
    return (b - a).total_seconds()


def fmt(x):
    return "   —  " if x is None else f"{x:6.2f}"


def analyse(ev, hooks, glass, acks, outs, ins, edts):
    presses = []
    used_hooks = set()
    used_outs = set()
    last_press_ts = {}
    for e in ev:
        if e["code"] != "G2S_CBE301":
            continue
        egm = e["egm"]
        p = {"ts": e["ts"], "egm": egm, "eid": e["eid"], "branch": "ignored",
             "hook_ts": None, "out_ts": None, "in_ts": None, "ack": "",
             "verb": "", "egm_delay": None}
        edt = edts.get(e["eid"])
        if edt:
            p["egm_delay"] = secs(edt, e["ts"])
        lp = last_press_ts.get(egm)
        # the hook line lands on the SAME handler thread within ms of the
        # event line; anything later than 1 s belongs to another press
        h = None
        for i, hk in enumerate(hooks):
            if i in used_hooks or hk["egm"] != egm:
                continue
            d = secs(e["ts"], hk["ts"])
            if -0.05 <= d <= 1.0:
                h = (i, hk)
                break
        if h:
            used_hooks.add(h[0])
            p["hook_ts"] = h[1]["ts"]
            what = h[1]["what"]
            if what.startswith("SHOW"):
                p["branch"] = "show"
                p["verb"] = "showMediaDisplay("
            elif what.startswith("hide"):
                p["branch"] = "hide"
                p["verb"] = "hideMediaDisplay("
            elif "recovery push" in what:
                p["branch"] = "recovery"
                p["verb"] = "loadContent("
                # a throttled recovery logs the hook line but no 🪟 push
                g = nearest_after(glass, h[1]["ts"], within=1.5,
                                  key=lambda x: x["egm"] == egm and (
                                      "recovery push" in x["msg"]
                                      or "glassShow" in x["msg"]))
                if g is None:
                    p["branch"] = "recovery-throttled"
                    p["verb"] = ""
                elif "residentShowOnly" in g["msg"] or \
                        "show-only short-circuit" in g["msg"]:
                    p["verb"] = "showMediaDisplay("
        elif lp is not None and secs(lp, e["ts"]) < DEBOUNCE_SEC:
            p["branch"] = "debounced"
        last_press_ts[egm] = e["ts"]
        if p["verb"]:
            start = p["hook_ts"] or e["ts"]
            for i, o in enumerate(outs):
                if i in used_outs or o["ts"] < start:
                    continue
                if not o["label"].startswith(p["verb"]):
                    continue
                if secs(start, o["ts"]) > 30:
                    break
                used_outs.add(i)
                p["out_ts"] = o["ts"]
                p["label"] = o["label"]
                r = nearest_after(ins, o["ts"], within=10,
                                  key=lambda x: x["label"] == o["label"])
                if r:
                    p["in_ts"] = r["ts"]
                    p["http"] = r["status"]
                a = nearest_after(acks, o["ts"], within=10,
                                  key=lambda x: x["label"] == o["label"])
                if a:
                    p["ack"] = "ok" if a["ok"] else a["err"]
                elif r:
                    p["ack"] = f"HTTP {r['status']}, no g2sAck"
                else:
                    p["ack"] = "no reply"
                break
            if p["out_ts"] is None:
                p["ack"] = "never POSTed"
        presses.append(p)
    return presses


def report(presses, ev, since):
    if not presses:
        print("No G2S_CBE301 (service button) events in the window."
              + (f" (since {since})" if since else ""))
        n302 = sum(1 for e in ev if e["code"] == "G2S_CBE302")
        if n302:
            print(f"  ({n302} CBE302 'lamp off' events were seen — the "
                  "cabinet narrates the lamp, not the button?)")
        return
    print(f"{'press':<23} {'egm-delay':>9} {'branch':<19} {'queue':>6} "
          f"{'egm':>6} {'total':>6}  ack")
    totals = []
    for p in presses:
        q = e = t = None
        if p["hook_ts"] and p["out_ts"]:
            q = secs(p["hook_ts"], p["out_ts"])
        if p["out_ts"] and p["in_ts"]:
            e = secs(p["out_ts"], p["in_ts"])
        if p["in_ts"]:
            t = secs(p["ts"], p["in_ts"])
            totals.append(t)
        print(f"{p['ts'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]:<23} "
              f"{fmt(p['egm_delay']):>9} {p['branch']:<19} {fmt(q):>6} "
              f"{fmt(e):>6} {fmt(t):>6}  {p['ack']}")
    print()
    by = {}
    for p in presses:
        by[p["branch"]] = by.get(p["branch"], 0) + 1
    print("branches: " + ", ".join(f"{k}={v}" for k, v in sorted(by.items())))
    if totals:
        s = sorted(totals)
        p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
        print(f"press -> EGM answered: median {statistics.median(s):.2f}s  "
              f"p90 {p90:.2f}s  max {s[-1]:.2f}s  (n={len(s)})")
    n302 = sum(1 for e in ev if e["code"] == "G2S_CBE302")
    print(f"CBE301 seen: {len(presses)}   CBE302 (lamp off) seen: {n302}")

    # --- the smells --------------------------------------------------------
    smells = []
    prev = None            # the last press that DECIDED something
    for p in presses:
        if prev and prev["branch"] == "show" and p["branch"] == "hide" and \
                secs(prev["ts"], p["ts"]) <= RETOGGLE_WINDOW_SEC:
            smells.append(
                f"{p['ts'].strftime('%H:%M:%S')} hide {secs(prev['ts'], p['ts']):.1f}s "
                f"after a show — if that was ONE physical press, a late "
                f"chirp escaped the {DEBOUNCE_SEC:.0f}s debounce and closed the "
                f"menu it had just opened")
        if p["branch"] in ("show", "hide") and p["ack"] == "never POSTed":
            smells.append(f"{p['ts'].strftime('%H:%M:%S')} {p['branch']} "
                          "decided but no matching POST left the hub within "
                          "30s — FIFO stalled, or the job was dropped as a "
                          "superseded epoch")
        if p["branch"] == "recovery-throttled":
            smells.append(f"{p['ts'].strftime('%H:%M:%S')} press hit the "
                          f"{RECOVERY_THROTTLE_SEC}s recovery throttle — "
                          "nothing was sent; the hub did not believe the "
                          "page was resident AND had tried recently")
        if p["branch"] == "recovery":
            smells.append(f"{p['ts'].strftime('%H:%M:%S')} press became a "
                          "full recovery push (load->activate->show, 5-8s) "
                          "— the hub had lost track of the resident page "
                          "(hub restart / rejoin / no SPA heartbeat)")
        if p["branch"] == "ignored":
            smells.append(f"{p['ts'].strftime('%H:%M:%S')} CBE301 with no "
                          "hook decision and outside any debounce window — "
                          "cabinet not onLine at that moment, or a "
                          "deduped/backfilled event")
        if p["branch"] != "debounced":
            prev = p
    if smells:
        print("\nworth a look:")
        for s in smells:
            print("  - " + s)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log-dir", default=None,
                    help="the host's log dir (default: G2S/logs next to "
                         "g2s_host.py, i.e. the unit's WorkingDirectory)")
    ap.add_argument("--since", default=None,
                    help="window: 90m, 2h, 1d, or an ISO timestamp "
                         "(default: everything the rotating logs still hold)")
    args = ap.parse_args()
    log_dir = args.log_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    if not os.path.isdir(log_dir):
        sys.exit(f"no such log dir: {log_dir} (pass --log-dir; the hub unit "
                 "writes logs/ under its WorkingDirectory, G2S/)")
    since = since_to_datetime(args.since)
    hosts = log_files(log_dir, "g2s_host")
    wires = log_files(log_dir, "g2s_wire")
    print(f"reading {len(hosts)} host log file(s) + {len(wires)} wire log "
          f"file(s) in {log_dir}" + (f", since {since}" if since else ""))
    if not wires:
        print("  (no g2s_wire_*.log — queue/egm columns will be empty; the "
              "wire log is where OUT >>> / OUT <<< live)")
    data = collect(log_dir, since)
    presses = analyse(*data)
    report(presses, data[0], since)


if __name__ == "__main__":
    main()
