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

EMPTY_FLEET = {"degraded": True, "sats": [], "companions": [],
               "machines": [], "leases": [], "neigh": [], "probes": {}}


def t_pick():
    """pick> is a prompt, not a dialer: q quits, a stray number or single
    keystroke gets the correction line — none of them become an ssh peer."""
    import builtins
    ctx = {"root": "/nowhere/fixture", "local": None}
    old_df, old_in = cc.discover_fleet, builtins.input
    cc.discover_fleet = lambda *a, **k: dict(EMPTY_FLEET)
    try:
        with patched_status({"__error__": "down"}):
            builtins.input = lambda prompt="": "q"
            res, _out = run_quiet(cc._pick_device, ctx, "smib")
            check("pick> 'q' quits — never dialed as a host", res == "quit")
            builtins.input = lambda prompt="": "9"
            res, out = run_quiet(cc._pick_device, ctx, "smib")
            check("pick> out-of-range number → corrected, not dialed",
                  res is None and "pick a number" in out)
            builtins.input = lambda prompt="": "x"
            res, out = run_quiet(cc._pick_device, ctx, "companion")
            check("pick> single keystroke → corrected, not dialed",
                  res is None and "pick a number" in out)
    finally:
        cc.discover_fleet, builtins.input = old_df, old_in


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
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    env = dict(os.environ, HOME=home)
    script = os.path.join(DEPLOY, "cabinetconfig.py")

    p = subprocess.run([sys.executable, script], input="q\n", text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: piped 'q' exits rc 0", p.returncode == 0)
    check("…finish pass closes the session",
          "nothing was touched" in p.stdout)

    walk = "3\n3\n0\nq\n"    # SMIB menu → S3 signpost → back → quit
    p = subprocess.run([sys.executable, script], input=walk, text=True,
                       capture_output=True, env=env, cwd=REPO, timeout=120)
    check("menu: 2-level walk exits rc 0", p.returncode == 0)
    check("…S3 signpost rendered on the walk", "Switchboard" in p.stdout)

    p = subprocess.run([sys.executable, script], input="", text=True,
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
        print("== pick prompt")
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
