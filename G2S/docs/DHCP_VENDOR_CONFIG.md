# DHCP Vendor Configuration Guide

This guide explains how CabiNet's DHCP server provides vendor-specific configuration to slot machines.

> **Authority note (2026-07-02):** the IGT Option-43 format below is the **wire-proven**
> single-TLV payload implemented by `encode_igt_avp_options` in
> `G2S/python/web/dhcp_dns_server_enhanced.py` — that function's docstring is the source of
> truth. An earlier revision of this doc described a multi-suboption payload (host IP + port +
> path + auto-discovery + config URL); that format is **wrong** and live-proven harmful: the
> AVP's parser treats the extra sub-options as malformed, **discards the whole option**, and
> keeps its `127.0.0.1` default. Do NOT "fix" the encoder to match any multi-suboption table.

## Supported Vendors

### IGT Slimeline AVP
- **Vendor Class**: the AVP sends `IGT` (the server matches on the `IGT` prefix and echoes the client's exact vendor class back rather than asserting its own)
- **MAC Prefixes**: `00:12:E4`, `00:01:2E`
- **Boot File (Option 67)**: `igt/g2s.xml` (informational only — the AVP takes its G2S host from Option 43, not TFTP)

### WMS BlueBird 2
- **Vendor Class**: `WMS-BB2`, `WMS-Gaming`, `WMS-BlueBird`
- **MAC Prefixes**: `00:A0:A5`, `00:1B:3F`
- **Config File**: `tftp-root/wms/bluebird.cfg`

## DHCP Options Provided

### Standard Options (All Vendors)

| Option | Name | Value |
|--------|------|-------|
| 1 | Subnet Mask | 255.255.255.0 |
| 3 | Router | DHCP server IP |
| 6 | DNS Servers | DHCP server IP, 8.8.8.8 |
| 15 | Domain Name | casinonet.local |
| 42 | NTP Servers | DHCP server IP |
| 51 | Lease Time | 3600 seconds |
| 54 | DHCP Server | Server IP |

### IGT-Specific Options

| Option | Name | Description |
|--------|------|-------------|
| 43 | Vendor Specific | ONE TLV sub-option — the G2S host IPv4, nothing else (see below) |
| 60 | Vendor Class | echo of the client's own vendor class (deliberately not asserted by the server) |
| 66 | TFTP Server | Server IP |
| 67 | Boot File | igt/g2s.xml |
| 72 | WWW Server | Server IP as a raw 4-byte IPv4 (RFC 2132 — not a URL string) |
| 119 | Domain Search | casinonet.local, g2s.local |

#### IGT Option 43 — wire-proven single-TLV format (2026-07-01, real AVP)

Option 43 for the AVP is **exactly one TLV sub-option and nothing else**:

```
0x01 0x04 <4-byte G2S host IPv4>
e.g. host 192.168.50.2  ->  01 04 C0 A8 32 02
```

- The AVP takes **only the host IP** from DHCP. The scheme, port, and path come from the
  AVP's own persisted URI segments (e.g. `http://` + `<opt-43 IP>` + `:8081/G2S`).
- **Any additional sub-options make the whole payload malformed to the AVP's parser** — it
  discards the entire option and keeps its `127.0.0.1` "host unset" default. This is
  wire-proven, not theoretical: earlier builds that appended port/path/"SOAP"/auto-discovery/
  config-URL sub-options bricked plug-and-play discovery.
- Implementation + authority: `encode_igt_avp_options()` in
  `G2S/python/web/dhcp_dns_server_enhanced.py` (returns `b'\x01\x04' + inet_aton(host)`).
- Plug-and-play requires the AVP side set to Override DHCP Configured Host = **NO**.

### WMS-Specific Options

| Option | Name | Description |
|--------|------|-------------|
| 43 | Vendor Specific | See WMS sub-options below |
| 60 | Vendor Class | WMS-BB2 |
| 66 | TFTP Server | Server IP |
| 67 | Boot File | wms/bluebird.cfg |
| 150 | Cisco TFTP | Server IP (some WMS use this) |

#### WMS Sub-options (Option 43) — ⚠️ UNVERIFIED ON HARDWARE
- **Sub-option 1**: G2S URL (variable, full URL)
- **Sub-option 2**: Auto-register flag (1 byte)
- **Sub-option 3**: Protocol version (variable, "G2S_v1.0.3")
- **Sub-option 4**: Heartbeat interval (2 bytes, seconds)
- **Sub-option 5**: Config download URL (variable)
- **Sub-option 10**: Asset server URL (variable)

This WMS table matches `encode_wms_bluebird_options()` in the same file but has never been
tested against a real BB2E — treat it as a starting guess, not a proven format (the IGT
experience above shows how intolerant EGM option-43 parsers can be).

## Vendor Detection

The DHCP server detects vendors using:

1. **Vendor Class Identifier (Option 60)** - Primary method
2. **MAC Address Prefix** - Fallback method
3. **Hostname** - Additional hint

## Configuration Files

### IGT AVP Configuration (`tftp-root/igt/g2s.xml`)
- XML format configuration (http/8081 — the cert-less permanent path)
- Informational: the AVP's G2S host discovery is Option 43, not this file
- `tftp-root/igt/avp-config.xml` and the root-level `tftp-root/*` files are older
  shotgun-discovery experiments — do not treat them as authoritative

### WMS BlueBird Configuration (`tftp-root/wms/bluebird.cfg`)
- INI-style configuration
- G2S URLs and protocols
- AFT and WAT settings
- Security parameters

## Testing

### Test DHCP Configuration
```bash
# Start the DHCP/DNS server by hand (normally the casinonet-dhcp unit)
sudo python3 G2S/start-dhcp-enhanced.py --interface <slotNIC>
```

### Verify Machine Configuration

1. **Watch the lease log**: `journalctl -u casinonet-dhcp -f` shows each
   DISCOVER/OFFER/ACK and the option-43 payload handed to the machine.

2. **Monitor G2S Connections**:
```bash
tail -f logs/webserver.log | grep "G2S"
```

3. **Check Machine Status**:
Visit http://localhost:8081 to see connected machines

## Troubleshooting

### Machine Not Getting IP
1. Check DHCP is enabled in config
2. Verify network interface
3. Check firewall rules (port 67/68 UDP)

### Wrong Vendor Detection
1. Check vendor class in DHCP request
2. Verify MAC address prefix
3. Update detection rules if needed

### G2S Connection Fails
1. Verify G2S URL in vendor options
2. Check DNS resolution for g2s.local
3. Ensure G2S endpoint is accessible

### TFTP Issues
1. Create tftp-root directory
2. Place config files in vendor subdirs
3. Check file permissions

## Network Architecture

```
Slot Machine                      CabiNet Server
    |                                  |
    |---- DHCP DISCOVER -------------->|
    |     (Vendor Class: IGT-AVP)      |
    |                                  |
    |<--- DHCP OFFER ------------------|
    |     (Option 43: G2S config)      |
    |                                  |
    |---- DHCP REQUEST --------------->|
    |                                  |
    |<--- DHCP ACK --------------------|
    |     (Complete configuration)     |
    |                                  |
    |---- TFTP GET igt/g2s.xml ------->|   (optional/informational)
    |                                  |
    |<--- Config file -----------------|
    |                                  |
    |---- G2S transport version ------>|   (to http://<opt-43 IP>:8081/G2S)
    |---- G2S commsOnLine ------------>|
```

## Advanced Configuration

### Static Reservations
Add to `data/dhcp_config.json`:
```json
{
  "reservations": [
    {
      "mac": "00:12:E4:00:00:01",
      "ip": "192.168.50.50",
      "hostname": "IGT-AVP-001"
    }
  ]
}
```

### Custom Vendor Options
Modify `dhcp_dns_server_enhanced.py` to add new vendors:
```python
'NewVendor': {
    'vendor_class': ['NewVendor-EGM'],
    'mac_prefixes': ['00:11:22'],
    'options': {
        43: self.encode_newvendor_options,
        60: b'NewVendor-EGM'
    }
}
```

## Security Considerations

1. **Restrict DHCP to Known MACs**: Add MAC filtering
2. **Separate VLAN**: Use dedicated gaming VLAN
3. **Monitor DHCP Logs**: Watch for unknown devices
4. **Validate Vendor Class**: Prevent spoofing
5. **Secure TFTP**: Limit access to config files