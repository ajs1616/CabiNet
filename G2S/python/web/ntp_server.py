#!/usr/bin/env python3
"""
Simple NTP Server for CasinoNet
Provides time synchronization for slot machines
"""
import socket
import struct
import time
import threading
import logging

class NTPServer:
    def __init__(self, port=123, interface=None):
        self.port = port
        self.interface = interface
        self.running = False
        self.socket = None
        
        # Setup logging
        self.logger = logging.getLogger('NTP')
        self.logger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('[%(name)s] %(message)s'))
        self.logger.addHandler(ch)
        
        # NTP epoch is January 1, 1900
        self.NTP_DELTA = 2208988800  # seconds between 1900 and 1970
        
    def start(self):
        """Start NTP server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Pin to the slot interface (eth0) — slot-net only, matching the
            # DHCP/DNS/TFTP servers.
            if self.interface and self.interface != 'auto':
                SO_BINDTODEVICE = getattr(socket, 'SO_BINDTODEVICE', 25)
                self.socket.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                                       self.interface.encode() + b'\0')
                self.logger.info(f"Pinned to interface {self.interface}")
            self.socket.bind(('0.0.0.0', self.port))
            self.running = True
            
            self.logger.info(f"Server started on port {self.port}")
            
            while self.running:
                try:
                    data, addr = self.socket.recvfrom(1024)
                    if len(data) >= 48:  # Minimum NTP packet size
                        self.logger.info(f"NTP request from {addr[0]}")
                        response = self._create_ntp_response(data)
                        self.socket.sendto(response, addr)
                except Exception as e:
                    if self.running:
                        self.logger.error(f"Error: {e}")
                        
        except PermissionError:
            self.logger.error(f"Permission denied on port {self.port}")
            self.logger.error("NTP requires port 123 which needs root/sudo")
            return False
        except Exception as e:
            self.logger.error(f"Failed to start: {e}")
            return False
            
        return True
    
    def _create_ntp_response(self, request):
        """Create NTP response packet"""
        # Parse request
        unpacked = struct.unpack('!BBBb11I', request[:48])
        
        # Create response
        # LI=0, VN=3, Mode=4 (server)
        byte1 = 0b00011100  # LI=00, VN=011 (v3), Mode=100 (server)
        
        # Stratum 1 = primary reference. Must pair with a 4-char refid like
        # 'LOCL' (below); a stratum >=2 server would instead need its upstream
        # server's IPv4 as refid, so stratum 2 + 'LOCL' is malformed and makes
        # strict clients (the AVP) distrust us. Advertise a clean stratum-1.
        stratum = 1
        
        # Poll interval (same as request)
        poll = unpacked[2]
        
        # Precision (-20 = about 1 microsecond)
        precision = -20
        
        # Get current time
        current_time = time.time() + self.NTP_DELTA
        
        # Timestamps (seconds and fractional seconds)
        timestamp_int = int(current_time)
        timestamp_frac = int((current_time - timestamp_int) * 2**32)
        
        # Build response packet
        response = struct.pack('!BBBb11I',
            byte1,                    # LI, VN, Mode
            stratum,                  # Stratum
            poll,                     # Poll
            precision,                # Precision
            0,                        # Root delay
            0,                        # Root dispersion
            0x4C4F434C,              # Reference ID ('LOCL')
            timestamp_int, timestamp_frac,  # Reference timestamp
            unpacked[13], unpacked[14],     # Origin ts = client's TRANSMIT ts
                                            # (req bytes 40-47); MUST echo this
                                            # or the client rejects the reply
            timestamp_int, timestamp_frac,  # Receive timestamp
            timestamp_int, timestamp_frac   # Transmit timestamp
        )
        
        return response
    
    def stop(self):
        """Stop NTP server"""
        self.running = False
        if self.socket:
            self.socket.close()
        self.logger.info("Server stopped")

# Standalone mode
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="CasinoNet NTP server (slot-net time sync)")
    ap.add_argument("--interface", default=None, help="pin to this NIC (e.g. eth0)")
    a = ap.parse_args()
    server = NTPServer(interface=a.interface)

    print("Starting NTP server...")
    print("This requires sudo to bind to port 123")
    
    try:
        if server.start():
            print("NTP server running. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping NTP server...")
        server.stop()