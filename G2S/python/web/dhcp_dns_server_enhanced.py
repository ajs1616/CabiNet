#!/usr/bin/env python3
"""
Enhanced DHCP/DNS Server for CasinoNet
Improved support for IGT Slimeline AVP and WMS BlueBird 2
"""

import os
import sys
import time
import socket
import struct
import threading
import json
import subprocess
import ipaddress
from datetime import datetime
import signal

# The slot VLAN server IP. DHCP may only ever be served on the interface that
# actually holds this address; serving anywhere else (e.g. the home LAN) would
# turn this into a rogue DHCP server on the user's network.
SLOT_SERVER_IP = '192.168.50.2'

class EnhancedDHCPServer:
    """Enhanced DHCP server with specific support for IGT AVP and WMS BlueBird 2"""
    
    def __init__(self, interface='auto', config_file='data/dhcp_config.json'):
        self.interface = interface
        self.config_file = config_file
        self.running = False
        self.thread = None
        self.sock = None
        self.leases = {}
        self.config = self.load_config()
        
        # Auto-detect interface ONLY when explicitly asked ('auto'). A real
        # interface name (eth0, wlan0, eno1) is an explicit pin and must never be
        # re-detected: auto_detect_interface() resolves via a route to 8.8.8.8,
        # which on the Pi is the wlan0 home-LAN — serving DHCP there would make
        # this a rogue DHCP server on the user's home network.
        if self.interface == 'auto':
            detected = self.auto_detect_interface()
            if detected:
                self.interface = detected
                print(f"[DHCP] Auto-detected interface: {self.interface}")
        
        # Update interface from config if available and not auto
        if 'interface' in self.config and self.config['interface'] and self.config['interface'] != 'auto':
            self.interface = self.config['interface']
        
        # Enhanced vendor-specific options for slot machines
        self.vendor_options = {
            'IGT': {
                    43: self.encode_igt_avp_options,  # IGT G2S vendor options (docs/DHCP_VENDOR_CONFIG.md)
                    # NOTE: option 60 is NOT set here on purpose — the offer builder
                    # echoes back the client's EXACT vendor class (e.g. 'IGT-AVP...').
                    # A literal 60: b'IGT' here would overwrite that echo with 'IGT'.
                    66: lambda: self.config['g2s_host'].encode('utf-8'),  # TFTP server name
                    67: b'igt/g2s.xml',  # Boot file in IGT directory
                    150: lambda: socket.inet_aton(self.config['g2s_host'])  # TFTP server IP
            },
            'WMS': {
                'vendor_class': ['WMS-Gaming', 'WMS-BB2', 'WMS-BlueBird'],
                'mac_prefixes': ['00:A0:A5', '00:1B:3F'],  # WMS MAC prefixes
                'options': {
                    43: self.encode_wms_bluebird_options, # Vendor Specific Info
                    # NOTE: option 60 is deliberately NOT set here — same wire
                    # lesson as IGT above: the offer builder echoes the
                    # client's EXACT vendor class, and a literal (the old
                    # 60: b'WMS-BB2') applied by the vendor-options loop
                    # OVERWROTE that echo in both OFFER and ACK. A BB2E that
                    # validates the echoed opt60 would silently ignore us.
                    66: self.encode_tftp_server,          # TFTP Server
                    67: b'wms/bluebird.cfg',              # Boot filename
                    15: self.encode_domain_name,          # Domain name
                    150: self.encode_cisco_tftp,          # Cisco TFTP (some WMS use this)
                }
            },
            'Aristocrat': {
                'vendor_class': ['Aristocrat-MK', 'Aristocrat-Helix'],
                'mac_prefixes': ['00:1C:23'],
                'options': {
                    43: self.encode_aristocrat_options,
                    # opt60 literal removed — would clobber the client echo
                    # (same landmine as the WMS/IGT note above).
                    15: self.encode_domain_name,
                }
            },
            'Bally': {
                'vendor_class': ['Bally-Alpha', 'Bally-Pro'],
                'mac_prefixes': ['00:0C:29', '00:50:56'],  # Often virtualized
                'options': {
                    43: self.encode_bally_options,
                    # opt60 literal removed — would clobber the client echo
                    # (same landmine as the WMS/IGT note above).
                    150: self.encode_cisco_tftp,
                }
            }
        }

        # Rogue-DHCP defence: the resolved interface must actually hold the slot
        # VLAN server IP. If it positively holds other IPv4 address(es) but NOT
        # SLOT_SERVER_IP, it is on the wrong network (e.g. the home LAN) and we
        # refuse to construct. Conservative: if the interface has no IPv4 yet, or
        # we can't read it, we log and proceed so the working eth0-pinned launcher
        # path (where eth0 does hold 192.168.50.2) is never broken.
        if self.interface and self.interface != 'auto':
            addrs = self._interface_ipv4_addrs(self.interface)
            if addrs and SLOT_SERVER_IP not in addrs:
                msg = (f"[DHCP] FATAL: interface {self.interface} holds {addrs} "
                       f"but not {SLOT_SERVER_IP}; refusing to start on the wrong "
                       f"network (rogue-DHCP guard)")
                print(msg)
                raise RuntimeError(msg)

    def _interface_ipv4_addrs(self, iface):
        """Return the list of IPv4 addresses configured on <iface>, or None if it
        can't be determined (callers fail-open rather than break a valid pin)."""
        try:
            result = subprocess.run(
                ['ip', '-4', '-o', 'addr', 'show', 'dev', iface],
                capture_output=True, text=True, timeout=3)
            if result.returncode != 0:
                return None
            addrs = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if 'inet' in parts:
                    idx = parts.index('inet')
                    if idx + 1 < len(parts):
                        addrs.append(parts[idx + 1].split('/')[0])
            return addrs
        except Exception:
            return None

    def load_config(self):
        """Load or create default configuration"""
        default_config = {
            'enabled': False,
            'auto_start': False,
            'interface': 'eth0',  # never 'auto' — see the rogue-DHCP guard in __init__
            'network': self.detect_network(),
            'start_ip': 100,
            'end_ip': 200,
            'lease_time': 3600,
            'dns_servers': [],  # Will be auto-populated with server IP
            'domain': 'casinonet.local',
            'g2s_host': self.get_local_ip(),
            'g2s_port': 8081,
            'g2s_https_port': 8334,
            'tftp_enabled': True,
            'tftp_root': 'tftp-root',
            'vendor_detection': True,
            'enable_pxe': True,
            'reservations': [],
            'router_ip': None,  # Auto-detect
            'ntp_servers': [],  # Will be auto-populated
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    loaded = json.load(f)
                    # Merge with defaults
                    for key, value in default_config.items():
                        if key not in loaded:
                            loaded[key] = value
                    return loaded
            else:
                self.save_config(default_config)
                return default_config
        except Exception as e:
            print(f"[DHCP] Error loading config: {e}")
            return default_config
    
    def detect_network(self):
        """Automatically detect network configuration"""
        try:
            local_ip = self.get_local_ip()
            ip_obj = ipaddress.ip_address(local_ip)
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
            
            return {
                'base': str(network.network_address).rsplit('.', 1)[0],
                'subnet': '255.255.255.0',
                'gateway': str(network.network_address + 1),
                'broadcast': str(network.broadcast_address),
                'server_ip': local_ip
            }
        except Exception as e:
            print(f"[DHCP] Network detection error: {e}")
            return {
                'base': '192.168.77',
                'subnet': '255.255.255.0',
                'gateway': '192.168.77.1',
                'broadcast': '192.168.77.255',
                'server_ip': '192.168.77.1'
            }
    
    def get_local_ip(self):
        """Get the local IP address"""
        try:
            # Try to get IP that routes to internet
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return '192.168.77.1'
    
    def auto_detect_interface(self):
        """Auto-detect the best network interface"""
        try:
            # Method 1: Use the interface that has our IP
            local_ip = self.get_local_ip()
            
            # Get all interfaces
            import netifaces
            for iface in netifaces.interfaces():
                if iface == 'lo':
                    continue
                    
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        if addr_info['addr'] == local_ip:
                            return iface
            
            # Method 2: First non-loopback interface with an IP
            for iface in netifaces.interfaces():
                if iface == 'lo':
                    continue
                    
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        ip = addr_info['addr']
                        if not ip.startswith('127.'):
                            return iface
                            
        except ImportError:
            # Fallback without netifaces
            import subprocess
            try:
                # Try to get the default route interface
                result = subprocess.run(['ip', 'route', 'show', 'default'], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    # Extract interface from "default via x.x.x.x dev wlp2s0"
                    parts = result.stdout.split()
                    if 'dev' in parts:
                        idx = parts.index('dev')
                        if idx + 1 < len(parts):
                            return parts[idx + 1]
            except:
                pass
                
        # Default fallback
        return None
    
    def encode_igt_avp_options(self):
        """Encode IGT AVP G2S vendor options (DHCP Option 43).

        Per IGT AVP documentation, Option 43 is ONE TLV sub-option and nothing
        else:  Type 0x01, Length 0x04, Value = the 4-byte G2S host (Ego/CMS)
        IPv4 address.  e.g. host 192.168.50.2 -> 0104 C0A83202.

        The AVP takes ONLY the host IP from DHCP and supplies its own protocol
        and port (URI segments 1 & 3).  Earlier builds appended extra
        sub-options (port / path / 'SOAP' / auto-discovery / config URL); those
        made the payload malformed to the AVP's parser, so it discarded the
        whole option and kept its 127.0.0.1 default."""
        return b'\x01\x04' + socket.inet_aton(self.config['g2s_host'])
    
    def encode_wms_bluebird_options(self):
        """Encode WMS BlueBird 2 specific vendor options"""
        server_ip = self.config['g2s_host']
        
        options = b''
        
        # Sub-option 1: G2S URL (WMS prefers full URL)
        protocol = self.get_protocol()
        port = self.get_g2s_port()
        g2s_url = f"{protocol}://{server_ip}:{port}/G2S".encode()
        options += b'\x01' + bytes([len(g2s_url)]) + g2s_url
        
        # Sub-option 2: Auto-register flag
        options += b'\x02\x01\x01'
        
        # Sub-option 3: Protocol version
        protocol = b'G2S_v1.0.3'
        options += b'\x03' + bytes([len(protocol)]) + protocol
        
        # Sub-option 4: Heartbeat interval (seconds)
        options += b'\x04\x02\x00\x3C'  # 60 seconds
        
        # Sub-option 5: Configuration download URL
        config_url = f"{protocol}://{server_ip}:{port}/wms/config".encode()
        options += b'\x05' + bytes([len(config_url)]) + config_url
        
        # Sub-option 10: Asset download server
        asset_url = f"{protocol}://{server_ip}:{port}/assets".encode()
        options += b'\x0A' + bytes([len(asset_url)]) + asset_url
        
        return options
    
    def get_protocol(self):
        """Get protocol (http/https) based on configuration"""
        try:
            # Check if g2s_config.json exists and has SSL settings
            g2s_config_file = 'config/g2s_config.json'
            if os.path.exists(g2s_config_file):
                with open(g2s_config_file, 'r') as f:
                    g2s_config = json.load(f)
                    if g2s_config.get('ssl_enabled', False):
                        return 'http'
        except:
            pass
        # Use HTTP port
        return 'http'
    
    def get_g2s_port(self):
        """Get G2S port based on protocol"""
        try:
            g2s_config_file = 'config/g2s_config.json'
            if os.path.exists(g2s_config_file):
                with open(g2s_config_file, 'r') as f:
                    g2s_config = json.load(f)
                    # Always use HTTPS port for SSL version
                    return g2s_config.get('http_port', 8081)
        except:
            pass
        # Check dhcp_config for port - always use HTTPS port for SSL
        return self.config.get('g2s_port', 8081)
    
    def encode_tftp_server(self):
        """Encode TFTP server name (Option 66)"""
        return self.config['g2s_host'].encode('utf-8')
    
    def encode_tftp_filename(self):
        """Encode TFTP boot filename (Option 67)"""
        # IGT expects segment3.cfg in igt directory
        return b'igt/segment3.cfg'
    
    def encode_domain_name(self):
        """Encode domain name (Option 15)"""
        return self.config.get('domain', 'casinonet.local').encode('utf-8')
    
    def encode_domain_search(self):
        """Encode domain search list (Option 119)"""
        # DNS search domains in RFC3397 format
        domains = ['casinonet.local', 'g2s.local', 'local']
        encoded = b''
        
        for domain in domains:
            parts = domain.split('.')
            for part in parts:
                encoded += bytes([len(part)]) + part.encode('utf-8')
            encoded += b'\x00'  # Domain separator
            
        return encoded
    
    def encode_cisco_tftp(self):
        """Encode Cisco TFTP server (Option 150)"""
        # Some WMS machines use Cisco phone TFTP option
        server_ip = socket.inet_aton(self.config['g2s_host'])
        return server_ip
    
    def encode_aristocrat_options(self):
        """Encode Aristocrat vendor-specific options"""
        host_ip = self.config['g2s_host'].encode('utf-8')
        
        options = b''
        options += b'\x01' + bytes([len(host_ip)]) + host_ip
        options += b'\x10\x04\x00\x00\x00\x01'  # Site ID
        
        return options
    
    def encode_bally_options(self):
        """Encode Bally vendor-specific options"""
        host_ip = self.config['g2s_host'].encode('utf-8')
        
        options = b''
        options += b'\x01' + bytes([len(host_ip)]) + host_ip
        options += b'\x02\x02\x04\xD2'  # SDS Port
        
        return options
    
    def detect_vendor(self, packet):
        """Enhanced vendor detection for slot machines"""
        # First check vendor class identifier (option 60)
        if 60 in packet.get('options', {}):
            vendor_class = packet['options'][60].decode('utf-8', errors='ignore')
            print(f"[DHCP] Option 60 received from client: '{vendor_class}' (hex: {packet['options'][60].hex()})")
            
            # Check IGT first (special case)
            if 'igt' in vendor_class.lower():
                print(f"[DHCP] Detected IGT by vendor class: {vendor_class}")
                return 'IGT'
            
            # Check other vendors
            for vendor, info in self.vendor_options.items():
                if vendor == 'IGT':
                    continue  # Already checked
                if 'vendor_class' in info:
                    for vc in info['vendor_class']:
                        if vc.lower() in vendor_class.lower():
                            print(f"[DHCP] Detected {vendor} by vendor class: {vendor_class}")
                            return vendor
        
        # Check by MAC address prefix
        mac = packet.get('mac', '')
        if mac:
            mac_prefix = mac[:8].upper().replace(':', '')
            
            for vendor, info in self.vendor_options.items():
                if vendor == 'IGT':
                    continue  # IGT doesn't have mac_prefixes
                if 'mac_prefixes' in info:
                    for prefix in info['mac_prefixes']:
                        check_prefix = prefix.replace(':', '').upper()
                        if mac_prefix.startswith(check_prefix):
                            print(f"[DHCP] Detected {vendor} by MAC prefix: {prefix}")
                            return vendor
        
        # Check hostname if provided
        if 12 in packet.get('options', {}):
            hostname = packet['options'][12].decode('utf-8', errors='ignore').lower()
            if 'igt' in hostname or 'avp' in hostname:
                return 'IGT'
            elif 'wms' in hostname or 'bluebird' in hostname or 'bb2' in hostname:
                return 'WMS'
            elif 'aristocrat' in hostname:
                return 'Aristocrat'
            elif 'bally' in hostname:
                return 'Bally'
        
        print(f"[DHCP] Unknown vendor for MAC {mac}")
        return 'Generic'
    
    def create_dhcp_offer(self, request, offered_ip, vendor='Generic'):
        """Create DHCP OFFER packet with vendor-specific options"""
        packet = {
            'op': 2,  # BOOTREPLY
            'htype': 1,  # Ethernet
            'hlen': 6,
            'hops': 0,
            'xid': request['xid'],
            'secs': 0,
            'flags': request.get('flags', 0),
            'ciaddr': '0.0.0.0',
            'yiaddr': offered_ip,
            'siaddr': self.config['network']['server_ip'],
            'giaddr': '0.0.0.0',
            'chaddr': request['chaddr'],
            'sname': b'CasinoNet-G2S',
            'file': b'',
            'options': {}
        }
        
        # Standard DHCP options
        packet['options'][53] = b'\x02'  # DHCP Offer
        packet['options'][1] = socket.inet_aton(self.config['network']['subnet'])  # Subnet mask
        packet['options'][3] = socket.inet_aton(self.config['network']['gateway'])  # Router
        packet['options'][51] = struct.pack('!I', self.config['lease_time'])  # Lease time
        packet['options'][54] = socket.inet_aton(self.config['network']['server_ip'])  # DHCP server
        
        # DNS servers - include our server first
        dns_servers = socket.inet_aton(self.config['network']['server_ip'])
        for dns in self.config.get('dns_servers', ['8.8.8.8']):
            dns_servers += socket.inet_aton(dns)
        packet['options'][6] = dns_servers
        
        # Domain name
        if self.config.get('domain'):
            packet['options'][15] = self.config['domain'].encode('utf-8')
        
        # NTP servers (important for slot machines)
        if self.config.get('ntp_servers'):
            ntp_data = b''
            for ntp in self.config['ntp_servers']:
                ntp_data += socket.inet_aton(ntp)
            packet['options'][42] = ntp_data
        else:
            # Use server as NTP
            packet['options'][42] = socket.inet_aton(self.config['network']['server_ip'])
        
        # Add vendor-specific options
        
        # Echo back vendor class (Option 60) - IGT requires this
        if 60 in request.get('options', {}):
            # Echo back exactly what IGT sent us
            packet['options'][60] = request['options'][60]
            print(f"[DHCP] Echoing vendor class: {request['options'][60]}")
        elif vendor == 'IGT':
            # Default IGT vendor class if not provided
            packet['options'][60] = b'IGT'
        if vendor in self.vendor_options:
            # Handle IGT's direct options format
            if vendor == 'IGT':
                vendor_opts = self.vendor_options[vendor]
            else:
                vendor_opts = self.vendor_options[vendor].get('options', {})
            
            for opt_code, opt_func in vendor_opts.items():
                if callable(opt_func):
                    packet['options'][opt_code] = opt_func()
                else:
                    packet['options'][opt_code] = opt_func
        
        # Option 72: WWW Server - RFC 2132 defines this as a 4-byte IPv4 address,
        # not a URL string (IGT expects the bare IP).
        packet['options'][72] = socket.inet_aton(self.config['g2s_host'])
        
        # Always add TFTP options for IGT devices
        if vendor == 'IGT' or (60 in packet.get('options', {}) and b'IGT' in packet['options'][60]):
            # Option 66: TFTP Server Name
            if 66 not in packet['options']:
                packet['options'][66] = self.config['g2s_host'].encode('utf-8')
            
            # Option 150: TFTP Server Address (Cisco format)
            if 150 not in packet['options']:
                packet['options'][150] = socket.inet_aton(self.config['g2s_host'])
            
            # Option 67: Boot File Name (already added via vendor options, but ensure it's
            # there). GR-23: point the fallback at the canonical igt/g2s.xml (http/8081)
            # so it agrees with the vendor-options path instead of the stale root file.
            if 67 not in packet['options']:
                packet['options'][67] = b'igt/g2s.xml'
        
        return packet
    
    def handle_dhcp_request(self, packet):
        """Handle DHCP REQUEST and send ACK"""
        mac = packet['mac']
        
        # Get the requested IP from options
        requested_ip = None
        if 50 in packet['options']:  # Requested IP option
            requested_ip = socket.inet_ntoa(packet['options'][50])
        
        # Check if we have this lease
        if mac in self.leases:
            lease_ip = self.leases[mac]['ip']
            if requested_ip and requested_ip != lease_ip:
                print(f"[DHCP] REQUEST from {mac} for {requested_ip}, but lease is {lease_ip}")
                return None
            
            # Update lease state
            self.leases[mac]['state'] = 'acked'
            self.leases[mac]['timestamp'] = time.time()
            
            print(f"[DHCP] REQUEST from {mac} - Sending ACK for {lease_ip}")
            
            # Create ACK packet (similar to offer but msg type 5)
            vendor = self.leases[mac].get('vendor', 'Generic')
            ack_packet = self.create_dhcp_offer(packet, lease_ip, vendor)
            ack_packet['options'][53] = b'\x05'  # DHCP ACK
            
            return ack_packet
        else:
            print(f"[DHCP] REQUEST from unknown MAC {mac}")
            return None
    
    def handle_dhcp_discover(self, packet):
        """Handle DHCP DISCOVER with vendor detection"""
        mac = packet['mac']
        vendor = self.detect_vendor(packet)
        
        # Find available IP
        offered_ip = self.find_available_ip(mac)
        if not offered_ip:
            print(f"[DHCP] No available IPs for {mac}")
            return None
        
        print(f"[DHCP] DISCOVER from {mac} ({vendor}) - Offering {offered_ip}")
        
        # Create lease entry
        self.leases[mac] = {
            'ip': offered_ip,
            'vendor': vendor,
            'state': 'offered',
            'timestamp': time.time()
        }
        
        return self.create_dhcp_offer(packet, offered_ip, vendor)
    
    def send_dhcp_response(self, packet, addr):
        """Send DHCP response packet"""
        try:
            # Build DHCP packet - allocate larger buffer for options
            response = bytearray(576)  # Minimum DHCP packet size
            
            # Fixed header
            response[0] = packet['op']  # Message type (2 = BOOTREPLY)
            response[1] = packet['htype']  # Hardware type
            response[2] = packet['hlen']  # Hardware address length
            response[3] = packet.get('hops', 0)  # Hops
            response[4:8] = struct.pack('!I', packet['xid'])  # Transaction ID
            response[8:10] = struct.pack('!H', packet.get('secs', 0))  # Seconds elapsed
            response[10:12] = struct.pack('!H', packet.get('flags', 0))  # Flags
            
            # IP addresses
            response[12:16] = socket.inet_aton(packet.get('ciaddr', '0.0.0.0'))  # Client IP
            response[16:20] = socket.inet_aton(packet['yiaddr'])  # Your IP (offered)
            response[20:24] = socket.inet_aton(packet['siaddr'])  # Server IP
            response[24:28] = socket.inet_aton(packet.get('giaddr', '0.0.0.0'))  # Gateway IP
            
            # Client hardware address (16 bytes field)
            chaddr = packet['chaddr']
            if isinstance(chaddr, bytes):
                response[28:28+min(16, len(chaddr))] = chaddr[:16]
            
            # Server hostname (64 bytes)
            sname = packet.get('sname', b'CasinoNet-G2S')
            if isinstance(sname, str):
                sname = sname.encode('utf-8')
            response[44:44+min(64, len(sname))] = sname[:64]
            
            # Boot filename (128 bytes)
            file = packet.get('file', b'')
            if isinstance(file, str):
                file = file.encode('utf-8')
            response[108:108+min(128, len(file))] = file[:128]
            
            # DHCP magic cookie
            response[236:240] = b'\x63\x82\x53\x63'
            
            # Add options
            offset = 240
            for opt_code, opt_data in packet.get('options', {}).items():
                if isinstance(opt_data, str):
                    opt_data = opt_data.encode('utf-8')
                if len(opt_data) > 255:
                    # A DHCP option's length is a single byte; skip an oversized
                    # option rather than let the ValueError from the length-byte
                    # assignment be swallowed and silently drop the ENTIRE offer.
                    print(f"[DHCP] Skipping option {opt_code}: {len(opt_data)} "
                          f"bytes exceeds the 255-byte DHCP option limit")
                    continue
                if offset + 2 + len(opt_data) < len(response) - 1:  # Leave room for end option
                    response[offset] = opt_code
                    response[offset + 1] = len(opt_data)
                    response[offset + 2:offset + 2 + len(opt_data)] = opt_data
                    offset += 2 + len(opt_data)
            
            # End option
            if offset < len(response):
                response[offset] = 255
                offset += 1
            
            # Trim packet to actual size
            response = bytes(response[:offset])
            
            # Send to broadcast address
            dest_addr = ('255.255.255.255', 68)
            self.sock.sendto(response, dest_addr)
            print(f"[DHCP] Sent {len(response)} byte response to {dest_addr}")
            
        except Exception as e:
            print(f"[DHCP] Error sending response: {e}")
            import traceback
            traceback.print_exc()
    
    def find_available_ip(self, mac):
        """Find available IP address"""
        # Check if MAC already has a lease
        if mac in self.leases:
            return self.leases[mac]['ip']
        
        # Check reservations
        for reservation in self.config.get('reservations', []):
            if reservation['mac'].lower() == mac.lower():
                return reservation['ip']
        
        # Find available IP in range
        network = self.config['network']
        base = network['base']
        
        for i in range(self.config['start_ip'], self.config['end_ip'] + 1):
            ip = f"{base}.{i}"
            
            # Check if IP is already leased
            ip_used = False
            for lease in self.leases.values():
                if lease['ip'] == ip:
                    ip_used = True
                    break
            
            if not ip_used:
                return ip
        
        return None
    
    def save_config(self, config=None):
        """Save configuration"""
        if config:
            self.config = config
        
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
            return True
        except Exception as e:
            print(f"[DHCP] Error saving config: {e}")
            return False
    
    def get_status(self):
        """Get DHCP server status"""
        return {
            'running': self.running,
            'config': self.config,
            'leases': self.leases,
            'vendors_supported': list(self.vendor_options.keys())
        }
    
    def start(self):
        """Start the DHCP server"""
        if self.running:
            return True
        
        try:
            # Create socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            # Try to bind to DHCP server port
            try:
                self.sock.bind(('', 67))
                print(f"[DHCP] Successfully bound to port 67")
                
                # Pin the socket to one interface. HARD safety requirement: without
                # SO_BINDTODEVICE the server answers DHCP DISCOVERs from EVERY
                # interface (incl. the Pi's wlan0 home-LAN) and becomes a rogue DHCP
                # server. If we can't pin, we refuse to serve rather than fall back
                # to all-interfaces.
                if self.interface and self.interface != 'auto':
                    try:
                        SO_BINDTODEVICE = getattr(socket, 'SO_BINDTODEVICE', 25)
                        self.sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                                             self.interface.encode() + b'\0')
                        print(f"[DHCP] Socket pinned to interface {self.interface} (SO_BINDTODEVICE)")
                    except Exception as e:
                        print(f"[DHCP] FATAL: cannot pin socket to {self.interface}: {e}")
                        print(f"[DHCP] Refusing to serve DHCP on all interfaces (rogue-DHCP guard)")
                        self.sock.close()
                        raise
                else:
                    print(f"[DHCP] FATAL: no explicit interface; refusing to serve DHCP "
                          f"on all interfaces (rogue-DHCP guard)")
                    self.sock.close()
                    raise RuntimeError("DHCP server requires an explicit interface")

            except OSError as e:
                if e.errno == 98:  # Address already in use
                    print(f"[DHCP] Port 67 already in use - is another DHCP server running?")
                elif e.errno == 13:  # Permission denied
                    print(f"[DHCP] Permission denied binding to port 67 - run with sudo")
                else:
                    print(f"[DHCP] Failed to bind to port 67: {e}")
                raise
            
            self.running = True
            self.thread = threading.Thread(target=self.run)
            self.thread.daemon = True
            self.thread.start()
            
            print(f"[DHCP] Enhanced DHCP server started on {self.interface}")
            return True
        except Exception as e:
            print(f"[DHCP] Failed to start: {e}")
            return False
    
    def stop(self):
        """Stop the DHCP server"""
        self.running = False
        if self.sock:
            self.sock.close()
        if self.thread:
            self.thread.join(timeout=2)
        print("[DHCP] Enhanced DHCP server stopped")
        
    def run(self):
        """Main DHCP server loop"""
        print(f"[DHCP] Thread started, entering main loop on {self.interface}")
        print(f"[DHCP] Socket info: fileno={self.sock.fileno()}, timeout={self.sock.gettimeout()}")
        
        # Set socket to non-blocking with timeout
        self.sock.settimeout(1.0)
        
        packet_count = 0
        last_debug = time.time()
        
        while self.running:
            try:
                # Periodic debug message
                if time.time() - last_debug > 10:
                    print(f"[DHCP] Still listening on {self.interface}... ({packet_count} packets received so far)")
                    last_debug = time.time()
                
                data, addr = self.sock.recvfrom(1024)
                packet_count += 1
                print(f"[DHCP] Received packet #{packet_count} from {addr}")
                print(f"[DHCP] Packet size: {len(data)} bytes")
                
                # Log first few bytes
                if len(data) >= 4:
                    print(f"[DHCP] First 4 bytes: {' '.join(f'{b:02x}' for b in data[:4])}")
                
                # Handle DHCP packet
                self.handle_dhcp_packet(data, addr)
                
            except socket.timeout:
                # Timeout is normal, just continue
                continue
            except Exception as e:
                if self.running:
                    print(f"[DHCP] Error in server loop: {e}")
                    import traceback
                    traceback.print_exc()
    
    def handle_dhcp_packet(self, data, addr):
        """Handle incoming DHCP packet"""
        try:
            # Basic DHCP packet parsing
            if len(data) < 240:
                print(f"[DHCP] Packet too small ({len(data)} bytes), ignoring")
                return
            
            # Verify DHCP magic cookie
            if data[236:240] != b'\x63\x82\x53\x63':
                print(f"[DHCP] Invalid magic cookie, not a DHCP packet")
                return
            
            # Parse basic fields
            op = data[0]
            htype = data[1]
            hlen = data[2]
            xid = struct.unpack('!I', data[4:8])[0]
            mac_bytes = data[28:28+hlen]
            mac = ':'.join(f'{b:02x}' for b in mac_bytes)
            
            print(f"[DHCP] Packet details: op={op}, mac={mac}, xid={xid:08x}")
            
            # Parse options
            options = {}
            i = 240
            while i < len(data):
                if data[i] == 255:  # End option
                    break
                elif data[i] == 0:  # Pad option
                    i += 1
                    continue
                
                opt_type = data[i]
                if i + 1 >= len(data):
                    break
                    
                opt_len = data[i + 1]
                if i + 2 + opt_len > len(data):
                    break
                    
                opt_data = data[i + 2:i + 2 + opt_len]
                options[opt_type] = opt_data
                i += 2 + opt_len
            
            # Check message type
            msg_type = None
            if 53 in options and len(options[53]) >= 1:
                msg_type = options[53][0]
                
            msg_type_names = {1: 'DISCOVER', 2: 'OFFER', 3: 'REQUEST', 4: 'DECLINE',
                            5: 'ACK', 6: 'NAK', 7: 'RELEASE', 8: 'INFORM'}
            msg_name = msg_type_names.get(msg_type, f'UNKNOWN({msg_type})')
            
            print(f"[DHCP] Message type: {msg_name}")

            # Host-discovery evidence for the bring-up cockpit. On every
            # DISCOVER/REQUEST, log which options the client actually asks for
            # (option 55 = Parameter Request List, one option code per byte) plus
            # its vendor class (option 60). The console parses these EXACT lines
            # to prove whether the AVP ever even asks DHCP for a G2S host option
            # (opt43 / opt125) — the definitive on-wire answer to "is the host
            # manual-URI only?". Option 55 is parsed defensively: it may be absent.
            if msg_type in (1, 3):  # DISCOVER / REQUEST
                req_codes = list(options.get(55, b''))
                print(f"[DHCP] client {mac} opt55 param-request: "
                      f"{','.join(str(c) for c in req_codes)}")
                opt60 = options.get(60)
                vendor_class = opt60.decode('utf-8', errors='ignore') if opt60 else '(none)'
                print(f"[DHCP] client {mac} opt60 vendor-class: '{vendor_class}'")
                opt43_req = 'yes' if 43 in req_codes else 'no'
                opt125_req = 'yes' if 125 in req_codes else 'no'
                print(f"[DHCP] host-discovery verdict: "
                      f"opt43_requested={opt43_req} opt125_requested={opt125_req}")

            # Create packet dict
            packet = {
                'op': op,
                'htype': htype,
                'hlen': hlen,
                'xid': xid,
                'mac': mac,
                'chaddr': mac_bytes,
                'options': options
            }
            
            if msg_type == 1:  # DHCP Discover
                response = self.handle_dhcp_discover(packet)
                if response:
                    # Send DHCP Offer
                    self.send_dhcp_response(response, addr)
            elif msg_type == 3:  # DHCP Request
                print(f"[DHCP] Received DHCP Request from {mac}")
                # Send DHCP ACK
                response = self.handle_dhcp_request(packet)
                if response:
                    self.send_dhcp_response(response, addr)
                
        except Exception as e:
            print(f"[DHCP] Error handling packet: {e}")
            import traceback
            traceback.print_exc()


# Create singleton instances
_dhcp_server = None
_dns_server = None

def get_dhcp_server():
    """Get or create DHCP server instance"""
    global _dhcp_server
    if _dhcp_server is None:
        # Load config to get interface
        import json
        import os
        config_file = 'data/dhcp_config.json'
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config = json.load(f)
                interface = config.get('interface', 'eth0')
        else:
            interface = 'eth0'
        _dhcp_server = EnhancedDHCPServer(interface=interface)
    return _dhcp_server

def get_dns_server():
    """Get or create DNS server instance"""
    global _dns_server
    if _dns_server is None:
        from dhcp_dns_server import DNSServer
        _dns_server = DNSServer()
    return _dns_server

if __name__ == "__main__":
    print("Enhanced DHCP Server for IGT AVP and WMS BlueBird 2")
    print("Vendor-specific options configured for:")
    print("- IGT Slimeline AVP")
    print("- WMS BlueBird 2")
    print("- Aristocrat MK")
    print("- Bally Alpha")