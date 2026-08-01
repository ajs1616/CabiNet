#!/usr/bin/env python3
"""Standalone gate for deploy/cabinetconfig.py — the fleet repair menu.

The diagnosis layer IS the product, so what this gate pins is the doctor
CLASSIFICATION (code strings, never prose) — above all the three "SAS leg
off" states NEVER-PROVISIONED / UNIT-OFF / HUB-PARKED, which look identical
from the floor and need three different fixes — plus the laws that keep the
tool a guest and never a second config system:

  * zero writes under any */data path (hub.db is opened mode=ro, ever);
  * the hub-API write whitelist is EMPTY — no POST, no write verb, only the
    read-only /api/hubkey reachability GET;
  * the mutation surface is frozen at exactly two verbs (C2 restart +
    S2 up) until a new failure class earns one in;
  * every refusal path (tournament, HUB-PARKED, NEVER-PROVISIONED,
    read-only rootfs, --dry-run) provably issues ZERO mutating commands;
  * golden --dry-run transcript for S2 — gate-coupling policy: drift on a
    script-replayed fragment fails the update DELIBERATELY, which is the
    forcing function that keeps those fragments minimal.

FIXTURES ARE SYNTHETIC. Shapes mirror real support bundles (status legs,
systemd list-unit-files/cat output, the casinonet-* unit-name pattern), but
every id, MAC, peer, hostname and label below is invented — nothing here is
copied from any collector's bundle, and nothing may ever be: bundles are
private, this repo is public.

Run: python3 G2S/tools/test_cabinetconfig.py
(expects "RESULT: N passed, 0 failed"). No live host, no fleet ssh —
MockTransport fixtures + short subprocess runs of the CLI itself (menu
pipe, bare-checkout import), pointed at a throwaway $HOME so the real
transcript never rotates. GR-04-clean: run it from a scratch checkout.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
DEPLOY = os.path.join(REPO, "deploy")
sys.path.insert(0, DEPLOY)
import cabinetconfig as cc  # noqa: E402

_passed = 0
_failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ✅ %s" % label)
    else:
        _failed += 1
        print("  ❌ %s" % label)


# Every MockTransport journal made here is swept by the law asserts at the
# end — `allow` names the mutations a test EXPECTS (S2's enable, C2's
# restart); anything else mutating, anywhere, fails the gate.
_JOURNALS = []


def mock(responses, allow=(), reachable=True):
    t = cc.MockTransport(responses, reachable=reachable)
    _JOURNALS.append((t.journal, tuple(allow)))
    return t


def run_quiet(fn, *args, **kw):
    """Run a doctor/verb capturing its say() output (say prints)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = fn(*args, **kw)
    return res, buf.getvalue()


class patched_status(object):
    """Verbs fetch api_status() themselves — hand them the fixture."""

    def __init__(self, st):
        self.st = st

    def __enter__(self):
        self._old = cc.api_status
        cc.api_status = lambda timeout=3: self.st
        return self

    def __exit__(self, *exc):
        cc.api_status = self._old


def finding(doc, code):
    return next((f for f in doc["findings"] if f["code"] == code), None)


# ---------------------------------------------------------------------------
# probe-string catalog — the EXACT shell literals cabinetconfig issues
# (grep `_q(t,` in deploy/cabinetconfig.py for the source of each)
# ---------------------------------------------------------------------------

LUF_SAS = ("systemctl list-unit-files cabinet-sas.service "
           "--no-legend --no-pager 2>/dev/null || true")
LUF_COMP = ("systemctl list-unit-files cabinet-companion.service "
            "--no-legend --no-pager 2>/dev/null || true")
LUF_OLD_SAS = ("systemctl list-unit-files 'casinonet-sas*' "
               "--no-legend --no-pager 2>/dev/null || true")
LUF_OLD_COMP = ("systemctl list-unit-files 'casinonet-companion*' "
                "--no-legend --no-pager 2>/dev/null || true")
EN_SAS = "systemctl is-enabled cabinet-sas 2>/dev/null || true"
AC_SAS = "systemctl is-active cabinet-sas 2>/dev/null || true"
CAT_SAS = "systemctl cat cabinet-sas 2>/dev/null || true"
PID_SAS = "systemctl show -p MainPID --value cabinet-sas 2>/dev/null || true"
FUSER = "fuser /dev/ttyAMA0 2>/dev/null || true"
FINDMNT = "findmnt -n -o OPTIONS / 2>/dev/null || true"
IP4 = "ip -4 addr 2>/dev/null || true"
IPROUTE = "ip route 2>/dev/null || true"
BOARD = "tr -d '\\0' < /proc/device-tree/model 2>/dev/null || echo unknown"
CFG_UART = ('CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || '
            'CFG=/boot/config.txt; grep -E '
            '"^enable_uart|^dtoverlay=disable-bt" "$CFG" 2>/dev/null || true')
CMDLINE = ('CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || '
           'CMD=/boot/cmdline.txt; cat "$CMD" 2>/dev/null || true')
HCIUART = "systemctl is-active hciuart 2>/dev/null || true"
GETTY_EN = ("systemctl is-enabled serial-getty@ttyAMA0.service "
            "2>/dev/null || true")
GETTY_AC = ("systemctl is-active serial-getty@ttyAMA0.service "
            "2>/dev/null || true")
LS_PORT = "ls /dev/ttyAMA0 2>/dev/null || true"
ID_NG = "id -nG pi 2>/dev/null || true"
VENV = ("[ -x /home/pi/venvs/cabinet/bin/python ] && { "
        "/home/pi/venvs/cabinet/bin/python -c 'import serial, crcmod, "
        "loguru' >/dev/null 2>&1 && echo venv-ok || echo deps-broken; } "
        "|| echo venv-missing")
J_SAS_GREP = ("journalctl -u cabinet-sas -b --no-pager -o cat "
              "2>/dev/null | grep -E 'cannot open|hub report failed"
              "|parking the poll loop|ImportError"
              "|ModuleNotFoundError' | tail -12 || true")
J_SAS_RAW = "journalctl -u cabinet-sas -b --no-pager -o cat 2>/dev/null"
HUBKEY_GET = ("curl -m 3 -s http://192.168.50.2:8081/api/hubkey "
              ">/dev/null 2>&1 && echo hub-answers || echo hub-dark")
EN_COMP = "systemctl is-enabled cabinet-companion 2>/dev/null || true"
AC_COMP = "systemctl is-active cabinet-companion 2>/dev/null || true"
NRES = ("systemctl show -p NRestarts --value cabinet-companion "
        "2>/dev/null || true")
CAT_COMP = "systemctl cat cabinet-companion 2>/dev/null || true"
CFG_I2C = ('CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || '
           'CFG=/boot/config.txt; grep -E '
           '"^dtoverlay=i2c-gpio|^dtparam=i2c_arm=on" "$CFG" '
           '2>/dev/null || true')
LS_I2C = "ls /dev/i2c-* 2>/dev/null || true"
I2CDET = ("command -v i2cdetect >/dev/null 2>&1 && i2cdetect -y "
          "11 0x24 0x24 2>/dev/null || echo no-i2cdetect")
# hub doctor (FIXROOT never exists — _git and lock probes answer honestly)
FIXROOT = "/fixture/hub"
FIXDB = FIXROOT + "/G2S/data/hub.db"
WD_G2S = ("grep -h '^WorkingDirectory=' /etc/systemd/system/"
          "cabinet-g2s.service 2>/dev/null || true")
