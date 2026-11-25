"""hub_wifi.py — put the hub on the house Wi-Fi from its own Settings page.

The slot network is wired and the hub runs it; Wi-Fi is only how a phone on
the couch reaches these pages. A freshly flashed hub has no Wi-Fi set, and the
person setting it up should not need a keyboard, a terminal or the flashing
tool's settings to give it one. This module is the small piece behind
Settings ▸ Wi-Fi: what the hub can hear, what it is joined to, join, forget.

It drives NetworkManager through nmcli and nothing else. A hub without nmcli
or without a Wi-Fi radio (a Windows hub, a mini PC on a cable) answers
available=False and the page hides the card. Stdlib only.

Changing the network needs more rights than the hub's account always has, so
a change is tried plainly first and, when NetworkManager says no, again
through the narrow `sudo -n` rules deploy/hub_setup.sh installs.
"""
import os
import re
import shutil
import subprocess

IS_WINDOWS = os.name == "nt"
_TIMEOUT_READ = 12
_TIMEOUT_JOIN = 50

# A Raspberry Pi keeps its radio switched off until it knows which country's
# channel rules to follow. The page offers these when the radio is off.
COUNTRIES = [
    ("US", "United States"), ("CA", "Canada"), ("GB", "United Kingdom"),
    ("AU", "Australia"), ("NZ", "New Zealand"), ("IE", "Ireland"),
    ("DE", "Germany"), ("FR", "France"), ("NL", "Netherlands"),
    ("BE", "Belgium"), ("ES", "Spain"), ("PT", "Portugal"), ("IT", "Italy"),
    ("AT", "Austria"), ("CH", "Switzerland"), ("SE", "Sweden"),
    ("NO", "Norway"), ("DK", "Denmark"), ("FI", "Finland"), ("PL", "Poland"),
    ("CZ", "Czechia"), ("UA", "Ukraine"), ("JP", "Japan"), ("MX", "Mexico"),
    ("BR", "Brazil"), ("AR", "Argentina"), ("ZA", "South Africa"),
]
_COUNTRY_CODES = {c for c, _ in COUNTRIES}

_DENIED = ("not authorized", "insufficient privileges", "permission denied",
           "org.freedesktop.networkmanager")


def _nmcli():
    return None if IS_WINDOWS else shutil.which("nmcli")


def _run(argv, timeout, root=False):
    """-> (returncode, stdout, stderr). Never raises."""
    if root:
        argv = ["sudo", "-n"] + argv
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as e:
        return 127, "", str(e)


def _change(argv, timeout):
    """Run a command that changes the network: plainly, then through sudo when
    the refusal was about rights."""
    rc, out, err = _run(argv, timeout)
    if rc != 0 and any(d in (err + out).lower() for d in _DENIED):
        rc2, out2, err2 = _run(argv, timeout, root=True)
        # sudo itself refusing (no rule installed) keeps the first answer
        if not (rc2 != 0 and "sudo:" in err2):
            return rc2, out2, err2
    return rc, out, err


