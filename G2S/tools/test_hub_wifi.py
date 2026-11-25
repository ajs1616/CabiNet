#!/usr/bin/env python3
"""Gate for Settings ▸ Wi-Fi (G2S/hub_wifi.py).

Never touches this box's real network: a stand-in `nmcli` is put first on
PATH, answers from canned text and writes down every call it gets.

  1. the pure parts — nmcli's terse format, the network list, what may be
     asked for;
  2. status / join / forget / radio-on against the stand-in — a hub with no
     radio, a joined hub, a wrong password, a radio that is switched off;
  3. the login rules — reading is for a signed-in person, changing is admin.

Must end "RESULT: N passed, 0 failed".
"""

import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import hub_wifi as hw  # noqa: E402
import web_auth as wa  # noqa: E402

_p = _f = 0


def ok(cond, what):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print("FAIL:", what)


STUB = r'''#!__PY__
import json, os, sys
d = os.environ["WIFI_STUB_DIR"]
cfg = json.load(open(os.path.join(d, "cfg.json")))
a = sys.argv[1:]
open(os.path.join(d, "calls.log"), "a").write(json.dumps(a) + "\n")
j = " ".join(a)
if "device status" in j:
    sys.stdout.write(cfg.get("devices", "")); sys.exit(0)
if j.endswith("radio wifi"):
    print(cfg.get("radio", "enabled")); sys.exit(0)
if "device wifi list" in j:
    sys.stdout.write(cfg.get("list_yes" if a[-1] == "yes" else "list", "")); sys.exit(0)
if "device show" in j:
    print(cfg.get("ip", "")); sys.exit(0)
if "device wifi connect" in j:
    sys.stderr.write(cfg.get("connect_err", "")); sys.exit(cfg.get("connect_rc", 0))
if "connection delete" in j:
    sys.exit(cfg.get("delete_rc", 0))
sys.exit(0)
'''


def calls(d):
    p = os.path.join(d, "calls.log")
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p).read().splitlines()]


def setcfg(d, **cfg):
    json.dump(cfg, open(os.path.join(d, "cfg.json"), "w"))
    p = os.path.join(d, "calls.log")
    if os.path.exists(p):
        os.remove(p)


def pure():
    ok(hw.split_terse(r"*:My\:Net:70:WPA2") == ["*", "My:Net", "70", "WPA2"],
       "an escaped colon stays inside the name")
    ok(hw.split_terse(r" :a\\b:1:") == [" ", "a\\b", "1", ""],
       "an escaped backslash")
    nets = hw.parse_networks("*:Home:60:WPA2\n :Home:90:WPA2\n ::80:WPA2\n"
                             " :Open:40:\n :Loud:95:WPA2 WPA3\n :Dash:30:--\n")
    ok([n["ssid"] for n in nets] == ["Home", "Loud", "Open", "Dash"],
       "joined first, then strongest; nameless left out; one row per name")
    ok(nets[0]["inUse"] and nets[0]["signal"] == 60,
       "the joined row wins over a stronger twin")
    ok(nets[2]["secure"] is False and nets[3]["secure"] is False
       and nets[1]["secure"] is True, "open networks are told apart")
    ok(hw.check_join("Home", "password1") is None, "a good request")
    ok(hw.check_join("Home", "") is None, "an open network needs no password")
    ok(hw.check_join("Home", "short") is not None, "a short password")
    ok(hw.check_join("Home", "x" * 64) is not None, "64 non-hex characters")
    ok(hw.check_join("Home", "a" * 64) is None, "64 hex characters")
    ok(hw.check_join("-x", "password1") is not None, "a name that looks like an option")
    ok(hw.check_join("", "password1") is not None, "no name")
    ok(hw.check_join("a\nb", "password1") is not None, "a control character")
    ok(hw.check_join("n" * 33, "password1") is not None, "a name over 32 bytes")
    ok(hw.check_join("Home", 12345678) is not None, "a password that is not text")