LS_HUBUNITS = ("ls /etc/systemd/system/cabinet-g2s.service "
               "/etc/systemd/system/cabinet-dhcp.service "
               "2>/dev/null | wc -l")
ACT5 = ("systemctl is-active cabinet-g2s cabinet-dhcp cabinet-dns "
        "cabinet-ntp cabinet-tftp 2>/dev/null || true")
PID_G2S = "systemctl show -p MainPID --value cabinet-g2s 2>/dev/null || true"
SS_8081 = "ss -ltnp 2>/dev/null | grep ':8081 ' || true"
SS_UDP = "ss -lunp 2>/dev/null | grep -E ':(67|53|123|69) ' || true"
DB_QC = ("python3 -c \"import sqlite3;print(sqlite3.connect("
         "'file:%s?mode=ro',uri=True).execute('pragma quick_check')"
         ".fetchone()[0])\" 2>/dev/null || true" % FIXDB)
DB_SAS_COUNT = ("python3 -c \"import sqlite3;print(sqlite3.connect("
                "'file:%s?mode=ro',uri=True).execute(\\\"select "
                "count(*) from satellites where kind='sas'\\\")"
                ".fetchone()[0])\" 2>/dev/null || true" % FIXDB)
LUF_OLD_ALL = ("systemctl list-unit-files 'casinonet-*' --no-legend "
               "--no-pager 2>/dev/null || true")
J_G2S_SPAM = ("journalctl -u cabinet-g2s -b --no-pager -o cat "
              "2>/dev/null | grep -c 'POST to unexpected path "
              "/G2S127.0.0.1' || true")
KILLMODE = ("systemctl show -p KillMode --value cabinet-g2s "
            "2>/dev/null || true")
DMESG = ("if dmesg >/dev/null 2>&1; then dmesg 2>/dev/null | grep -iE "
         "'mmc[0-9]+.*(error|timeout|fail)' | tail -3; else echo "
         "__dmesg-unreadable__; fi")

# the two allowed mutations — everything else mutating is a gate failure
CMD_S2 = "sudo systemctl enable --now cabinet-sas"
CMD_C2 = "sudo systemctl restart cabinet-companion"

# ---------------------------------------------------------------------------
# fixtures — ALL invented (ids, peers, MACs, labels); shapes only are real
# ---------------------------------------------------------------------------

SMIB = "smib-77aa11"
SPEER = "192.168.50.60"
COMP = "companion-4f9a2c"
CPEER = "192.168.50.61"