def split_terse(line):
    """One line of `nmcli -t` output -> fields. A colon inside a value arrives
    as `\\:` and a backslash as `\\\\`."""
    out, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1]); i += 2; continue
        if ch == ":":
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _wifi_device(nm):
    """-> (device, state, connection) of the first Wi-Fi radio, or None."""
    rc, out, _ = _run([nm, "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                       "device", "status"], _TIMEOUT_READ)
    if rc != 0:
        return None
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 4 and f[1] == "wifi":
            return f[0], f[2], f[3]
    return None


def _radio_off(nm, state):
    if state == "unavailable":
        return True
    rc, out, _ = _run([nm, "-t", "radio", "wifi"], _TIMEOUT_READ)
    return rc == 0 and out.strip() == "disabled"


def parse_networks(text):
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list` -> a list of
    {ssid, signal, secure, inUse}, one row per name (the strongest), joined
    one first then strongest first. Hidden (nameless) networks are left out."""
    best = {}
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 4 or not f[1]:
            continue
        try:
            sig = int(f[2])
        except ValueError:
            sig = 0
        row = {"ssid": f[1], "signal": sig,
               "secure": bool(f[3].strip()) and f[3].strip() != "--",
               "inUse": f[0].strip() == "*"}
        cur = best.get(f[1])
        if cur is None or row["inUse"] or (not cur["inUse"]
                                           and sig > cur["signal"]):
            best[f[1]] = row
    return sorted(best.values(),
                  key=lambda r: (not r["inUse"], -r["signal"], r["ssid"]))


def status(scan=True):
    """What Settings ▸ Wi-Fi shows. Always answers; never raises."""
    nm = _nmcli()
    if not nm:
        return {"available": False}
    dev = _wifi_device(nm)
    if not dev:
        return {"available": False}
    device, state, conn = dev
    out = {"available": True, "device": device, "connected": False,
           "ssid": "", "ip": "", "radioOff": _radio_off(nm, state),
           "networks": [],
           "countries": [{"code": c, "name": n} for c, n in COUNTRIES]}
    if state == "connected" and conn:
        out["connected"] = True
        out["ssid"] = conn
        rc, o, _ = _run([nm, "-t", "-g", "IP4.ADDRESS", "device", "show",
                         device], _TIMEOUT_READ)
        if rc == 0 and o.strip():
            out["ip"] = o.strip().splitlines()[0].split("|")[0] \
                .strip().split("/")[0]
    if scan and not out["radioOff"]:
        listing = [nm, "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                   "device", "wifi", "list", "ifname", device, "--rescan"]
        rc, o, _ = _run(listing + ["auto"], _TIMEOUT_READ)
        if rc == 0:
            out["networks"] = parse_networks(o)
        if not out["networks"]:
            # Nothing on record (a radio that has only just come on has not
            # listened yet): make it listen now and wait for the answer.
            rc, o, _ = _run(listing + ["yes"], _TIMEOUT_READ)
            if rc == 0:
                out["networks"] = parse_networks(o)
        if rc == 0:
            for n in out["networks"]:
                if n["inUse"]:
                    out["ssid"] = n["ssid"]
    return out


def check_join(ssid, password):
    """-> an error sentence, or None when the request is well formed."""
    if not isinstance(ssid, str) or not ssid:
        return "Pick a network"
    if len(ssid.encode("utf-8")) > 32 or ssid.startswith("-") \
            or re.search(r"[\x00-\x1f\x7f]", ssid):
        return "That network name cannot be used"
    if not isinstance(password, str):
        return "The password must be text"
    if password and not (8 <= len(password) <= 63
                         or re.fullmatch(r"[0-9A-Fa-f]{64}", password)):
        return "A Wi-Fi password is 8 to 63 characters"
    if re.search(r"[\x00-\x1f\x7f]", password):
        return "That password cannot be used"
    return None


def _radio_on(nm, country):
    """Switch the radio on, telling a Raspberry Pi its country first."""
    if country:
        rc_path = shutil.which("raspi-config")
        if rc_path:
            _run([rc_path, "nonint", "do_wifi_country", country],
                 _TIMEOUT_READ, root=True)
    rf = shutil.which("rfkill")
    if rf:
        _change([rf, "unblock", "wifi"], _TIMEOUT_READ)
    _change([nm, "radio", "wifi", "on"], _TIMEOUT_READ)


def _plain_error(rc, text):
    t = (text or "").strip().lower()
    if rc == 124:
        return "The network did not answer in time. Try again."
    if "secrets were required" in t or "802-11-wireless-security" in t \
            or "password" in t and "invalid" in t:
        return "That password was not accepted."
    if "no network with ssid" in t:
        return "The hub cannot hear that network any more. Move closer or try again."
    if any(d in t for d in _DENIED) or "sudo:" in t:
        return ("The hub is not allowed to change its Wi-Fi. "
                "Run the hub setup once more to fix that.")
    last = (text or "").strip().splitlines()[-1:] or ["no reason given"]
    return "Could not join: " + last[0][:160]


def join(ssid, password="", country=""):
    """Join a network. -> {ok, ...status} or {ok: False, error}."""
    nm = _nmcli()
    dev = _wifi_device(nm) if nm else None
    if not dev:
        return {"ok": False, "error": "This hub has no Wi-Fi."}
    bad = check_join(ssid, password)
    if bad:
        return {"ok": False, "error": bad}
    country = (country or "").strip().upper()
    if country and country not in _COUNTRY_CODES:
        return {"ok": False, "error": "Pick a country from the list"}
    device, state, _ = dev
    if _radio_off(nm, state):
        if not country and shutil.which("raspi-config"):
            return {"ok": False, "needCountry": True,
                    "error": "Pick your country first — the Wi-Fi radio "
                             "stays off until the hub knows it."}
        _radio_on(nm, country)
        _run([nm, "device", "wifi", "rescan", "ifname", device], _TIMEOUT_READ)
    argv = [nm, "--wait", "40", "device", "wifi", "connect", ssid]
    if password:
        argv += ["password", password]
    argv += ["ifname", device]
    rc, out, err = _change(argv, _TIMEOUT_JOIN)
    if rc != 0:
        # a failed join leaves a half-made saved network behind; drop it so
        # the next try starts clean and a wrong password is not remembered
        _change([nm, "connection", "delete", "id", ssid], _TIMEOUT_READ)
        return {"ok": False, "error": _plain_error(rc, err or out)}
    res = status(scan=False)
    res["ok"] = True
    return res


def radio_on(country=""):
    """Switch the Wi-Fi radio on (a Raspberry Pi needs its country first)."""
    nm = _nmcli()
    dev = _wifi_device(nm) if nm else None
    if not dev:
        return {"ok": False, "error": "This hub has no Wi-Fi."}
    country = (country or "").strip().upper()
    if country and country not in _COUNTRY_CODES:
        return {"ok": False, "error": "Pick a country from the list"}
    if not country and shutil.which("raspi-config"):
        return {"ok": False, "needCountry": True,
                "error": "Pick your country first."}
    _radio_on(nm, country)
    _run([nm, "device", "wifi", "rescan", "ifname", dev[0]], _TIMEOUT_READ)
    res = status()
    if res.get("radioOff"):
        return {"ok": False, "error": "The Wi-Fi radio did not come on. "
                "Run the hub setup once more, then try again."}
    res["ok"] = True
    return res


def forget(ssid):
    """Leave a network and stop rejoining it."""
    nm = _nmcli()
    if not nm or not _wifi_device(nm):
        return {"ok": False, "error": "This hub has no Wi-Fi."}
    if check_join(ssid, ""):
        return {"ok": False, "error": "That network name cannot be used"}
    rc, out, err = _change([nm, "connection", "delete", "id", ssid],
                           _TIMEOUT_READ)
    if rc != 0:
        return {"ok": False, "error": _plain_error(rc, err or out)}
    res = status(scan=False)
    res["ok"] = True
    return res
