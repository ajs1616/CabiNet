#!/usr/bin/env python3
"""
Simple TFTP Server for IGT Configuration
"""
import socket
import struct
import os
import threading

class TFTPServer:
    def __init__(self, root_dir='tftp-root', port=69, interface=None):
        self.root_dir = root_dir
        self.port = port
        self.interface = interface
        self.running = False
        self.socket = None

    def start(self):
        """Start TFTP server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Pin to the slot interface (e.g. eth0) so we never serve the
            # home-LAN side, mirroring the DHCP/DNS servers.
            if self.interface and self.interface != 'auto':
                SO_BINDTODEVICE = getattr(socket, 'SO_BINDTODEVICE', 25)
                self.socket.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE,
                                       self.interface.encode() + b'\0')
                print(f"[TFTP] Pinned to interface {self.interface}")
            self.socket.bind(('0.0.0.0', self.port))
            self.running = True
            
            print(f"[TFTP] Server started on port {self.port}")
            print(f"[TFTP] Serving files from: {self.root_dir}")
            
            while self.running:
                try:
                    data, addr = self.socket.recvfrom(1024)
                    threading.Thread(target=self.handle_request, 
                                   args=(data, addr), 
                                   daemon=True).start()
                except Exception as e:
                    if self.running:
                        print(f"[TFTP] Error: {e}")
                        
        except PermissionError:
            print(f"[TFTP] Permission denied on port {self.port}")
            print("[TFTP] TFTP requires port 69 which needs root/sudo")
            return False
        except Exception as e:
            print(f"[TFTP] Failed to start: {e}")
            return False
            
        return True
        
    def handle_request(self, data, addr):
        """Handle TFTP request"""
        print(f"[TFTP] Request from {addr[0]}:{addr[1]} - {len(data)} bytes")
        
        if len(data) < 4:
            return
            
        opcode = struct.unpack('!H', data[:2])[0]
        
        if opcode == 1:  # RRQ (Read Request)
            self.handle_read_request(data[2:], addr)
        elif opcode == 2:  # WRQ (Write Request)
            # We don't support writes
            self.send_error(addr, 2, "Write not supported")
            
    def handle_read_request(self, data, addr):
        """Handle read request"""
        # Extract filename (null-terminated string)
        parts = data.split(b'\x00')
        if len(parts) < 2:
            return
            
        filename = parts[0].decode('utf-8', errors='ignore')
        mode = parts[1].decode('utf-8', errors='ignore') if len(parts) > 1 else 'octet'
        
        print(f"[TFTP] Read request from {addr[0]}: {filename}")
        
        # Security: prevent directory traversal
        # Remove any leading slashes and .. sequences
        filename = filename.lstrip('/')
        filename = filename.replace('../', '')
        filename = filename.replace('..\\', '')
        
        # Build the full path
        filepath = os.path.join(self.root_dir, filename)
        
        # Check if file exists
        if not os.path.exists(filepath):
            print(f"[TFTP] File not found: {filename}")
            self.send_error(addr, 1, "File not found")
            return
            
        # Read and send file
        try:
            with open(filepath, 'rb') as f:
                file_data = f.read()
                
            print(f"[TFTP] Sending {filename} ({len(file_data)} bytes)")
            self.send_file(addr, file_data)
            
        except Exception as e:
            print(f"[TFTP] Error reading file: {e}")
            self.send_error(addr, 0, str(e))
            
    def send_file(self, addr, data):
        """Send file via TFTP"""
        block_num = 1
        block_size = 512
        
        # Create new socket for this transfer
        transfer_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        transfer_socket.settimeout(5.0)
        
        try:
            pos = 0
            while pos < len(data):
                # Get next block
                block = data[pos:pos + block_size]
                
                # Send DATA packet
                packet = struct.pack('!HH', 3, block_num) + block
                transfer_socket.sendto(packet, addr)
                
                # Wait for ACK
                try:
                    ack_data, ack_addr = transfer_socket.recvfrom(1024)
                    if len(ack_data) >= 4:
                        ack_opcode, ack_block = struct.unpack('!HH', ack_data[:4])
                        if ack_opcode == 4 and ack_block == block_num:
                            # ACK received, send next block
                            pos += block_size
                            block_num += 1
                        else:
                            print(f"[TFTP] Unexpected ACK: opcode={ack_opcode}, block={ack_block}")
                            break
                except socket.timeout:
                    print(f"[TFTP] Timeout waiting for ACK")
                    break
                    
                # Last block was less than 512 bytes - we're done
                if len(block) < block_size:
                    print(f"[TFTP] Transfer complete")
                    break
                    
        finally:
            transfer_socket.close()
            
    def send_error(self, addr, error_code, error_msg):
        """Send TFTP error packet"""
        packet = struct.pack('!HH', 5, error_code) + error_msg.encode() + b'\x00'
        self.socket.sendto(packet, addr)
        
    def stop(self):
        """Stop TFTP server"""
        self.running = False
        if self.socket:
            self.socket.close()
            
# Standalone mode
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="CasinoNet TFTP server (slot-net config files)")
    ap.add_argument("root_dir", nargs="?", default="tftp-root")
    ap.add_argument("--interface", default=None, help="pin to this NIC (e.g. eth0); slot-net only")
    a = ap.parse_args()
    server = TFTPServer(a.root_dir, interface=a.interface)

    print("Starting standalone TFTP server...")
    print("This requires sudo to bind to port 69")
    
    try:
        if server.start():
            print("TFTP server running. Press Ctrl+C to stop.")
            while True:
                pass
    except KeyboardInterrupt:
        print("\nStopping TFTP server...")
        server.stop()