UNIT_CAT = (
    "# /etc/systemd/system/cabinet-sas.service\n"
    "[Unit]\n"
    "Description=CabiNet SAS bridge\n"
    "[Service]\n"
    "User=pi\n"
    "WorkingDirectory=/home/pi/CabiNet/SAS\n"
    "ExecStart=/home/pi/venvs/cabinet/bin/python -u sas_host.py "
    "/dev/ttyAMA0 --address 1 --hub http://192.168.50.2:8081\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)

COMP_CAT = """# /etc/systemd/system/cabinet-companion.service
[Service]
User=pi
WorkingDirectory=/home/pi/CabiNet/Companion
ExecStart=/usr/bin/python3 -u companion_host.py
"""


def tile(**over):
    t = {"smibId": SMIB, "address": 1, "peer": SPEER, "online": True,
         "stale": False, "reportAgeSec": 0.4, "sasEnabled": True}
    t.update(over)
    return t


def comp_row(**over):
    r = {"companionId": COMP, "peer": CPEER, "stale": False,
         "reportAgeSec": 2.1, "readerOk": True, "lastError": None,
         "bindings": {"g2sEgmId": None, "sasSmib": None, "sasAddress": None},
         "lastTap": None, "label": "Bench Reader", "assigned": False}
    r.update(over)
    return r


def status_fix(sas=None, companions=None, phase="idle"):
    """The /api/status shape (bundle-derived, values invented): status legs
    at the top level next to EGM entries keyed by egmId."""
    return {
        "IGT_00AA00BB00CC": {"egmId": "IGT_00AA00BB00CC",
                             "egmLocation": "http://192.168.50.31:8080",
                             "commsState": "onLine", "offline": False,
                             "joining": False},
        "sas": dict(sas or {}), "companions": dict(companions or {}),
        "names": {}, "tournament": {"phase": phase},
        "activity": [], "hostOptions": {},
    }


ST_EMPTY = status_fix()
ST_LIVE = status_fix(sas={SMIB + "/1": tile()})
ST_PARKED = status_fix(sas={SMIB + "/1": tile(online=False,
                                              sasEnabled=False,
                                              reportAgeSec=0.6)})
ST_TOURN = status_fix(sas={SMIB + "/1": tile()}, phase="running")

# the verbatim C1.1 sentence — pinned HERE so module-side drift fails loudly
BANNER = ("COMPANION TIER: RFID only — this Pi does NOT poll SAS; SAS needs "
          "deploy/smib_setup.sh on a Pi wired to the machine.")

IP4_SAT = ("2: eth0    inet 192.168.50.60/24 brd 192.168.50.255 "
           "scope global eth0")

# the healthy UNIT-OFF SMIB Pi — clean everywhere except the unit is off
SM_OFF = {
    LUF_SAS: (0, "cabinet-sas.service  disabled  disabled"),
    LUF_OLD_SAS: (0, ""),
    EN_SAS: (0, "disabled"), AC_SAS: (0, "inactive"),
    CAT_SAS: (0, UNIT_CAT),
    IP4: (0, IP4_SAT),
    IPROUTE: (0, "default via 192.168.50.2 dev eth0 proto dhcp "
                 "src 192.168.50.60 metric 100"),
    HUBKEY_GET: (0, "hub-answers"),
    BOARD: (0, "Raspberry Pi Zero 2 W Rev 1.0"),
    CFG_UART: (0, "enable_uart=1\ndtoverlay=disable-bt"),
    CMDLINE: (0, "console=tty1 root=PARTUUID=deadbeef-02 rootfstype=ext4 "
                 "fsck.repair=yes rootwait"),
    HCIUART: (0, "inactive"),
    GETTY_EN: (0, "masked"), GETTY_AC: (0, "inactive"),
    LS_PORT: (0, "/dev/ttyAMA0"),
    ID_NG: (0, "pi adm dialout gpio i2c"),
    VENV: (0, "venv-ok"),
}
SM_UP = dict(SM_OFF)
SM_UP.update({LUF_SAS: (0, "cabinet-sas.service  enabled  enabled"),
              EN_SAS: (0, "enabled"), AC_SAS: (0, "active"),
              J_SAS_GREP: (0, ""),
              J_SAS_RAW: (0, "SAS poller up on /dev/ttyAMA0\n"
                             "reporting to the hub\nmachine online")})

CO_BROKEN = {
    LUF_COMP: (0, "cabinet-companion.service  enabled  enabled"),
    LUF_SAS: (0, ""),
    LUF_OLD_COMP: (0, ""),
    EN_COMP: (0, "enabled"), AC_COMP: (0, "active"),
    NRES: (0, "4"),
    CAT_COMP: (0, COMP_CAT),
    CFG_I2C: (0, "dtoverlay=i2c-gpio,i2c_gpio_sda=23,i2c_gpio_scl=24,"
                 "i2c_gpio_delay_us=2,bus=11"),
    LS_I2C: (0, "/dev/i2c-1\n/dev/i2c-11"),
    I2CDET: (0, "no-i2cdetect"),
}

HUB_OK = {
    WD_G2S: (0, "WorkingDirectory=%s/G2S" % FIXROOT),
    LS_HUBUNITS: (0, "2"),
    IP4: (0, "2: eth0    inet 192.168.50.2/24 brd 192.168.50.255 "
             "scope global eth0"),
    ACT5: (0, "active\nactive\nactive\nactive\nactive"),
    PID_G2S: (0, "1234"),
    SS_8081: (0, 'LISTEN 0 128 0.0.0.0:8081 0.0.0.0:* '
                 'users:(("python3",pid=1234,fd=6))'),
    SS_UDP: (0, 'UNCONN 0 0 0.0.0.0:67 0.0.0.0:* '
                'users:(("python3",pid=1201,fd=5))'),
    DB_QC: (0, "ok"),
    DB_SAS_COUNT: (0, "0"),
    LUF_OLD_ALL: (0, ""),
    J_G2S_SPAM: (0, "0"),
    KILLMODE: (0, "process"),
    FINDMNT: (0, "rw,noatime"),
    DMESG: (0, ""),
    "df -P / 2>/dev/null | tail -1 || true":
        (0, "/dev/root 15000000 5000000 10000000 34% /"),
    "timedatectl show 2>/dev/null || true": (0, "NTPSynchronized=yes"),
}

# S2 --dry-run golden transcript (probe order is the contract; nothing here
# carries a peer or user, and _norm() keeps it that way if one ever leaks in)
GOLDEN_S2 = [LUF_SAS, FINDMNT, EN_SAS, AC_SAS, CAT_SAS, PID_SAS, FUSER]


def _norm(cmds):
    """Peer/user placeholders, so golden fixtures never pin an identity."""
    return [c.replace(SPEER, "<peer>").replace("/home/pi/", "/home/<user>/")
            for c in cmds]


# ---------------------------------------------------------------------------
# 1. doctor classification — the diagnosis layer IS the product
# ---------------------------------------------------------------------------

def t_doctors():
    check("C1.1 banner verbatim in the module", cc._C11_BANNER == BANNER)

    # NEVER-PROVISIONED, with a companion present — the first tester's
    # exact stuck floor (an RFID-only Pi believed to be polling SAS)
    t = mock({LUF_SAS: (0, ""),
              LUF_COMP: (0, "cabinet-companion.service  enabled  enabled"),
              LUF_OLD_SAS: (0, ""), IP4: (0, IP4_SAT)})
    doc, _out = run_quiet(cc.doctor_smib, t, SMIB, ST_EMPTY)
    check("no-unit Pi → verdict NEVER-PROVISIONED",
          doc["verdict"] == "NEVER-PROVISIONED")
    f = finding(doc, "NEVER-PROVISIONED")
    check("…with the COMPANION-TIER banner (companion unit present)",
          f is not None and BANNER in f["hint"])

    # UNIT-OFF — present, disabled/inactive, everything else clean
    t = mock(SM_OFF)
    doc, _out = run_quiet(cc.doctor_smib, t, SMIB, ST_EMPTY)
    check("unit present but off → verdict UNIT-OFF",
          doc["verdict"] == "UNIT-OFF")
    check("…and it is the ONLY code on a clean Pi",
          doc["codes"] == ["UNIT-OFF"])

    # RENAME-CONTENTION — pre-rename casinonet-sas still installed AND armed
    fx = dict(SM_UP)
    fx[LUF_OLD_SAS] = (0, "casinonet-sas.service  enabled  enabled")
    doc, _out = run_quiet(cc.doctor_smib, mock(fx), SMIB, ST_LIVE)
    check("casinonet-sas residue (armed) → RENAME-CONTENTION verdict",
          doc["verdict"] == "RENAME-CONTENTION"
          and "RENAME-CONTENTION" in doc["codes"])

    # RENAME-RESIDUE — the same file DISABLED and inactive is dormant, not
    # contention: a healthy polling leg must not be verdict-buried by a unit
    # that cannot wake (live-caught on a real SMIB 2026-07-29).
    fx = dict(SM_UP)
    fx[LUF_OLD_SAS] = (0, "casinonet-sas.service  disabled  enabled")
    fx["systemctl is-active casinonet-sas 2>/dev/null || true"] = (
        3, "inactive")
    doc, _out = run_quiet(cc.doctor_smib, mock(fx), SMIB, ST_LIVE)
    check("dormant casinonet-sas → RENAME-RESIDUE warn, not contention",
          "RENAME-RESIDUE" in doc["codes"]
          and "RENAME-CONTENTION" not in doc["codes"])
    check("…and a healthy leg still reads OK overall",
          doc["verdict"] == "OK")

    # HUB-PARKED — the hub's own sasEnabled=false preference, tile path
    doc, out = run_quiet(cc.doctor_smib, mock(SM_UP), SMIB, ST_PARKED)
    check("parked tile → verdict HUB-PARKED",
          doc["verdict"] == "HUB-PARKED")
    f = finding(doc, "HUB-PARKED")
    check("…hint carries the Switchboard signpost",
          f is not None and "Switchboard ▸ Machines" in f["hint"])

    # companion doctor: ENXIO + crash loop + never bound + the tier banner
    doc, _out = run_quiet(cc.doctor_companion, mock(CO_BROKEN), COMP,
                          status_fix(companions={COMP: comp_row(
                              readerOk=False,
                              lastError="OSError: [Errno 6] No such device "
                                        "or address")}))
    f = finding(doc, "COMPANION-TIER")
    check("companion doctor: COMPANION-TIER banner, whole and alone",
          f is not None and f["hint"] == BANNER)
    check("ENXIO lastError → READER-NOT-DETECTED",
          "READER-NOT-DETECTED" in doc["codes"])
    check("NRestarts=4 → DAEMON-CRASH-LOOP",
          "DAEMON-CRASH-LOOP" in doc["codes"])
    check("bindings all null → NEVER-BOUND (info)",
          "NEVER-BOUND" in doc["codes"])
    check("companion verdict = first fatal",
          doc["verdict"] == "DAEMON-CRASH-LOOP")

    # hub doctor: empty .sas + zero kind='sas' rows → the never-built-leg
    # check fires, info severity, banner as the hint; verdict stays OK
    doc, _out = run_quiet(cc.doctor_hub, FIXROOT, mock(HUB_OK),
                          status_fix(companions={COMP: comp_row()}))
    f = finding(doc, "NO-SAS-SATELLITE-EVER-REPORTED")
    check("hub: empty sas + db count 0 → NO-SAS-SATELLITE-EVER-REPORTED",
          f is not None)
    check("…info severity (a G2S-only floor is legit) + banner hint",
          f is not None and f["severity"] == "info" and f["hint"] == BANNER)
    check("…hub verdict stays OK", doc["verdict"] == "OK")

    # UNREACHABLE ≠ NEVER-PROVISIONED: a box that answers NOTHING gets the
    # honest verdict — never a re-provisioning sentence for a Pi that is
    # merely powered off or unkeyed
    t = mock({}, reachable=False)
    doc, out = run_quiet(cc.doctor_smib, t, SMIB, ST_EMPTY)
    check("dead box → smib verdict UNREACHABLE, zero probes issued",
          doc["verdict"] == "UNREACHABLE" and t.journal == [])
    check("…hint names power/wire/hub-key, never smib_setup",
          "hub key" in out and "smib_setup" not in out)
    t = mock({}, reachable=False)
    doc, out = run_quiet(cc.doctor_companion, t, COMP, ST_EMPTY)
    check("dead box → companion verdict UNREACHABLE, zero probes",
          doc["verdict"] == "UNREACHABLE" and t.journal == [])
    check("…and no 'hub key authorized' claim for a box never reached",
          "authorized" not in out)

    # readerOk=false WITHOUT ENXIO → the wedge gets a CODE (the D3 sweep
    # folds each box to verdict+codes; no code = an invisible clean row)
    fx = dict(CO_BROKEN)
    fx[NRES] = (0, "0")
    doc, _out = run_quiet(cc.doctor_companion, mock(fx), COMP,
                          status_fix(companions={COMP: comp_row(
                              readerOk=False)}))
    f = finding(doc, "READER-WEDGED")
    check("readerOk=false, no ENXIO → READER-WEDGED (warn), C2 in the hint",
          f is not None and f["severity"] == "warn"
          and "restart" in f["hint"])

    # SILENT-MISMATCH needs a READABLE empty journal — an unreadable one is
    # "could not look", never evidence of a silent stale tree
    fx = dict(SM_UP)
    fx[J_SAS_RAW] = (0, "")
    doc, _out = run_quiet(cc.doctor_smib, mock(fx), SMIB, ST_LIVE)
    check("active + readable-EMPTY journal → SILENT-MISMATCH",
          "SILENT-MISMATCH" in doc["codes"])
    fx = dict(SM_UP)
    fx[J_SAS_RAW] = (1, "")
    doc, _out = run_quiet(cc.doctor_smib, mock(fx), SMIB, ST_LIVE)
    check("unreadable journal → no SILENT-MISMATCH minted",
          "SILENT-MISMATCH" not in doc["codes"])


# ---------------------------------------------------------------------------
# 2. degraded mode — the tool must work best on the worst nights
# ---------------------------------------------------------------------------

def t_degraded(tmp):
    root = os.path.join(tmp, "degraded-root")
    os.makedirs(os.path.join(root, "G2S", "data"))
    with open(os.path.join(root, "G2S", "data", "dhcp_leases.json"),
              "w") as f:
        json.dump({"aa:bb:cc:00:00:60": {"ip": SPEER,
                                         "hostname": "pi-smib-bench"},
                   "aa:bb:cc:00:00:61": {"ip": CPEER, "hostname": ""}}, f)
    t = mock({"ip -4 -o addr 2>/dev/null || true": (0, ""),
              "ip neigh 2>/dev/null || true":
                  (0, "%s dev eth0 lladdr aa:bb:cc:00:00:61 REACHABLE"
                      % CPEER)})
    fleet = cc.discover_fleet({"__error__": "connection refused"},
                              root, local=t)
    check("hub-API-dead → discovery reports degraded",
          fleet["degraded"] is True)
    check("…leases still read (read-only)", len(fleet["leases"]) == 2)
    rows, out = run_quiet(cc.fleet_table, fleet)
    peers = {r["peer"] for r in rows}
    check("…fleet table still renders from leases + ARP",
          peers == {SPEER, CPEER} and "status dark" in out)
    flags = {r["peer"]: r["flag"] for r in rows}
    check("…lease-only box ❌, wire-live-no-ssh box ⚠️",
          flags.get(SPEER) == "❌" and flags.get(CPEER) == "⚠️")


# ---------------------------------------------------------------------------
# 3. verbs — refusals first, then the golden path
# ---------------------------------------------------------------------------

def t_verbs():
    # HUB-PARKED → refusal + the S3 signpost, census probe only
    t = mock({LUF_SAS: (0, "cabinet-sas.service  enabled  enabled")})
    with patched_status(ST_PARKED):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             assume_yes=True)
    check("S2 on a parked leg → refused", not res)
    check("…prints the exact Switchboard path",
          "Switchboard ▸ Machines ▸ %s ▸ SAS toggle" % SMIB in out)
    check("…names the EMPTY write whitelist", "EMPTY" in out)
    check("…journal = the census probe alone, zero writes",
          t.journal == [LUF_SAS])

    # NEVER-PROVISIONED → refusal + banner (companion present)
    t = mock({LUF_SAS: (0, ""),
              LUF_COMP: (0, "cabinet-companion.service  enabled  enabled")})
    with patched_status(ST_EMPTY):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             assume_yes=True)
    check("S2 never-provisioned → refused, banner printed, smib_setup "
          "signposted",
          not res and BANNER in out and "smib_setup.sh" in out)

    # unreachable box → refused with the honest name, zero commands (the
    # census would misread the silence as NEVER-PROVISIONED otherwise)
    t = mock({}, reachable=False)
    with patched_status(ST_EMPTY):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             assume_yes=True)
    check("S2 on an unreachable box → refused as UNREACHABLE, zero probes",
          not res and "UNREACHABLE" in out and t.journal == [])

    # tournament running → refusal before ANY probe
    t = mock({})
    with patched_status(ST_TOURN):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             assume_yes=True)
        res2, out2 = run_quiet(cc.verb_companion_restart, t, COMP,
                               {"touched": []}, assume_yes=True)
    check("tournament running → S2 and C2 both refuse, phase named",
          not res and not res2 and "running" in out and "running" in out2)
    check("…with ZERO probes issued", t.journal == [])

    # read-only rootfs → both write verbs refuse (⚑ the SD-health rail)
    t = mock({LUF_SAS: (0, "cabinet-sas.service  enabled  enabled"),
              FINDMNT: (0, "ro,noatime")})
    with patched_status(ST_EMPTY):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             assume_yes=True)
    check("S2 on a read-only rootfs → refused, card named",
          not res and "READ-ONLY" in out and t.journal == [LUF_SAS, FINDMNT])
    t = mock({LUF_COMP: (0, "cabinet-companion.service  enabled  enabled"),
              FINDMNT: (0, "ro,noatime")})
    with patched_status(ST_EMPTY):
        res, out = run_quiet(cc.verb_companion_restart, t, COMP,
                             {"touched": []}, assume_yes=True)
    check("C2 on a read-only rootfs → refused, zero writes",
          not res and "READ-ONLY" in out and t.journal == [LUF_COMP, FINDMNT])

    # the golden --dry-run: exact probe sequence, no mutation, falsy return
    t = mock(SM_OFF)
    with patched_status(ST_EMPTY):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, {"touched": []},
                             dry_run=True)
    check("S2 --dry-run → golden probe sequence, exactly",
          _norm(t.journal) == _norm(GOLDEN_S2))
    check("…plan shows the literal command, then stops",
          not res and CMD_S2 in out and "(dry-run)" in out)

    # the real S2 motion end-to-end (mock mutation + fresh-tile verify)
    fx = dict(SM_OFF)
    fx[CMD_S2] = (0, "")
    t = mock(fx, allow=(CMD_S2,))
    session = {"touched": []}
    with patched_status(ST_LIVE):
        res, out = run_quiet(cc.verb_smib_up, t, SMIB, session,
                             assume_yes=True)
    check("S2 full run → verified-up True, one ✅ outcome line",
          res is True and "SAS leg is back on the floor" in out)
    check("…exactly one mutation, last in the journal",
          t.journal.count(CMD_S2) == 1 and t.journal[-1] == CMD_S2)
    check("…touched records the leg",
          session["touched"] == [("smib", SMIB, "127.0.0.1")])

    # C2 end-to-end (restart + fresh readerOk verify, can-lie wording)
    fx = dict(CO_BROKEN)
    fx[CMD_C2] = (0, "")
    t = mock(fx, allow=(CMD_C2,))
    with patched_status(status_fix(companions={COMP: comp_row()})):
        res, out = run_quiet(cc.verb_companion_restart, t, COMP, session,
                             assume_yes=True)
    check("C2 full run → verified True, readerOk-can-lie caveat",
          res is True and "readerOk can lie" in out)
    check("…exactly one mutation in the journal",
          t.journal.count(CMD_C2) == 1)

    # finish pass over what this session touched — never ends ambiguous
    with patched_status(ST_LIVE):
        _res, out = run_quiet(cc.finish_pass, session)
    check("finish pass → ✅ line per touched device + leave-state law",
          "✅ smib %s" % SMIB in out and "0x1A" in out)


