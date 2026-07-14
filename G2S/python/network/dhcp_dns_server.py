#!/usr/bin/env python3
"""
CasinoNet Built-In DNS and DHCP Server
Lightweight DNS and DHCP server for plug-and-play G2S host discovery
Works offline without internet access or manual configuration
"""

import json
import subprocess
import socket
import threading
import time
import os
import logging
import ipaddress
import struct
from datetime import datetime, timedelta
from pathlib import Path

class CasinoNetDHCPDNSServer:
    def __init__(self, config_path='config/network_config.json', data_dir='data'):
        self.config_path = config_path
        self.data_dir = Path(data_dir)
        self.log_file = self.data_dir / 'network_log.json'
        self.running = False
        self.dhcp_thread = None
        self.dns_thread = None
        self.dhcp_socket = None
        self.dns_socket = None
        
        # Ensure directories exist
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
        
        # Load configuration
        self.config = self.load_config()
        
        # Setup logging
        self.setup_logging()
        
        # DHCP lease tracking
        self.dhcp_leases = {}
        self.load_leases()
        
    def load_config(self):
        """Load network configuration from JSON file"""
        default_config = {
            "dhcp": {
                "enabled": True,
                "interface": "auto",  # auto-detect or specify interface
                "ip_range": {
                    "start": "192.168.77.100",
                    "end": "192.168.77.200",
                    "subnet": "192.168.77.0/24",
                    "gateway": "192.168.77.1"
                },
                "lease_time": 3600,  # 1 hour
                "static_leases": {
                    # "aa:bb:cc:dd:ee:ff": "192.168.77.50"
                },
                "options": {
                    "dns_servers": ["192.168.77.1"],  # Point to our DNS
                    "domain_name": "casinonet.local"
                }
            },
            "dns": {
                "enabled": True,
                "port": 53,
                "hostname_mappings": {
                    "g2s.local": "auto",  # auto = use server IP
                    "g2shost.local": "auto",
                    "casinonet.local": "auto",
                    "slothost.local": "auto"
                },
                # Intentionally EMPTY: the slot net is offline by design.
                # Forwarding to public DNS leaks home-LAN answers onto the
                # slot net and, since forward_dns_query() runs inline in the
                # single-threaded DNS loop (2 s timeout per upstream), a dead
                # uplink stalls ALL answers — g2s.local included. See
                # config/network_config.json (_forward_dns_comment).
                "forward_dns": []
            },
            "security": {
                "restrict_to_ethernet": False,
                "allowed_interfaces": [],  # Empty = allow all
                "rate_limiting": {
                    "dhcp_max_per_minute": 60,
                    "dns_max_per_minute": 300
                }
            },
            "logging": {
                "log_dhcp_assignments": True,
                "log_dns_queries": True,
                "max_log_entries": 1000
            }
        }
        
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    user_config = json.load(f)
                # Merge with defaults
                config = self.deep_merge(default_config, user_config)
            except Exception as e:
                print(f"Error loading config: {e}, using defaults")
                config = default_config
        else:
            config = default_config
            # Save default config
            self.save_config(config)
        
        return config
    
    def save_config(self, config):
        """Save configuration to JSON file"""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.log_event('error', f'Failed to save config: {e}')
    
    def deep_merge(self, base, override):
        """Deep merge two dictionaries"""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self.deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - CasinoNet - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('CasinoNet')
    
    def get_server_ip(self):
        """Get the server's slot-network IP. Never loopback, never the
        internet-facing (home-LAN/Wi-Fi) NIC: EGMs reach us only on the slot
        interface, so an explicit config value or the slot NIC's own IP wins.
        The old code accepted 127.0.1.1 (cloud-init maps the hostname to
        loopback in /etc/hosts) and the 8.8.8.8-route IP (= wlan0); both are
        useless to the AVP and made it fall back to a 127.0.0.1 G2S host."""
        # 1. Explicit override — deterministic, no auto-detect guesswork.
        explicit = self.config.get('server_ip') or self.config.get('dns', {}).get('server_ip')
        if explicit and not str(explicit).startswith('127.'):
            return explicit
        try:
            methods = [
                self._get_ip_via_interface,   # IP bound to the slot NIC (e.g. eth0)
                self._get_ip_via_route,
                self._get_ip_via_socket,
                self._get_ip_via_hostname,
            ]
            for method in methods:
                try:
                    ip = method()
                    if ip and not str(ip).startswith('127.'):   # reject ALL loopback
                        return ip
                except Exception:
                    continue
            # Fallback to gateway IP from config
            return self.config['dhcp']['ip_range']['gateway']
        except Exception:
            return "192.168.77.1"

    def _get_ip_via_interface(self):
        """IP currently assigned to the configured slot interface (e.g. eth0)."""
        iface = self.config.get('dhcp', {}).get('interface')
        if not iface or iface == 'auto':
            return None
        out = subprocess.run(['ip', '-4', '-o', 'addr', 'show', iface],
                             capture_output=True, text=True)
        for line in out.stdout.split('\n'):
            parts = line.split()
            if 'inet' in parts:
                ip = parts[parts.index('inet') + 1].split('/')[0]
                if ip and not ip.startswith('127.'):
                    return ip
        return None
    
    def _get_ip_via_route(self):
        """Get IP via ip route command"""
        result = subprocess.run(['ip', 'route', 'get', '8.8.8.8'], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'src' in line:
                    parts = line.split()
                    if 'src' in parts:
                        idx = parts.index('src') + 1
                        if idx < len(parts):
                            return parts[idx]
        return None
    
    def _get_ip_via_socket(self):
        """Get IP via socket connection"""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    
    def _get_ip_via_hostname(self):
        """Get IP via hostname resolution"""
        return socket.gethostbyname(socket.gethostname())
    
    def load_leases(self):
        """Load DHCP leases from file"""
        lease_file = self.data_dir / 'dhcp_leases.json'
        try:
            if lease_file.exists():
                with open(lease_file, 'r') as f:
                    saved_leases = json.load(f)
                    
                # Convert and validate leases
                current_time = time.time()
                for mac, lease_data in saved_leases.items():
                    if lease_data['expires'] > current_time:
                        self.dhcp_leases[mac] = lease_data
        except Exception as e:
            self.logger.warning(f"Failed to load DHCP leases: {e}")
    
    def save_leases(self):
        """Save DHCP leases to file"""
        lease_file = self.data_dir / 'dhcp_leases.json'
        try:
            with open(lease_file, 'w') as f:
                json.dump(self.dhcp_leases, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save DHCP leases: {e}")
    
    def log_event(self, event_type, message, details=None):
        """Log events to network log file"""
        if not self.config['logging'].get(f'log_{event_type}_assignments', True) and event_type == 'dhcp':
            return
        if not self.config['logging'].get(f'log_{event_type}_queries', True) and event_type == 'dns':
            return
        
        log_entry = {
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'type': event_type,
            'message': message
        }
        
        if details:
            log_entry.update(details)
        
        try:
            # Load existing log
            if self.log_file.exists():
                with open(self.log_file, 'r') as f:
                    log_data = json.load(f)
            else:
                log_data = []
            
            # Add new entry
            log_data.append(log_entry)
            
            # Trim log if too long
            max_entries = self.config['logging'].get('max_log_entries', 1000)
            if len(log_data) > max_entries:
                log_data = log_data[-max_entries:]
            
            # Save log
            with open(self.log_file, 'w') as f:
                json.dump(log_data, f, indent=2)
                
        except Exception as e:
            self.logger.error(f"Failed to log event: {e}")
    
    def start(self):
        """Start DNS and DHCP servers"""
        if self.running:
            self.logger.warning("Server already running")
            return False
        
        self.logger.info("🌐 Starting CasinoNet DNS/DHCP Server")
        self.running = True
        
        # Detect server IP
        server_ip = self.get_server_ip()
        self.logger.info(f"🎯 Server IP detected: {server_ip}")
        
        # Update auto mappings
        for hostname, ip in self.config['dns']['hostname_mappings'].items():
            if ip == "auto":
                self.config['dns']['hostname_mappings'][hostname] = server_ip
        
        # Start services
        success = True
        
        if self.config['dhcp']['enabled']:
            if self.start_dhcp_server():
                self.logger.info("✅ DHCP server started")
            else:
                self.logger.error("❌ Failed to start DHCP server")
                success = False
        
        if self.config['dns']['enabled']:
            if self.start_dns_server():
                self.logger.info("✅ DNS server started")
            else:
                self.logger.error("❌ Failed to start DNS server")
                success = False
        
        if success:
            self.log_event('system', f'CasinoNet server started on {server_ip}')
            self.logger.info(f"🎰 CasinoNet ready - Machines can now discover G2S host at {server_ip}")
        
        return success
    
    def start_dhcp_server(self):
        """Start DHCP server thread"""
        try:
            # Try to use system dnsmasq first
            if self.try_system_dnsmasq():
                self.logger.info("Using system dnsmasq for DHCP")
                return True
            
            # Fall back to built-in Python DHCP
            self.logger.info("Using built-in Python DHCP server")
            self.dhcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.dhcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.dhcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            # Bind to DHCP port
            self.dhcp_socket.bind(('', 67))
            
            # Start DHCP thread
            self.dhcp_thread = threading.Thread(target=self.dhcp_server_loop, daemon=True)
            self.dhcp_thread.start()
            
            return True
            
        except PermissionError:
            self.logger.error("❌ Permission denied - run as root or use sudo for DHCP")
            return False
        except Exception as e:
            self.logger.error(f"❌ Failed to start DHCP server: {e}")
            return False
    
    def try_system_dnsmasq(self):
        """Try to configure and use system dnsmasq"""
        try:
            # Check if dnsmasq is available
            result = subprocess.run(['which', 'dnsmasq'], capture_output=True)
            if result.returncode != 0:
                return False
            
            # Generate dnsmasq config
            config_content = self.generate_dnsmasq_config()
            
            # Write temporary config
            dnsmasq_conf = self.data_dir / 'dnsmasq.conf'
            with open(dnsmasq_conf, 'w') as f:
                f.write(config_content)
            
            # Start dnsmasq
            cmd = [
                'dnsmasq',
                '--conf-file=' + str(dnsmasq_conf),
                '--no-daemon',
                '--log-queries',
                '--log-dhcp'
            ]
            
            self.dnsmasq_process = subprocess.Popen(cmd, 
                                                   stdout=subprocess.PIPE,
                                                   stderr=subprocess.PIPE)
            
            # Give it a moment to start
            time.sleep(1)
            
            if self.dnsmasq_process.poll() is None:
                self.logger.info("✅ dnsmasq started successfully")
                return True
            else:
                stderr = self.dnsmasq_process.stderr.read().decode()
                self.logger.warning(f"dnsmasq failed to start: {stderr}")
                return False
                
        except Exception as e:
            self.logger.warning(f"Failed to start dnsmasq: {e}")
            return False
    
    def generate_dnsmasq_config(self):
        """Generate dnsmasq configuration"""
        dhcp_config = self.config['dhcp']
        dns_config = self.config['dns']
        
        config = [
            "# CasinoNet dnsmasq configuration",
            "# Auto-generated - do not edit manually",
            "",
            "# Interface binding",
        ]
        
        if dhcp_config['interface'] != 'auto':
            config.append(f"interface={dhcp_config['interface']}")
        else:
            config.append("bind-interfaces")
        
        config.extend([
            "",
            "# DHCP Configuration",
            f"dhcp-range={dhcp_config['ip_range']['start']},{dhcp_config['ip_range']['end']},{dhcp_config['lease_time']}s",
            f"dhcp-option=option:router,{dhcp_config['ip_range']['gateway']}",
        ])
        
        # DNS servers
        dns_servers = ",".join(dhcp_config['options']['dns_servers'])
        config.append(f"dhcp-option=option:dns-server,{dns_servers}")
        
        # Domain name
        if dhcp_config['options'].get('domain_name'):
            config.append(f"dhcp-option=option:domain-name,{dhcp_config['options']['domain_name']}")
        
        # Static leases
        config.append("\n# Static DHCP leases")
        for mac, ip in dhcp_config['static_leases'].items():
            config.append(f"dhcp-host={mac},{ip}")
        
        # DNS Configuration
        config.extend([
            "",
            "# DNS Configuration",
            f"port={dns_config['port']}"
        ])
        
        # Local hostname mappings
        config.append("\n# Local hostname mappings")
        for hostname, ip in dns_config['hostname_mappings'].items():
            config.append(f"address=/{hostname}/{ip}")
        
        # Upstream DNS
        if dns_config.get('forward_dns'):
            for upstream in dns_config['forward_dns']:
                config.append(f"server={upstream}")
        
        return "\n".join(config)
    
    def start_dns_server(self):
        """Start DNS server thread"""
        if hasattr(self, 'dnsmasq_process') and self.dnsmasq_process:
            # dnsmasq is handling DNS
            return True
        
        try:
            self.dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.dns_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Pin DNS to the slot interface so we never answer queries from the
            # home-LAN side (rogue-DNS guard), mirroring the DHCP server.
            iface = self.config.get('dhcp', {}).get('interface')
            if iface and iface != 'auto':
                SO_BINDTODEVICE = getattr(socket, 'SO_BINDTODEVICE', 25)
                self.dns_socket.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                                           iface.encode() + b'\0')
                self.logger.info(f"DNS pinned to interface {iface}")

            port = self.config['dns']['port']
            self.dns_socket.bind(('', port))
            
            self.dns_thread = threading.Thread(target=self.dns_server_loop, daemon=True)
            self.dns_thread.start()
            
            return True
            
        except PermissionError:
            self.logger.error("❌ Permission denied - run as root or use sudo for DNS port 53")
            return False
        except Exception as e:
            self.logger.error(f"❌ Failed to start DNS server: {e}")
            return False
    
    def dhcp_server_loop(self):
        """Main DHCP server loop"""
        self.logger.info("🔄 DHCP server listening...")
        
        while self.running:
            try:
                data, addr = self.dhcp_socket.recvfrom(1024)
                if len(data) < 240:  # Minimum DHCP packet size
                    continue
                
                # Process DHCP packet
                response = self.process_dhcp_packet(data, addr)
                if response:
                    self.dhcp_socket.sendto(response, ('255.255.255.255', 68))
                    
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"DHCP server error: {e}")
    
    def process_dhcp_packet(self, data, addr):
        """Process incoming DHCP packet"""
        try:
            # Parse DHCP packet
            packet = self.parse_dhcp_packet(data)
            if not packet:
                return None
            
            client_mac = packet['chaddr']
            message_type = packet.get('message_type')
            
            if message_type == 1:  # DHCP Discover
                return self.create_dhcp_offer(packet, client_mac, addr)
            elif message_type == 3:  # DHCP Request
                return self.create_dhcp_ack(packet, client_mac, addr)
                
        except Exception as e:
            self.logger.error(f"Error processing DHCP packet: {e}")
        
        return None
    
    def parse_dhcp_packet(self, data):
        """Parse DHCP packet structure"""
        if len(data) < 240:
            return None
        
        # Basic DHCP header parsing
        packet = {
            'op': data[0],
            'htype': data[1],
            'hlen': data[2],
            'hops': data[3],
            'xid': struct.unpack('!I', data[4:8])[0],
            'secs': struct.unpack('!H', data[8:10])[0],
            'flags': struct.unpack('!H', data[10:12])[0],
            'ciaddr': socket.inet_ntoa(data[12:16]),
            'yiaddr': socket.inet_ntoa(data[16:20]),
            'siaddr': socket.inet_ntoa(data[20:24]),
            'giaddr': socket.inet_ntoa(data[24:28]),
            'chaddr': ':'.join(f'{b:02x}' for b in data[28:34])
        }
        
        # Parse options
        options_start = 236
        if data[options_start:options_start+4] != b'\x63\x82\x53\x63':  # Magic cookie
            return packet
        
        i = options_start + 4
        while i < len(data) - 1:
            option_type = data[i]
            if option_type == 255:  # End option
                break
            if option_type == 0:  # Pad option
                i += 1
                continue
            
            option_length = data[i + 1]
            option_data = data[i + 2:i + 2 + option_length]
            
            if option_type == 53:  # DHCP Message Type
                packet['message_type'] = option_data[0]
            elif option_type == 50:  # Requested IP Address
                packet['requested_ip'] = socket.inet_ntoa(option_data)
            
            i += 2 + option_length
        
        return packet
    
    def create_dhcp_offer(self, request, client_mac, addr):
        """Create DHCP Offer response"""
        # Determine IP to offer
        offered_ip = self.get_available_ip(client_mac)
        if not offered_ip:
            return None
        
        # Log the offer
        self.log_event('dhcp', f'DHCP Offer sent to {client_mac}', {
            'mac': client_mac,
            'offered_ip': offered_ip,
            'client_addr': addr[0]
        })
        
        return self.build_dhcp_response(request, offered_ip, 2)  # DHCP Offer
    
    def create_dhcp_ack(self, request, client_mac, addr):
        """Create DHCP ACK response"""
        requested_ip = request.get('requested_ip')
        
        # Validate and assign IP
        if self.validate_ip_assignment(client_mac, requested_ip):
            # Create lease
            lease_time = self.config['dhcp']['lease_time']
            self.dhcp_leases[client_mac] = {
                'ip': requested_ip,
                'expires': time.time() + lease_time,
                'hostname': f'slot-{client_mac.replace(":", "")[-6:]}'
            }
            self.save_leases()
            
            # Log the assignment
            self.log_event('dhcp', f'IP assigned: {requested_ip} to {client_mac}', {
                'mac': client_mac,
                'ip': requested_ip,
                'lease_expires': self.dhcp_leases[client_mac]['expires']
            })
            
            self.logger.info(f"🎰 Slot machine connected: {client_mac} -> {requested_ip}")
            
            return self.build_dhcp_response(request, requested_ip, 5)  # DHCP ACK
        
        return None
    
    def get_available_ip(self, client_mac):
        """Get an available IP for assignment"""
        # Check for existing lease
        if client_mac in self.dhcp_leases:
            lease = self.dhcp_leases[client_mac]
            if lease['expires'] > time.time():
                return lease['ip']
        
        # Check for static lease
        if client_mac in self.config['dhcp']['static_leases']:
            return self.config['dhcp']['static_leases'][client_mac]
        
        # Find available IP in range
        start_ip = ipaddress.IPv4Address(self.config['dhcp']['ip_range']['start'])
        end_ip = ipaddress.IPv4Address(self.config['dhcp']['ip_range']['end'])
        
        assigned_ips = {lease['ip'] for lease in self.dhcp_leases.values() 
                       if lease['expires'] > time.time()}
        
        for ip_int in range(int(start_ip), int(end_ip) + 1):
            ip = str(ipaddress.IPv4Address(ip_int))
            if ip not in assigned_ips:
                return ip
        
        return None
    
    def validate_ip_assignment(self, client_mac, requested_ip):
        """Validate if IP can be assigned to client"""
        if not requested_ip:
            return False
        
        # Check if IP is in our range
        start_ip = ipaddress.IPv4Address(self.config['dhcp']['ip_range']['start'])
        end_ip = ipaddress.IPv4Address(self.config['dhcp']['ip_range']['end'])
        req_ip = ipaddress.IPv4Address(requested_ip)
        
        if not (start_ip <= req_ip <= end_ip):
            return False
        
        # Check if IP is available
        for mac, lease in self.dhcp_leases.items():
            if lease['ip'] == requested_ip and mac != client_mac:
                if lease['expires'] > time.time():
                    return False
        
        return True
    
    def build_dhcp_response(self, request, offered_ip, message_type):
        """Build DHCP response packet"""
        response = bytearray(300)  # Standard DHCP packet size
        
        # DHCP header
        response[0] = 2  # Boot Reply
        response[1] = request['htype']
        response[2] = request['hlen']
        response[3] = 0  # hops
        
        # Transaction ID
        struct.pack_into('!I', response, 4, request['xid'])
        
        # Times
        response[8:10] = b'\x00\x00'  # secs
        response[10:12] = b'\x00\x00'  # flags
        
        # Addresses
        response[12:16] = socket.inet_aton('0.0.0.0')  # ciaddr
        response[16:20] = socket.inet_aton(offered_ip)  # yiaddr (offered IP)
        response[20:24] = socket.inet_aton(self.get_server_ip())  # siaddr (server IP)
        response[24:28] = b'\x00\x00\x00\x00'  # giaddr
        
        # Client hardware address
        mac_bytes = bytes.fromhex(request['chaddr'].replace(':', ''))
        response[28:28+len(mac_bytes)] = mac_bytes
        
        # Magic cookie
        response[236:240] = b'\x63\x82\x53\x63'
        
        # Options
        options_start = 240
        offset = options_start
        
        # DHCP Message Type
        response[offset:offset+3] = bytes([53, 1, message_type])
        offset += 3
        
        # Server Identifier
        server_ip_bytes = socket.inet_aton(self.get_server_ip())
        response[offset:offset+2] = bytes([54, 4])
        response[offset+2:offset+6] = server_ip_bytes
        offset += 6
        
        # Lease Time
        lease_time = self.config['dhcp']['lease_time']
        response[offset:offset+2] = bytes([51, 4])
        struct.pack_into('!I', response, offset+2, lease_time)
        offset += 6
        
        # Subnet Mask
        subnet = ipaddress.IPv4Network(self.config['dhcp']['ip_range']['subnet'])
        response[offset:offset+2] = bytes([1, 4])
        response[offset+2:offset+6] = subnet.netmask.packed
        offset += 6
        
        # Router
        response[offset:offset+2] = bytes([3, 4])
        response[offset+2:offset+6] = socket.inet_aton(self.config['dhcp']['ip_range']['gateway'])
        offset += 6
        
        # DNS Servers
        dns_servers = self.config['dhcp']['options']['dns_servers']
        if dns_servers:
            dns_bytes = b''.join(socket.inet_aton(dns) for dns in dns_servers[:3])  # Max 3
            response[offset:offset+2] = bytes([6, len(dns_bytes)])
            response[offset+2:offset+2+len(dns_bytes)] = dns_bytes
            offset += 2 + len(dns_bytes)
        
        # End option
        response[offset] = 255
        
        return bytes(response[:offset+1])
    
    def dns_server_loop(self):
        """Main DNS server loop"""
        self.logger.info("🔄 DNS server listening...")
        
        while self.running:
            try:
                data, addr = self.dns_socket.recvfrom(512)
                response = self.process_dns_query(data, addr)
                if response:
                    self.dns_socket.sendto(response, addr)
                    
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"DNS server error: {e}")
    
    def process_dns_query(self, data, addr):
        """Process DNS query"""
        try:
            # Parse DNS query
            query = self.parse_dns_query(data)
            if not query:
                return None
            
            hostname = query['hostname'].lower()
            
            # Check local mappings
            if hostname in self.config['dns']['hostname_mappings']:
                response_ip = self.config['dns']['hostname_mappings'][hostname]
                
                # Log DNS query
                self.log_event('dns', f'DNS query resolved: {hostname} -> {response_ip}', {
                    'hostname': hostname,
                    'response_ip': response_ip,
                    'client_ip': addr[0]
                })
                
                return self.build_dns_response(data, query, response_ip)
            
            # Forward to upstream DNS if configured
            if self.config['dns'].get('forward_dns'):
                return self.forward_dns_query(data, hostname, addr)
            
        except Exception as e:
            self.logger.error(f"Error processing DNS query: {e}")
        
        return None
    
    def parse_dns_query(self, data):
        """Parse DNS query packet"""
        if len(data) < 12:
            return None
        
        # DNS header
        query_id = struct.unpack('!H', data[0:2])[0]
        flags = struct.unpack('!H', data[2:4])[0]
        
        # Check if it's a query
        if flags & 0x8000:  # QR bit set (response)
            return None
        
        questions = struct.unpack('!H', data[4:6])[0]
        if questions != 1:
            return None
        
        # Parse question
        offset = 12
        hostname_parts = []
        
        while offset < len(data):
            length = data[offset]
            if length == 0:
                offset += 1
                break
            
            hostname_parts.append(data[offset+1:offset+1+length].decode('utf-8'))
            offset += 1 + length
        
        if offset + 4 > len(data):
            return None
        
        hostname = '.'.join(hostname_parts)
        qtype = struct.unpack('!H', data[offset:offset+2])[0]
        qclass = struct.unpack('!H', data[offset+2:offset+4])[0]
        
        return {
            'id': query_id,
            'hostname': hostname,
            'qtype': qtype,
            'qclass': qclass
        }
    
    def build_dns_response(self, original_query, query, response_ip):
        """Build DNS response packet"""
        response = bytearray(original_query)
        
        # Set response flag
        flags = struct.unpack('!H', response[2:4])[0]
        flags |= 0x8000  # QR bit (response)
        flags |= 0x0400  # AA bit (authoritative)
        struct.pack_into('!H', response, 2, flags)
        
        # Set answer count
        struct.pack_into('!H', response, 6, 1)
        
        # Add answer section
        # Compression pointer to question name
        response.extend(b'\xc0\x0c')
        
        # Type (A record), Class (IN)
        response.extend(struct.pack('!HH', 1, 1))
        
        # TTL (1 hour)
        response.extend(struct.pack('!I', 3600))
        
        # Data length (4 bytes for IPv4)
        response.extend(struct.pack('!H', 4))
        
        # IP address
        response.extend(socket.inet_aton(response_ip))
        
        return bytes(response)
    
    def forward_dns_query(self, data, hostname, client_addr):
        """Forward DNS query to upstream servers"""
        for upstream in self.config['dns']['forward_dns']:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(2)
                sock.sendto(data, (upstream, 53))
                response, _ = sock.recvfrom(512)
                sock.close()
                
                self.log_event('dns', f'DNS query forwarded: {hostname} via {upstream}', {
                    'hostname': hostname,
                    'upstream': upstream,
                    'client_ip': client_addr[0]
                })
                
                return response
                
            except Exception as e:
                continue
        
        return None
    
    def stop(self):
        """Stop DNS and DHCP servers"""
        if not self.running:
            return
        
        self.logger.info("🛑 Stopping CasinoNet DNS/DHCP Server")
        self.running = False
        
        # Stop dnsmasq if running
        if hasattr(self, 'dnsmasq_process') and self.dnsmasq_process:
            try:
                self.dnsmasq_process.terminate()
                self.dnsmasq_process.wait(timeout=5)
            except:
                self.dnsmasq_process.kill()
        
        # Close sockets
        if self.dhcp_socket:
            self.dhcp_socket.close()
        if self.dns_socket:
            self.dns_socket.close()
        
        self.log_event('system', 'CasinoNet server stopped')
        self.logger.info("✅ CasinoNet DNS/DHCP Server stopped")
    
    def get_status(self):
        """Get server status for API endpoint"""
        status = {
            'running': self.running,
            'server_ip': self.get_server_ip(),
            'services': {
                'dhcp': {
                    'enabled': self.config['dhcp']['enabled'],
                    'running': self.dhcp_thread and self.dhcp_thread.is_alive() if self.dhcp_thread else False,
                    'active_leases': len([l for l in self.dhcp_leases.values() if l['expires'] > time.time()]),
                    'ip_range': f"{self.config['dhcp']['ip_range']['start']} - {self.config['dhcp']['ip_range']['end']}"
                },
                'dns': {
                    'enabled': self.config['dns']['enabled'],
                    'running': self.dns_thread and self.dns_thread.is_alive() if self.dns_thread else False,
                    'hostname_mappings': len(self.config['dns']['hostname_mappings'])
                }
            },
            'leases': [
                {
                    'mac': mac,
                    'ip': lease['ip'],
                    'hostname': lease.get('hostname', 'unknown'),
                    'expires': lease['expires'],
                    'expires_in': max(0, lease['expires'] - time.time())
                }
                for mac, lease in self.dhcp_leases.items()
                if lease['expires'] > time.time()
            ]
        }
        
        return status


def main():
    """Main function for standalone execution"""
    import argparse
    
    parser = argparse.ArgumentParser(description='CasinoNet DNS/DHCP Server')
    parser.add_argument('--config', default='config/network_config.json',
                       help='Configuration file path')
    parser.add_argument('--data-dir', default='data',
                       help='Data directory path')
    parser.add_argument('--daemon', action='store_true',
                       help='Run as daemon')
    
    args = parser.parse_args()
    
    server = CasinoNetDHCPDNSServer(args.config, args.data_dir)
    
    try:
        if server.start():
            print("🎰 CasinoNet DNS/DHCP Server started successfully")
            print(f"🌐 Server IP: {server.get_server_ip()}")
            print("🎮 Slot machines can now discover G2S host automatically")
            print("\nPress Ctrl+C to stop")
            
            while True:
                time.sleep(1)
        else:
            print("❌ Failed to start server")
            return 1
            
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    except Exception as e:
        print(f"❌ Server error: {e}")
        return 1
    finally:
        server.stop()
    
    return 0


if __name__ == '__main__':
    exit(main())
