#!/bin/bash
#
# Hardware Test Script for Raspberry Pi with RS232 HAT
# Tests serial communication and SAS protocol implementation
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== CasinoNet SAS Hardware Test Suite ==="
echo "Testing on: $(uname -a)"
echo "Date: $(date)"
echo

# Check if running on Raspberry Pi
if [ -f /proc/device-tree/model ]; then
    echo "Raspberry Pi Model: $(cat /proc/device-tree/model)"
else
    echo "Warning: Not running on Raspberry Pi"
fi

# Check for RS232 device
echo
echo "Checking for serial devices..."
ls -la /dev/ttyS* /dev/ttyAMA* /dev/ttyUSB* 2>/dev/null || echo "No standard serial devices found"

# Check serial port permissions
echo
echo "Checking serial port permissions..."
if [ -e /dev/ttyS0 ]; then
    ls -la /dev/ttyS0
    if [ ! -r /dev/ttyS0 ] || [ ! -w /dev/ttyS0 ]; then
        echo "Warning: No read/write access to /dev/ttyS0"
        echo "You may need to run: sudo usermod -a -G dialout $USER"
    fi
fi

# Test Python environment
echo
echo "Testing Python environment..."
python3 --version

# Check required Python packages
echo
echo "Checking Python packages..."
python3 -c "import serial; print(f'pyserial version: {serial.__version__}')" || echo "pyserial not installed"
python3 -c "import asyncio; print('asyncio: OK')" || echo "asyncio not available"
python3 -c "import loguru; print('loguru: OK')" || echo "loguru not installed"

# Run hardware detection
echo
echo "Running hardware detection..."
python3 << EOF
import serial.tools.list_ports

print("Available serial ports:")
for port in serial.tools.list_ports.comports():
    print(f"  {port.device}: {port.description}")
    if port.manufacturer:
        print(f"    Manufacturer: {port.manufacturer}")
    if port.product:
        print(f"    Product: {port.product}")
EOF

# Test serial loopback (if pins 2&3 are connected)
echo
echo "Testing serial loopback (connect TX to RX for this test)..."
read -p "Are TX and RX pins connected for loopback test? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    python3 << EOF
import serial
import time

try:
    ser = serial.Serial('/dev/ttyS0', 19200, timeout=1)
    test_data = b'Hello SAS Test!'
    
    print(f"Sending: {test_data}")
    ser.write(test_data)
    time.sleep(0.1)
    
    received = ser.read(len(test_data))
    print(f"Received: {received}")
    
    if received == test_data:
        print("✓ Loopback test PASSED")
    else:
        print("✗ Loopback test FAILED")
        
    ser.close()
except Exception as e:
    print(f"✗ Loopback test ERROR: {e}")
EOF
fi

# Run unit tests
echo
echo "Running unit tests..."
cd "$PROJECT_DIR"
python3 -m pytest tests/test_sas_protocol.py -v --tb=short || echo "Unit tests failed"

# Live-wire probe (first contact with a real machine)
echo
echo "For first contact with a real machine on the wire, use:"
echo "  python3 tools/sas_bench_poll.py /dev/ttyAMA0 --credits"
echo "(stop casinonet-sas first — the service holds the port)" 

echo
echo "Hardware test completed"
echo "Check log files in current directory for detailed results"