# ---------------------------------------------------------------------------
# 4. CLI surface — menu pipe, pick prompt, dispatch rc, bare-checkout import
# ---------------------------------------------------------------------------

def t_pick():
    """The prompt is a prompt, not a dialer: q quits, a stray number or
    single keystroke gets the correction line — none of them become an ssh
    peer. _read_choice IS the whole vocabulary now (numbers pick, b q r ? are
    the only letters), so these three guards are asserted at the source
    instead of through a screen that has to build a fleet first."""
    import builtins
    old_in = builtins.input
    try:
        builtins.input = lambda prompt="": "q"
        res, _out = run_quiet(cc._read_choice, 3, 0)
        check("pick> 'q' quits — never dialed as a host", res == "quit")
        builtins.input = lambda prompt="": "9"
        res, out = run_quiet(cc._read_choice, 3, 0, True)
        check("pick> out-of-range number → corrected, not dialed",
              res is None and "pick a number" in out)
        builtins.input = lambda prompt="": "x"
        res, out = run_quiet(cc._read_choice, 3, 1, True)
        check("pick> single keystroke → corrected, not dialed",
              res is None and "pick a number" in out)
        # b is the only back key, it is NEVER rendered at the root, and it
        # never exits — the old Enter/0 could dump you past the finish pass.
        builtins.input = lambda prompt="": "b"
        res, out = run_quiet(cc._read_choice, 3, 0)
        check("b at the root corrects instead of exiting",
              res is None and "You are at the top" in out)
        res, _out = run_quiet(cc._read_choice, 3, 1)
        check("b one screen in goes back", res == "back")
        builtins.input = lambda prompt="": ""
        res, _out = run_quiet(cc._read_choice, 3, 1)
        check("Enter redraws — never navigation", res == "redraw")
        builtins.input = lambda prompt="": "192.168.50.66"
        res, _out = run_quiet(cc._read_choice, 3, 0, True)
        check("a typed address is taken only where the footer offers it",
              res == ("host", "192.168.50.66"))
        res, out = run_quiet(cc._read_choice, 3, 1, False)
        check("…and corrected where it is not", res is None
              and "pick a number" in out)
        builtins.input = lambda prompt="": "2"
        res, _out = run_quiet(cc._read_choice, 3, 0)
        check("an in-range number picks the n-th thing on THIS screen",
              res == ("pick", 2))
    finally:
        builtins.input = old_in


