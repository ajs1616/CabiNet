#!/usr/bin/env python3
"""
Bench pre-flight — GO/NO-GO check for the AVP join session.

Run from G2S/ right before powering the machine:
    python3 tools/bench_preflight.py

Checks the environment hazards that have actually bitten this project:
wrong/missing slot-VLAN IP, port squatters, the webserver.py UDP-67 trap,
firewall state, and stale config values. Exits 0 on GO.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

SERVER_IP = "192.168.50.2"
NIC = "eno1"
AVP_IP = "192.168.50.100"
G2S_DIR = Path(__file__).resolve().parent.parent

GO = True
WARN = 0


def res(ok, label, detail="", fix="", warn=False):
    global GO, WARN
    if ok:
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    elif warn:
        WARN += 1
        print(f"  ⚠️  {label}" + (f" — {detail}" if detail else ""))
        if fix:
            print(f"      fix: {fix}")
    else:
        GO = False
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
        if fix:
            print(f"      fix: {fix}")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=10).stdout
    except Exception:
        return ""


print("CasinoNet bench pre-flight\n" + "=" * 40)

print(f"\n[1] Slot VLAN NIC ({NIC})")
ip_out = sh(f"ip addr show {NIC} 2>/dev/null")
if not ip_out:
    res(False, f"{NIC} exists", "interface not found",
        f"check NIC name: ip link")
else:
    has_ip = f"inet {SERVER_IP}/" in ip_out
    res(has_ip, f"{SERVER_IP}/24 assigned",
        "" if has_ip else "no slot-VLAN IP",
        f"sudo ip addr add {SERVER_IP}/24 dev {NIC} && sudo ip link set {NIC} up")
    carrier = "NO-CARRIER" not in ip_out and "state UP" in ip_out
    res(carrier, "link is UP with carrier",
        "" if carrier else "NO-CARRIER / link down",
        "plug the cable to the slot switch / machine, then: "
        f"sudo ip link set {NIC} up", warn=True)

print("\n[2] Ports")
tcp = sh("ss -tlnp 2>/dev/null")
m = re.search(r":8081\s.*?users:\(\((\"[^\"]+\")", tcp)
if ":8081 " in tcp or ":8081\n" in tcp or m:
    who = m.group(1) if m else "unknown process"
    if "g2s_host" in tcp:
        res(True, "TCP 8081", "g2s_host.py is already running")
    else:
        res(False, "TCP 8081 free", f"held by {who}",
            "kill the old server (check for webserver.py / diagnostic servers)")
else:
    res(True, "TCP 8081 free", "start g2s_host.py when ready")

udp = sh("ss -ulnp 2>/dev/null")
udp67 = [ln for ln in udp.splitlines() if ":67 " in ln]
if not udp67:
    res(True, "UDP 67 free", "start start-dhcp-enhanced.py (sudo) when ready")
elif all("virbr0" in ln for ln in udp67):
    res(True, "UDP 67", "only libvirt dnsmasq on virbr0 (coexists via "
        "SO_REUSEADDR + SO_BINDTODEVICE)")
else:
    res(False, "UDP 67 conflict", udp67[0].strip(),
        "stop the conflicting DHCP server (dnsmasq? old webserver.py?)")

print("\n[3] Process traps")
ps = sh("pgrep -af 'webserver.py|g2s_diagnostic|simple_server' 2>/dev/null")
ps = "\n".join(ln for ln in ps.splitlines()
               if "pgrep" not in ln and "/bin/sh" not in ln)
res(not ps.strip(), "no legacy servers running",
    ps.strip()[:80] if ps.strip() else "",
    "kill them — webserver.py's integrated DHCP fights on UDP 67")

print("\n[4] Firewall")
ufw = sh("sudo -n ufw status 2>/dev/null") or sh("ufw status 2>/dev/null")
if "inactive" in ufw:
    res(True, "ufw inactive", "nothing blocks 8081/67/69")
elif "active" in ufw:
    ok = "8081" in ufw
    res(ok, "ufw active with 8081 rule",
        "" if ok else "no 8081 allow rule",
        f"sudo ufw allow from 192.168.50.0/24 to any port 8081 proto tcp",
        warn=not ok)
else:
    res(True, "ufw status unknown (needs sudo)",
        "run: sudo ufw status — expect inactive", warn=True)

print("\n[5] Config sanity")
g2s_xml = (G2S_DIR / "tftp-root/igt/g2s.xml")
t = g2s_xml.read_text() if g2s_xml.exists() else ""
res("<Port>8081</Port>" in t and SERVER_IP in t,
    "tftp-root/igt/g2s.xml -> 192.168.50.2:8081",
    "" if t else "file missing", "restore from git")
cfg = G2S_DIR / "config/g2s_config.json"
try:
    c = json.loads(cfg.read_text())
    ok = c.get("http_port") == 8081 and not c.get("ssl_enabled", False)
    res(ok, "config/g2s_config.json http:8081 cert-less",
        f"http_port={c.get('http_port')} ssl={c.get('ssl_enabled')}",
        "set http_port/https_port 8081, ssl_enabled false")
except Exception as e:
    res(False, "config/g2s_config.json parses", str(e), "fix the JSON")

print("\n[6] AVP reachability (informational)")
ping = sh(f"ping -c1 -W1 {AVP_IP} 2>/dev/null")
if "1 received" in ping or "1 packets received" in ping:
    res(True, f"AVP answers at {AVP_IP}",
        "it already has its lease — skip waiting for DHCP")
else:
    res(True, f"AVP not pingable at {AVP_IP} yet",
        "normal if it's powered off / hasn't leased — DHCP will handle it")

print("\n" + "=" * 40)
if GO:
    extra = f" ({WARN} warning{'s' * (WARN != 1)})" if WARN else ""
    print(f"GO{extra} — start order:")
    print("  T1: sudo python3 start-dhcp-enhanced.py        (from G2S/)")
    print("  T2: python3 g2s_host.py --harvest              (NO sudo)")
    print(f"  T3: sudo tcpdump -i {NIC} -w logs/bench_$(date +%Y%m%d_%H%M%S).pcap host {AVP_IP}")
    print("  then power the AVP.")
else:
    print("NO-GO — fix the ❌ items above first.")
sys.exit(0 if GO else 1)
