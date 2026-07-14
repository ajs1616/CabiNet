# SAS Protocol Implementation Architecture

## Overview
A modern, fully network-based SAS (Slot Accounting System) implementation where all communication happens through custom Raspberry Pi SMIBs (Slot Machine Interface Boards) over Ethernet (wired-only). No direct serial connections to the server - the SMIBs handle all serial communication with the gaming machines.

## System Architecture

### Network-Only Design
```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│   Gaming Machine    │     │   Gaming Machine    │     │   Gaming Machine    │
│    (IGT/WMS/Bally)  │     │    (IGT/WMS/Bally)  │     │    (IGT/WMS/Bally)  │
└──────────┬──────────┘     └──────────┬──────────┘     └──────────┬──────────┘
           │ RS-232                    │ RS-232                    │ RS-232
           │                           │                           │
┌──────────▼──────────┐     ┌──────────▼──────────┐     ┌──────────▼──────────┐
│   Raspberry Pi      │     │   Raspberry Pi      │     │   Raspberry Pi      │
│      SMIB #1        │     │      SMIB #2        │     │      SMIB #3        │
│  ┌───────────────┐  │     │  ┌───────────────┐  │     │  ┌───────────────┐  │
│  │ RS-232 HAT    │  │     │  │ RS-232 HAT    │  │     │  │ RS-232 HAT    │  │
│  │ Touch Screen  │  │     │  │ Touch Screen  │  │     │  │ Touch Screen  │  │
│  │ Network Stack │  │     │  │ Network Stack │  │     │  │ Network Stack │  │
│  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │
└──────────┬──────────┘     └──────────┬──────────┘     └──────────┬──────────┘
           │                           │                           │
           │ TCP/IP (Ethernet)    │                           │
           │                           │                           │
           └───────────────────────────┴───────────────────────────┘
                                      │
                        ┌─────────────▼─────────────┐
                        │    CabiNet Server         │
                        │  ┌────────────────────┐  │
                        │  │  SAS Protocol Core │  │
                        │  │  AFT/TITO/WAT/HP   │  │
                        │  │  Network Manager    │  │
                        │  │  Database Backend   │  │
                        │  │  Web Interface      │  │
                        │  └────────────────────┘  │
                        └───────────────────────────┘
```

### SMIB (Slot Machine Interface Board) Design

Each Raspberry Pi SMIB acts as a bridge between the gaming machine's serial SAS interface and the network-based server:

#### Hardware Components
- **Raspberry Pi 4B** (or Pi Zero 2 W for cost optimization)
- **RS-232 HAT** with proper voltage levels for gaming machines
- **7" Touch Display** for local control and diagnostics
- **Power Supply** with battery backup
- **Secure enclosure** with tamper detection

#### SMIB Software Stack
```
┌─────────────────────────────────┐
│     Touch UI (Kivy/KivyMD)      │ <- Operator Interface
├─────────────────────────────────┤
│    SMIB Control Application     │ <- Local Logic
├─────────────────────────────────┤
│   SAS Protocol Handler (Local)  │ <- SAS Translation
├─────────────────────────────────┤
│   Network Communication Layer   │ <- Server Connection
├─────────────┬───────────────────┤
│   Serial    │    Network        │
│   Driver    │    Stack          │
└─────────────┴───────────────────┘
```

## Network Communication Protocol

### SMIB ↔ Server Protocol
Since there's no direct serial connection to the server, we use an enhanced protocol:

```json
{
  "message_type": "sas_packet",
  "smib_id": "SMIB-001",
  "machine_id": "IGT-123456",
  "timestamp": "2024-07-28T18:30:00Z",
  "sas_data": {
    "raw": "01 1F 00 00 AB CD",  // Original SAS packet
    "parsed": {
      "address": "01",
      "command": "1F",
      "data": "0000",
      "crc": "ABCD"
    }
  },
  "metadata": {
    "signal_quality": 95,
    "latency_ms": 12,
    "battery_level": 100
  }
}
```

### Communication Modes

1. **Real-time Mode** (WebSocket)
   - Instant event notifications
   - Live meter updates
   - Command execution

2. **Polling Mode** (TCP)
   - Regular meter collection
   - Batch transaction processing
   - Configuration updates

3. **Discovery Mode** (mDNS/UDP)
   - Automatic SMIB discovery
   - Zero-configuration networking
   - Hot-plug support

## Core Features Implementation

### 1. AFT (Automated Funds Transfer)
- SMIB handles local SAS AFT commands
- Server manages account balances
- Transaction queuing for network failures
- Offline mode with reconciliation

### 2. TITO (Ticket In/Ticket Out)
- Ticket validation through server
- Local caching of recent tickets
- QR code display on SMIB screen
- Network ticket database

### 3. WAT (Wide Area Progressive)
- Real-time progressive updates
- Network-wide hit notifications
- SMIB displays current progressive values
- Failover to standalone progressives

### 4. Handpay Management
- Remote authorization from server
- Local override via SMIB touchscreen
- Photo capture for large handpays
- Digital signature collection

### 5. Machine Control
- Server-initiated commands
- Local lockout via SMIB
- Emergency shutdown button on SMIB
- Scheduled enable/disable

### 6. Freeplay Features (via SMIB Touchscreen)
- Credit addition without money
- Time-based freeplay sessions
- Promotional credit management
- Player-specific offers

## SMIB Touch Interface Features

### Main Menu
1. **Machine Status** - Current state, meters, diagnostics
2. **Freeplay Mode** - Enable/disable, add credits
3. **Maintenance** - Diagnostics, logs, configuration
4. **Player Services** - Card in/out, player info
5. **Emergency** - Shutdown, security alert

### Advanced Features
- **Virtual Attendant** - Handle common requests
- **Promotion Engine** - Apply bonuses and offers
- **Diagnostic Mode** - Real-time SAS monitoring
- **Configuration** - Network settings, machine setup

## Security Model

### Network Security
- TLS 1.3 for all SMIB ↔ Server communication
- Certificate-based SMIB authentication
- Encrypted configuration storage
- Secure boot on Raspberry Pi

### Physical Security
- Tamper-evident SMIB enclosure
- Intrusion detection
- Secure mounting inside machine
- Emergency wipe capability

## Database Schema (Server Side)

### Core Tables
- **smibs** - SMIB registration and status
- **machines** - Gaming machine configuration
- **transactions** - All AFT, TITO, handpay records
- **meters** - Real-time and historical meter data
- **events** - Machine and SMIB events
- **progressives** - WAT levels and history
- **freeplay_sessions** - Freeplay tracking

## Advantages of Network-Only Architecture

1. **Simplified Wiring** - Just network cables, no serial runs
2. **Scalability** - Easy to add more machines
3. **Remote Management** - Full control from anywhere
4. **Enhanced Features** - Touch UI enables new capabilities
5. **Better Diagnostics** - Local intelligence in SMIB
6. **Redundancy** - SMIBs can operate offline
7. **Modern Stack** - Standard IT infrastructure

## Development Roadmap

### Phase 1: Core Infrastructure
- Network communication framework
- SMIB ↔ Server protocol
- Basic SAS command routing

### Phase 2: SMIB Development
- Raspberry Pi image creation
- Serial ↔ Network bridge
- Basic touch UI

### Phase 3: Core SAS Features
- AFT implementation
- TITO support
- Basic meter collection

### Phase 4: Advanced Features
- WAT/Progressive support
- Handpay management
- Machine control

### Phase 5: Touch UI Enhancement
- Freeplay system
- Promotional features
- Advanced diagnostics

### Phase 6: Production Readiness
- Security hardening
- Performance optimization
- Deployment tools