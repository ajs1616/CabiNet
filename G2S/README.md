# G2S — CabiNet's G2S host for direct-IP EGMs

Home slot-collector hobby project: talks G2S over IP to real iron (live-proven on an
IGT AVP and a WMS BB2E). Cert-less HTTP on port 8081 is the **permanent** path — the
SCEP/SSL era is dead and is not coming back.

## Key files

- `g2s_host.py` — **the** canonical G2S host (stdlib-only, cert-less). Everything else that
  looks like a server in this tree is historical.
- `tools/test_*.py` — the self-contained regression gates (hub store, TITO, machine
  linking, link demotion, companion/RFID); each must end **"N passed, 0 failed"**.
  (`tools/avp_replay.py` is the dev-rig gate — it replays real-AVP wire captures that
  are not included in this distribution, and says so if you run it.)
- `webui/index.html` — the Test Panel web UI, served by the host at `/`.
- `BENCH_BRINGUP.md` — bench runbook. `ROADMAP_G2S.md` — task backlog + current state.
- `start-dhcp-enhanced.py` + `python/web/dhcp_dns_server_enhanced.py` — the DHCP/DNS server
  for the slot VLAN (single-TLV IGT Option 43; see `docs/DHCP_VENDOR_CONFIG.md`).
- `tftp-root/igt/g2s.xml` — canonical TFTP config (http/8081). Root-level `tftp-root/*` files
  are old shotgun-discovery experiments; don't trust them.

## Ground rules

- The sync HTTP reply carries ONLY a SINGLE-wrapped `g2s:g2sResponse` g2sAck (d556ad2) —
  never touch the emission shape. Application traffic is host POSTs to the EGM's
  `egmLocation` (two-channel architecture).
- `g2s_host.py` stays stdlib-only. Cert-less permanently. No ownership/commConfig cycle is
  ever a prerequisite (that theory is disproven).
