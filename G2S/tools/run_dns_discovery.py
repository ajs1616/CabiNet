#!/usr/bin/env python3
"""
DNS discovery server for the slot network.

Runs the DNS half of python/network/dhcp_dns_server.py ONLY (DHCP stays with
the enhanced server). It resolves the AVP's discovery names — g2shost.local,
g2s.local, casinonet.local, etc. — to the slot-net host IP so the AVP can find
the G2S host. Forces the slot interface and host IP explicitly so it can never
auto-detect to loopback (cloud-init's 127.0.1.1) or the Wi-Fi/home-LAN NIC.

    sudo python3 tools/run_dns_discovery.py            # eth0 / 192.168.50.2
    sudo python3 tools/run_dns_discovery.py --interface eth0 --server-ip 192.168.50.2
"""
import sys
import os
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'python', 'network'))

from dhcp_dns_server import CasinoNetDHCPDNSServer

ap = argparse.ArgumentParser(description="CasinoNet slot-network DNS discovery server")
ap.add_argument("--interface", default="eth0", help="slot NIC to pin DNS to")
ap.add_argument("--server-ip", default="192.168.50.2", help="slot-net host IP the names resolve to")
args = ap.parse_args()

srv = CasinoNetDHCPDNSServer()

# DNS-only — the enhanced DHCP server owns port 67.
srv.config['dhcp']['enabled'] = False
srv.config['dhcp']['interface'] = args.interface     # used for the SO_BINDTODEVICE pin + get_server_ip
srv.config['dns']['enabled'] = True
srv.config['server_ip'] = args.server_ip             # explicit override -> never loopback / wlan0

# Resolve every 'auto' discovery name to the slot-net host IP up front.
for host, ip in list(srv.config['dns']['hostname_mappings'].items()):
    if ip == 'auto':
        srv.config['dns']['hostname_mappings'][host] = args.server_ip

print(f"[DNS] discovery names -> {args.server_ip}  (pinned to {args.interface})")
for host, ip in srv.config['dns']['hostname_mappings'].items():
    print(f"      {host} -> {ip}")

if not srv.start():
    print("[DNS] failed to start", file=sys.stderr)
    sys.exit(1)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
