#!/usr/bin/env python3
"""
Start the enhanced DHCP server with proper configuration.

This is a PRODUCT component, not just bench scaffolding: the host IS the
DHCP server on the wired slot segment, and this server hands machines their
IPs + G2S host URL (option 43). Interface and server IP are configurable:

    sudo python3 start-dhcp-enhanced.py                      # bench: eno1
    sudo python3 start-dhcp-enhanced.py --interface eth0     # deployed host
"""
import argparse
import os
import sys
import json

ap = argparse.ArgumentParser(description="CasinoNet slot-network DHCP server")
ap.add_argument("--interface", default="eno1",
                help="NIC to serve on (bench: eno1; Pi AP: wlan0; Pi wired: eth0)")
ap.add_argument("--server-ip", default="192.168.50.2",
                help="this host's IP on the slot network (also the G2S host)")
args = ap.parse_args()

# Add python/web to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'python/web'))

from dhcp_dns_server_enhanced import EnhancedDHCPServer

# Create proper config structure for enhanced server
config = {
    'enabled': True,
    'auto_start': True,
    'interface': args.interface,
    'network': {
        'base': '.'.join(args.server_ip.split('.')[:3]),
        'subnet': '255.255.255.0',
        'gateway': args.server_ip,
        'broadcast': '.'.join(args.server_ip.split('.')[:3]) + '.255',
        'server_ip': args.server_ip
    },
    'start_ip': 100,
    'end_ip': 200,
    'lease_time': 3600,
    'dns_servers': [args.server_ip],
    'domain': 'casinonet.local',
    'g2s_host': args.server_ip,
    'g2s_port': 8081,
    'g2s_https_port': 8334,
    'tftp_enabled': True,
    'tftp_root': 'tftp-root',
    'vendor_detection': True,
    'enable_pxe': True,
    'reservations': [],
    'router_ip': args.server_ip,      # offline slot net: the server IS the gw
    'ntp_servers': [args.server_ip]
}

# Save enhanced config
os.makedirs('python/web/data', exist_ok=True)
with open('python/web/data/dhcp_config_enhanced.json', 'w') as f:
    json.dump(config, f, indent=2)

# Create and start server
print("[DHCP] Starting Enhanced DHCP Server for IGT AVP")
print("[DHCP] Configuration:")
print(f"  Interface: {config['interface']}")
print(f"  Network: {config['network']['base']}.0/24")
print(f"  DHCP Range: {config['network']['base']}.{config['start_ip']} - {config['network']['base']}.{config['end_ip']}")
print(f"  G2S Host: {config['g2s_host']}:{config['g2s_port']}")

server = EnhancedDHCPServer(interface=args.interface,
                            config_file='python/web/data/dhcp_config_enhanced.json')
server.config = config  # Override with our config
server.save_config()

# Start the server
if server.start():
    print("[DHCP] Server started successfully")
    print("[DHCP] Press Ctrl+C to stop")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[DHCP] Stopping server...")
        server.stop()
else:
    print("[DHCP] Failed to start server")
    print("[DHCP] Make sure to run with sudo: sudo python3 start-dhcp-enhanced.py")