def driven(d):
    # no radio at all
    setcfg(d, devices="eth0:ethernet:connected:cabinet-slot\n")
    ok(hw.status() == {"available": False}, "no radio -> not available")
    ok(hw.join("Home", "password1")["ok"] is False, "no radio -> join refused")

    # not joined, three networks heard
    dev = "eth0:ethernet:connected:cabinet-slot\nwlan0:wifi:disconnected:\n"
    setcfg(d, devices=dev, list=" :Home:80:WPA2\n :Cafe:40:\n")
    s = hw.status()
    ok(s["available"] and not s["connected"] and not s["radioOff"],
       "a radio, not joined")
    ok([n["ssid"] for n in s["networks"]] == ["Home", "Cafe"], "heard networks")
    ok(len(s["countries"]) > 10, "the country list rides along")

    # nothing on record (the radio has only just come on): made to listen
    setcfg(d, devices=dev, list="", list_yes=" :Home:70:WPA2\n")
    s = hw.status()
    ok([n["ssid"] for n in s["networks"]] == ["Home"]
       and any(x[-1] == "yes" for x in calls(d)),
       "an empty list makes the radio listen now")
    setcfg(d, devices=dev, list=" :Home:80:WPA2\n :Cafe:40:\n")
    hw.status()
    ok(not any(x[-1] == "yes" for x in calls(d)),
       "a list with networks on it is not asked for twice")

    # a refused request never reaches nmcli's connect
    r = hw.join("Home", "short")
    ok(r["ok"] is False and not any("connect" in c for c in calls(d)),
       "a bad request is refused before nmcli is asked")

    # a good join
    setcfg(d, devices=dev, list=" :Home:80:WPA2\n")
    r = hw.join("Home", "password1")
    c = [x for x in calls(d) if "connect" in x]
    ok(r["ok"] is True and len(c) == 1, "a join asks nmcli once")
    ok(c and c[0][-6:] == ["connect", "Home", "password", "password1",
                           "ifname", "wlan0"], "with the name, password and radio")

    # an open network sends no password
    setcfg(d, devices=dev)
    hw.join("Cafe", "")
    c = [x for x in calls(d) if "connect" in x]
    ok(c and "password" not in c[0], "an open network sends no password")

    # a wrong password: plain words back, and the half-made network dropped
    setcfg(d, devices=dev, connect_rc=4,
           connect_err="Error: Connection activation failed: Secrets were "
                       "required, but not provided.\n")
    r = hw.join("Home", "wrongpass1")
    ok(r["ok"] is False and "password" in r["error"].lower()
       and "Error:" not in r["error"], "a wrong password is said plainly")
    ok(any(x[-3:] == ["delete", "id", "Home"] for x in calls(d)),
       "a failed join does not leave a saved network behind")

    # joined
    setcfg(d, devices="wlan0:wifi:connected:Home\n", ip="192.168.4.50/24",
           list="*:Home:80:WPA2\n")
    s = hw.status()
    ok(s["connected"] and s["ssid"] == "Home" and s["ip"] == "192.168.4.50",
       "joined: name and address")

    # forget
    r = hw.forget("Home")
    ok(r["ok"] is True and any(x[-3:] == ["delete", "id", "Home"]
                               for x in calls(d)), "forget drops the network")
    ok(hw.forget("-x")["ok"] is False, "forget refuses an option-like name")

    # radio switched off: no listening, and an unknown country is refused
    setcfg(d, devices="wlan0:wifi:unavailable:\n", radio="disabled")
    s = hw.status()
    ok(s["radioOff"] is True and s["networks"] == []
       and not any("list" in x for x in calls(d)),
       "radio off: says so and does not listen")
    ok(hw.radio_on("ZZ")["ok"] is False, "an unknown country is refused")
    ok(hw.join("Home", "password1", "ZZ")["ok"] is False,
       "join with an unknown country is refused")


def rules():
    ok(wa.post_level("/api/wifi", b"{}") == wa.ADMIN, "changing Wi-Fi is admin")
    lvl, page = wa.get_level("/api/wifi")
    ok(lvl >= wa.NORMAL and page is False, "reading Wi-Fi needs a sign-in")


def main():
    pure()
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "nmcli")
        open(stub, "w").write(STUB.replace("__PY__", sys.executable))
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        os.environ["WIFI_STUB_DIR"] = d
        # ONLY the stand-in: no sudo, no rfkill, no raspi-config on this PATH
        os.environ["PATH"] = d
        if os.name == "nt":
            print("skipping the driven layer on Windows (no nmcli there)")
        else:
            driven(d)
    rules()
    print(f"\nRESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