def t_dispatch():
    """The one-liner contract: a mutating verb that did not verify-up exits
    nonzero; a target on a target-less verb is refused, never ignored."""
    import argparse as _ap
    args = _ap.Namespace(yes=True, dry_run=False, json=False)
    ctx = {"root": "/nowhere/fixture", "mode": "read-only", "args": args,
           "status": {"__error__": "down"}, "session": {"touched": []},
           "identity": {"verdict": "not-hub", "wd": ""}}
    rc, out = run_quiet(cc.dispatch, ctx, "smib", [SPEER, "up"])
    check("read-only mutating one-liner → refused AND rc nonzero",
          rc == 1 and "read-only" in out)
    rc, out = run_quiet(cc.dispatch, ctx, "hub", [SPEER, "doctor"])
    check("target on a target-less verb → rc 2, never silently ignored",
          rc == 2 and "takes no target" in out)

def t_cli(tmp):
    """Every menu walk runs with `--hub none` so the PLACE is deterministic:
    without it a gate run on a box that can route to a real floor would find
    it, hit the network, and render a different screen."""
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    # CABINET_PLACE pins the PLACE as well as the hub: --hub none stops the
    # network probe, but hub_identity reads the local box, and update.py gates
    # in a git worktree ON THE HUB — units present, WorkingDirectory pointing
    # elsewhere — which renders the wrong-clone screen, not the front door.
    # Without this the front-door checks pass here and fail there.
    env = dict(os.environ, HOME=home, CABINET_PLACE="not-hub")
    script = os.path.join(DEPLOY, "cabinetconfig.py")
    argv = [sys.executable, script, "--hub", "none"]

    p = subprocess.run(argv, input="q\n", text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: piped 'q' exits rc 0", p.returncode == 0)
    check("…finish pass closes the session",
          "nothing was touched" in p.stdout)

    # THE FRONT DOOR — the most-seen screen in a public repo. It teaches,
    # names where the tool runs, and never leads with what it failed to find.
    front = p.stdout
    check("front door: says what the tool is and that it runs ON THE HUB",
          "fleet repair" in front and "It runs ON THE HUB" in front)
    check("…and shows exactly how to get there",
          "ssh <you>@<your-hub>" in front and "./cabinetconfig" in front)
    check("…no scolding banner, no read-only tag, no dead entries",
          not any(bad in front for bad in
                  ("READ-ONLY", "no hub evidence", "planned", "[ro]",
                   "[signpost]", "0) back")))
    # Split defensively: if the footer's lead ever moves, this must FAIL the
    # check, never raise IndexError out of the gate.
    foot = ([l for l in front.splitlines() if l.startswith(" Pick a number.")]
            or [""])[0]
    check("…and no back affordance at the root",
          bool(foot) and ") back" not in foot)
    check("…and no dead 'look for a hub' entry when discovery is switched "
          "off", "Look for a hub" not in front and "answered at ." not in front
          and "r) check again" not in foot)

    walk = "1\nb\nq\n"       # scope page → back → quit
    p = subprocess.run(argv, input=walk, text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: 2-level walk exits rc 0", p.returncode == 0)
    check("…the scope page names the frozen two-repair surface",
          "It changes exactly two things" in p.stdout)

    # b at the root is a correction, never an exit past the finish pass
    p = subprocess.run(argv, input="b\nq\n", text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: 'b' at the root does not exit",
          p.returncode == 0 and "You are at the top" in p.stdout)

    p = subprocess.run(argv, input="", text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: EOF (empty stdin) exits rc 0", p.returncode == 0)

    # bare-checkout import: no install, cwd elsewhere, deploy/ on sys.path;
    # the tee patch (update speaks through our say) must survive import
    code = ("import sys; sys.path.insert(0, %r); "
            "import cabinetconfig as c; import update as u; "
            "assert u.say is c.say, 'tee patch lost'; print('import-ok')"
            % DEPLOY)
    p = subprocess.run([sys.executable, "-c", code], text=True,
                       capture_output=True, env=env, cwd=tmp, timeout=60)
    check("import-from-bare-checkout + say-tee patch",
          p.returncode == 0 and "import-ok" in p.stdout)


# ---------------------------------------------------------------------------
# 4b. the screens — renderers return line lists, so every screen can be
#     asserted with no subprocess and no fake stdin
# ---------------------------------------------------------------------------

EGM = "IGT_00AA00BB00CC"
# a plan code (S1.4, C2, H14) in anything a person is asked to CHOOSE is the
# leak the rework exists to close; design-doc tags likewise
PLAN_CODE = re.compile(r"\b[SCH]\d{1,2}(\.\d+)?\b")
DOC_TAGS = ("[ro]", "[signpost]")


def screen_ctx(place, status, mode="full", local=None, verdict="hub",
               wd="", root="/nowhere/fixture"):
    import argparse as _ap
    ctx = {"root": root, "mode": mode, "place": place, "status": status,
           "session": {"touched": []}, "local": local, "tried": [],
           "identity": {"verdict": verdict, "wd": wd, "units": True},
           "args": _ap.Namespace(yes=False, dry_run=False, json=False,
                                 hub="none")}
    ctx["ui"] = cc._fresh_ui(ctx)
    return ctx


def floor_status(sas_over=None, comp_over=None, phase="idle"):
    st = status_fix(sas={SMIB + "/1": tile(**(sas_over or {}))},
                    companions={COMP: comp_row(**(comp_over or {}))},
                    phase=phase)
    st["companions"][COMP]["bindings"] = {"g2sEgmId": EGM, "sasSmib": SMIB,
                                          "sasAddress": 1}
    st["names"] = {EGM: "Bluebird 2"}
    st["hostOptions"] = {"gameroomName": "THE GAME ROOM",
                         "updates": {"current": "abc1234 a commit"}}
    return st


def fleet_of(st):
    return cc.discover_fleet(st, "/nowhere/fixture", local=mock(
        {"ip -4 -o addr 2>/dev/null || true": (0, ""),
         "ip neigh 2>/dev/null || true": (0, "")}))


HUB_LOCAL = dict(HUB_OK)
HUB_LOCAL['for u in cabinet-g2s cabinet-dhcp cabinet-dns cabinet-ntp '
          'cabinet-tftp; do echo "$u $(systemctl show -p '
          'ExecMainStartTimestamp --value $u 2>/dev/null)"; done'] = (0, "")


def t_screens():
    screens = []                       # (label, lines, action-labels)

    def render(label, lines, acts=()):
        screens.append((label, list(lines), [a["label"] for a in acts]))
        return lines, acts

    st = floor_status()
    ctx = screen_ctx("hub", st, local=mock(HUB_LOCAL))
    lines, acts = render("root healthy",
                         *cc.render_root(ctx, {"status": st,
                                               "fleet": fleet_of(st)}))
    body = "\n".join(lines)
    check("root: leads with the ANSWER, not a menu",
          body.index("Nothing needs you") < body.index("Floor tools"))
    check("root: the top list IS the fleet, device-first",
          [a["label"] for a in acts][:4]
          == ["Bluebird 2", SMIB, COMP, "hub"])

    # the same floor on fire must NOT render like the healthy one
    st_bad = floor_status(
        sas_over={"online": False, "stale": True, "reportAgeSec": 740},
        comp_over={"readerOk": False, "lastError": "PN532 wedged"})
    ctx_bad = screen_ctx("hub", st_bad, local=mock(HUB_LOCAL))
    lines, acts = render("root broken",
                         *cc.render_root(ctx_bad, {"status": st_bad,
                                                   "fleet": fleet_of(st_bad)}))
    body_bad = "\n".join(lines)
    check("root: a burning floor cannot render like a healthy one",
          body_bad != body and "need you, worst first" in body_bad)
    check("…and the machine's row says MONEY, not 'offline' (dual-stack law)",
          "credits and meters" in body_bad)
    check("…worst first: the ❌ rows come before 'The rest are fine'",
          body_bad.index("credits and meters")
          < body_bad.index("The rest are fine"))

    # device screen: the fix is a CHILD of the finding, in plain language
    doc, _o = run_quiet(cc.doctor_smib, mock(SM_OFF), SMIB, ST_EMPTY)
    dev = next(e for e in ctx_bad["ui"]["ents"] if e["kind"] == "smib")
    lines, acts = render("device UNIT-OFF",
                         *cc.render_device(ctx_bad, dev, doc))
    body = "\n".join(lines)
    check("device: UNIT-OFF renders as a sentence with the fix under it",
          "The SAS service on this Pi is switched off" in body
          and acts[0]["label"] == "Turn the SAS leg back on")
    check("…the classification code still prints, next to its evidence",
          "UNIT-OFF" in body)
    check("…and the probe table collapses to a count",
          "Looked at" in body and "config.txt" not in body)
    render("checks", cc.render_checks(dev, doc))
    render("one-liners", cc.render_oneliners(ctx_bad, dev))

    # HUB-PARKED: the switch, never the verb
    pdoc, _o = run_quiet(cc.doctor_smib, mock(SM_UP), SMIB, ST_PARKED)
    lines, acts = render("device HUB-PARKED",
                         *cc.render_device(ctx_bad, dev, pdoc))
    check("HUB-PARKED offers the signpost and NEVER 'turn the leg on'",
          acts[0]["label"].startswith("Show me where")
          and not any("Turn the SAS leg" in a["label"] for a in acts))

    # UNREACHABLE: zero actions that could not run
    udoc, _o = run_quiet(cc.doctor_smib, mock({}, reachable=False), SMIB,
                         ST_EMPTY)
    lines, acts = render("device UNREACHABLE",
                         *cc.render_device(ctx_bad, dev, udoc))
    check("UNREACHABLE offers no repair and no page that cannot answer",
          [a["label"] for a in acts] == ["The one-line commands for this "
                                         "screen"])

    # a rail replaces the action with ONE sentence — never a dead item
    ctx_t = screen_ctx("hub", floor_status(phase="running"),
                       local=mock(HUB_LOCAL))
    cc.render_root(ctx_t, {"status": ctx_t["status"],
                           "fleet": fleet_of(ctx_t["status"])})
    lines, acts = render("device UNIT-OFF, tournament up",
                         *cc.render_device(ctx_t, dev, doc))
    check("a tournament removes the repair ITEM and says why",
          "tournament is running" in "\n".join(lines)
          and not any("Turn the SAS leg" in a["label"] for a in acts))

    # a card reader is never told its SAS service is off
    cdoc, _o = run_quiet(cc.doctor_companion,
                         mock(dict(CO_BROKEN, **{NRES: (0, "0"),
                                                 EN_COMP: (0, "disabled"),
                                                 AC_COMP: (0, "inactive")})),
                         COMP, ST_EMPTY)
    cdev = next(e for e in ctx_bad["ui"]["ents"] if e["kind"] == "companion")
    lines, acts = render("device companion UNIT-OFF",
                         *cc.render_device(ctx_bad, cdev, cdoc))
    check("UNIT-OFF on a READER never says 'SAS service' or offers smib up",
          "card reader's service is switched off" in "\n".join(lines)
          and not any("SAS leg" in a["label"] for a in acts))

    # THE HUB'S OWN device screen — it runs cabinet-g2s, it is its own noun,
    # and it takes no target. It used to be offered a cabinet-sas journal and
    # a copy-pasteable `smib hub up --yes`.
    hdoc, _o = run_quiet(cc.doctor_hub, FIXROOT, mock(HUB_OK), floor_status())
    hdev = cc._dev("hub", "hub", "192.168.50.2", role="the floor host",
                   key="hub")
    lines, acts = render("device hub", *cc.render_device(ctx_bad, hdev, hdoc))
    hlabels = [a["label"] for a in acts]
    check("the hub's screen offers its OWN unit's journal, not a satellite's",
          any("cabinet-g2s" in l for l in hlabels)
          and not any("cabinet-sas" in l for l in hlabels))
    ol = "\n".join(render("one-liners hub",
                          cc.render_oneliners(ctx_bad, hdev))[0])
    check("…and its one-liners are hub verbs with no target, never `smib "
          "hub up`", "./cabinetconfig hub doctor" in ol
          and "smib hub" not in ol and "hub up" not in ol)

    # the plan-code stripper the sweep now runs its doctors through
    _d, dout = run_quiet(cc.doctor_smib, mock(SM_OFF), SMIB, ST_EMPTY)
    stripped = cc._plain_report(dout.rstrip("\n").split("\n"))
    check("sweep: a doctor's printed table loses its plan codes and keeps "
          "its columns",
          not any(PLAN_CODE.search(l) for l in stripped)
          and any(l.startswith("   ✅ ") and "  " in l[6:] for l in stripped)
          and any("unit census" in l for l in stripped))

    # the machine screen — "what is wrong with my BB2"
    ment = next(e for e in ctx_bad["ui"]["ents"] if e["kind"] == "machine")
    lines, acts = render("machine", *cc.render_machine(ctx_bad, ment))
    check("machine screen routes to what serves it, and owns no settings",
          [a["label"] for a in acts] == [SMIB, COMP]
          and "never changed from here" in "\n".join(lines))

    # off the hub, hub reachable: full visibility, mutation RELOCATED
    ctx_r = screen_ctx("remote", st_bad, mode="read-only", verdict="not-hub")
    lines, acts = render("root remote",
                         *cc.render_root(ctx_r, {"status": st_bad,
                                                 "fleet": fleet_of(st_bad)}))
    body_r = "\n".join(lines)
    check("state 2: the header names the hub and how it was reached",
          "reachable from here" in body_r)
    check("…the read-only reason is the lock and the key, not 'permission'",
          "update lock" in body_r and "key that reaches the Pis" in body_r)
    check("…lease/ARP is hidden off the hub (it would be fiction)",
          not any("who is on the wire" in a["label"] for a in acts))
    rdev = next(e for e in ctx_r["ui"]["ents"] if e["kind"] == "smib")
    lines, acts = render("device remote", *cc.render_device(ctx_r, rdev, doc))
    check("state 2: the repair is relocated, not refused",
          acts[0]["label"].endswith("runs on the hub"))
    render("relay", cc.render_relay(ctx_r, rdev, "verb-smib-up"))
    render("hub over the API", *cc.render_hub_api_view(ctx_r))

    # THE HUB'S VIEW of a Pi this computer cannot ssh to — the screen that
    # used to print `tile smib-x/1 — online=False stale=True` at a person.
    rcomp = next(e for e in ctx_r["ui"]["ents"] if e["kind"] == "companion")
    hv_s = render("hub view of a SAS leg", *cc.render_hub_view(ctx_r, rdev))
    hv_c = render("hub view of a reader", *cc.render_hub_view(ctx_r, rcomp))
    hv = "\n".join(list(hv_s[0]) + list(hv_c[0]))
    check("the hub's view speaks sentences, never dict fields",
          not any(t in hv for t in ("online=", "stale=", "readerOk=",
                                    "True", "False", " tile "))
          and "the hub has not heard from this leg" in hv)
    check("…and it never guesses the hub login for a copy-paste ssh line",
          "<you>@" in hv and ("ssh %s@" % cc._u.SAT_USER) not in hv)

    # EVERY 'show how to fix it' page — the leaf screens of the whole flow,
    # and the ones the 80-column law never used to see.
    wire_row = {"kind": "wire", "name": "192.168.50.104",
                "peer": "192.168.50.104", "role": "a box on the wire"}
    for _tbl_name, tbl in (("code", cc.CODE_UI),
                           ("companion", cc.CODE_UI_BY_KIND["companion"])):
        for code, ui in sorted(tbl.items()):
            d = dict(rdev, kind=("companion" if _tbl_name == "companion"
                                 else "smib"))
            act = ui.get("act") or {}
            if act.get("kind") == "block":
                render("fix page %s" % code,
                       ["", cc._b("   " + act["label"])]
                       + cc._pad(act["build"](d)))
            if ui.get("after"):
                render("fix page %s" % code, ui["after"](d))
                # a lease row carries no reported login: this used to be a
                # KeyError, i.e. the menu dying on the normal case
                render("fix page %s (lease row)" % code,
                       ui["after"](wire_row))

    # the other places
    ctx_d = screen_ctx("degraded", {"__error__": "down"}, mode="degraded",
                       local=mock(HUB_LOCAL))
    render("root degraded",
           *cc.render_root(ctx_d, {"status": {"__error__": "down"},
                                   "fleet": cc.discover_fleet(
                                       {"__error__": "down"},
                                       "/nowhere/fixture",
                                       local=mock({}))}))
    ctx_w = screen_ctx("wrong-clone", st, mode="read-only",
                       verdict="wrong-clone", wd="/other/tree/G2S",
                       local=mock(HUB_LOCAL))
    lines, acts = render("root wrong clone",
                         *cc.render_root(ctx_w, {"status": st,
                                                 "fleet": fleet_of(st)}))
    check("wrong clone: the menu SHRINKS to what still works, fix printed",
          "cd /other/tree" in "\n".join(lines)
          and not any(a["label"] == SMIB for a in acts))
    ctx_f = screen_ctx("away", {"__error__": "no hub"}, mode="read-only",
                       verdict="not-hub")
    render("front door (--hub none: no look-again item)",
           *cc.render_front_door(ctx_f))
    # …and the shape where discovery DID run and found nothing: item 4 is
    # live, and the note that explains why that is normal is at the bottom.
    ctx_f2 = screen_ctx("away", {"__error__": "no hub"}, mode="read-only",
                        verdict="not-hub")
    ctx_f2["args"].hub = None
    ctx_f2["ui"]["tried"] = [("http://192.168.50.2:8081", "the documented "
                              "default"), ("http://192.168.4.1:8081",
                                           "this computer's gateway")]
    fd2, fd2a = render("front door (looked, found none)",
                       *cc.render_front_door(ctx_f2))
    check("front door: the look-again item exists only when looking can work",
          any(a["label"].startswith("Look for a hub") for a in fd2a)
          and not any(a["label"].startswith("Look for a hub")
                      for a in cc.render_front_door(ctx_f)[1]))
    render("scope", cc.render_scope())
    render("cli map", cc.render_cli_map())

    # THE 14-MACHINE TESTER FLOOR — the fold the code cites as its reason to
    # exist, with collector-given names, which is exactly where it broke 80.
    big = []
    names = ["Bally Blazing 7s Upright", "IGT Game King Corner Slot",
             "Konami Podium Center Row", "Wheel of Fortune 25c",
             "Red White and Blue", "Triple Cash Wheel", "Jackpot Party",
             "Money Storm", "Piggy Bankin", "Reel Em In", "Cash Express",
             "Top Dollar", "Double Diamond", "Blazing Sevens"]
    for i, nm in enumerate(names):
        big.append({"kind": "machine", "name": nm,
                    "peer": "192.168.50.%d" % (30 + i), "flag": "✅",
                    "detail": "online · G2S", "sub": [],
                    "key": "machine:%d" % i})
    big.append({"kind": "hub", "name": "hub", "peer": "192.168.50.2",
                "flag": "✅", "detail": "5 services up, API answering",
                "sub": [], "key": "hub"})
    fold_acts = []
    fold = cc._board_lines(screen_ctx("hub", st), big, fold_acts)
    render("root folded 14-machine floor", fold,
           [{"label": a} for a in [x["label"] for x in fold_acts]])
    check("the folded tail stays pickable — one number per device",
          len(fold_acts) == len(big))
    check("…and the hub keeps its full row instead of folding to a bare name",
          any("✅ hub" in l and "192.168.50.2" in l for l in fold))

    # --- the laws, over every screen rendered above --------------------
    leaks, tags, dead = [], [], []
    for label, lines, act_labels in screens:
        for a in act_labels:
            if PLAN_CODE.search(a):
                leaks.append("%s: %r" % (label, a))
            if any(t in a for t in DOC_TAGS) or "[y/N]" in a:
                tags.append("%s: %r" % (label, a))
            if not a.strip():
                dead.append(label)
        for l in lines:
            if PLAN_CODE.search(cc._ANSI.sub("", l)):
                leaks.append("%s: %r" % (label, l))
            if any(t in l for t in DOC_TAGS):
                tags.append("%s: %r" % (label, l))
            if "planned" in l or "0) back" in l:
                dead.append("%s: %r" % (label, l))
    for b in (leaks + tags + dead)[:6]:
        print("     ↳ %s" % b)
    check("LAW: no plan code (S1.4/C2/H14) in ANY rendered screen or label, "
          "across %d screens" % len(screens), not leaks)
    check("LAW: no design-doc tag ([ro]/[signpost]/[y/N]) in any label",
          not tags)
    check("LAW: no dead entry — nothing rendered that does nothing",
          not dead)
    # DISPLAY cells, not code points: ✅/❌ are one character and two cells,
    # so len() under-measures every flagged row by one.
    wide = ["%s: %d cols: %r" % (lb, cc._dispw(cc._ANSI.sub("", l)), l)
            for lb, ls, _a in screens for l in ls
            if cc._dispw(cc._ANSI.sub("", l)) > 80]
    for w in wide[:6]:
        print("     ↳ %s" % w)
    check("LAW: every rendered screen fits 80 columns, measured in display "
          "cells, across %d screens" % len(screens), not wide)


def t_menu_safety(tmp):
    """THE SAFETY LAW: a number never changes anything. A number opens a
    screen or shows a plan; only an explicit y at a confirm() prompt mutates.
    That is what makes the worst-first renumbering safe — the board can
    reorder between two glances and a stale keystroke still cannot fire a
    repair."""
    import builtins
    root = os.path.join(tmp, "safety-root")
    os.makedirs(root)
    st = floor_status()
    ctx = screen_ctx("hub", st, local=mock(HUB_LOCAL), root=root)
    cc.render_root(ctx, {"status": st, "fleet": fleet_of(st)})
    dev = next(e for e in ctx["ui"]["ents"] if e["kind"] == "smib")
    doc, _o = run_quiet(cc.doctor_smib, mock(SM_OFF), SMIB, ST_EMPTY)
    ctx["ui"]["docs"][dev["key"]] = doc
    t = mock(SM_OFF)                       # the box every action would touch
    old_tp, old_in = cc._transport_for, builtins.input
    # press every number on the screen, in order, backing out of each
    keys = iter(list("1234") + ["b"] * 6 + ["q"])
    cc._transport_for = lambda c, d: t
    # echo the prompt the way a terminal does, so confirm()'s own [y/N] is
    # part of the captured session and not swallowed by the patch
    builtins.input = lambda prompt="": (sys.stdout.write(prompt),
                                        next(keys))[1]
    try:
        with patched_status(st):
            _res, out = run_quiet(cc.device_screen, ctx, dev)
    finally:
        cc._transport_for, builtins.input = old_tp, old_in
    fired = [c for c in t.journal
             if any(tok in c for tok in MUT_TOKENS)]
    check("LAW: pressing only NUMBERS issues zero mutating commands",
          not fired)
    check("…the repair still showed its literal plan and asked",
          CMD_S2 in out and "[y/N]" in out)


# ---------------------------------------------------------------------------
# 5. the laws — hard-fail forever
# ---------------------------------------------------------------------------

# "sudo " is the strongest tripwire: every mutation this tool can issue is
# sudo-prefixed by construction, so ANY un-allowed sudo in a journal fails.
MUT_TOKENS = ("sudo ", "systemctl enable", "systemctl disable",
              "systemctl restart", "systemctl start ", "systemctl stop ",
              "systemctl mask", "usermod", "sed -i", " tee ", "reboot",
              "shutdown", "truncate", "mkfs", "dd if=", "rm -", ">>")


def t_laws():
    check("mutation surface frozen at exactly two verbs",
          cc.MUTATING == {("companion", "restart"), ("smib", "up")})
    # Discovery's probe runs ON THE HUB through a shell — a satellite's
    # self-reported sshUser must ride as data, never as shell
    import shlex as _sh
    evil = cc._probe_cmd("192.168.50.66", "pi; touch /tmp/pwned #")
    check("LAW: hostile self-reported identity stays ONE ssh token",
          "pi; touch /tmp/pwned #@192.168.50.66" in _sh.split(evil))
    check("…and a clean identity is byte-identical to the plain form",
          cc._probe_cmd(SPEER, "pi").endswith(" pi@%s true" % SPEER))
    bad = []
    for journal, allow in _JOURNALS:
        for cmd in journal:
            if any(tok in cmd for tok in MUT_TOKENS) and cmd not in allow:
                bad.append("mutation: %s" % cmd)
            if ("/data/" in cmd or "G2S/data" in cmd) and not (
                    cmd.startswith('python3 -c "import sqlite3')
                    and "mode=ro" in cmd):
                bad.append("*/data touched: %s" % cmd)
            if ("/api/" in cmd or "curl" in cmd) and cmd != HUBKEY_GET:
                bad.append("hub-API beyond the hubkey GET: %s" % cmd)
    for b in bad[:6]:
        print("     ↳ %s" % b)
    check("LAW: zero */data writes, EMPTY API whitelist, no un-allowed "
          "mutation across all %d journals" % len(_JOURNALS), not bad)


def main():
    tmp = tempfile.mkdtemp(prefix="cabinetconfig-gate-")
    try:
        print("== doctor classification")
        t_doctors()
        print("== degraded mode")
        t_degraded(tmp)
        print("== verbs")
        t_verbs()
        print("== CLI (menu pipe + bare-checkout import)")
        t_cli(tmp)
        print("== screens")
        t_screens()
        print("== menu safety law")
        t_menu_safety(tmp)
        print("== the prompt")
        t_pick()
        print("== dispatch exit codes")
        t_dispatch()
        print("== laws")
        t_laws()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 50)
    print("RESULT: %d passed, %d failed" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
