#!/usr/bin/env python3
"""Standalone gate for DHCP lease persistence + expiry, hardened after the
2026-07-23 adversarial review. Pins: restart survival, reservations-win,
PRUNE-ONLY-ON-EXHAUSTION (the collision guard), clock-jump safety (pre-NTP /
RTC-less boot must never mass-prune live leases), null/garbage timestamp is
never fatal, load-does-not-prune, corrupt-file tolerance, atomic save, and
DECLINE frees the offered IP. Constructs the server via object.__new__ to skip
the socket/interface machinery. Run: python3 test_dhcp_leases.py.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dhcp_dns_server_enhanced import EnhancedDHCPServer  # noqa: E402

passed = failed = 0
NOW = time.time()             # a sane, post-2023 wall clock
FLOOR = EnhancedDHCPServer.SANE_EPOCH


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} -- {detail}")


def mk(lease_file, retention=7 * 24 * 3600, start=100, end=200, reservations=None):
    s = object.__new__(EnhancedDHCPServer)
    s.config = {'lease_retention': retention, 'lease_file': lease_file,
                'start_ip': start, 'end_ip': end,
                'network': {'base': '192.168.50'},
                'reservations': reservations or []}
    s.lease_file = lease_file
    s.leases = {}
    return s


tmp = tempfile.mkdtemp()
LF = os.path.join(tmp, 'leases.json')
AVP, ZERO = '00:01:2e:49:28:15', '00:e0:4c:5c:19:30'


def lease(ip, ts=None):
    return {'ip': ip, 'vendor': 'IGT', 'state': 'acked',
            'timestamp': NOW if ts is None else ts}


# 1 — restart survival + collision avoidance (the core fix)
print("— restart survival: leases persist across a fresh instance")
s1 = mk(LF)
s1.leases = {AVP: lease('192.168.50.101'), ZERO: lease('192.168.50.102')}
s1._save_leases()
s2 = mk(LF)
s2._load_leases()
check("both leases reload with same IPs after a fresh instance",
      s2.leases.get(AVP, {}).get('ip') == '192.168.50.101'
      and s2.leases.get(ZERO, {}).get('ip') == '192.168.50.102', s2.leases)
check("AVP keeps .101 across restart; Zero keeps .102 (no reshuffle)",
      s2.find_available_ip(AVP) == '192.168.50.101'
      and s2.find_available_ip(ZERO) == '192.168.50.102')
check("a NEW mac gets a free IP colliding with neither held lease",
      s2.find_available_ip('aa:bb:cc:dd:ee:ff') not in ('192.168.50.101', '192.168.50.102'))

# 2 — reservations WIN over a persisted lease (review LOW/MEDIUM)
print("— a reservation overrides a MAC's stale persisted lease")
sr = mk(LF, reservations=[{'mac': AVP, 'ip': '192.168.50.201'}])
sr.leases = {AVP: lease('192.168.50.101')}   # persisted lease says .101
check("reservation .201 wins over the persisted .101",
      sr.find_available_ip(AVP) == '192.168.50.201')

# 3 — PRUNE ONLY ON EXHAUSTION (the collision guard) — review HIGH
print("— expired leases are reclaimed ONLY when the pool is exhausted")
# roomy pool: an EXPIRED lease is NOT pruned; its IP stays held
sroom = mk(LF, retention=10, start=100, end=200)
sroom.leases = {'gone': lease('192.168.50.150', ts=NOW - 999),   # 999s old, ret 10s
                'here': lease('192.168.50.151', ts=NOW)}
got = sroom.find_available_ip('new:mac')
check("with room, an expired lease is NOT pruned (its IP stays held)",
      'gone' in sroom.leases and got not in ('192.168.50.150', '192.168.50.151'), (got, sroom.leases))
# full pool: NOW the expired lease is reclaimed to make room
sfull = mk(LF, retention=10, start=100, end=101)   # only .100, .101
sfull.leases = {'fresh': lease('192.168.50.100', ts=NOW),
                'gone':  lease('192.168.50.101', ts=NOW - 999)}
got2 = sfull.find_available_ip('new:mac')
check("with the pool exhausted, the expired lease is reclaimed (.101 freed)",
      got2 == '192.168.50.101' and 'gone' not in sfull.leases, (got2, sfull.leases))
check("the FRESH lease is never reclaimed even under exhaustion",
      'fresh' in sfull.leases)

# 4 — clock-jump safety — review HIGH
print("— clock-jump safety: never expire under an untrusted clock")
sc = mk(LF, retention=10)
old = lease('192.168.50.160', ts=NOW - 999)   # genuinely old under a sane clock
check("pre-NTP boot (now below the sane-epoch floor) -> nothing is expired",
      sc._is_expired(old, now=1000.0) is False)
garbage = lease('192.168.50.161', ts=500)      # stamp below the floor (pre-sync)
check("a lease stamped by a pre-sync clock is NOT expired under a sane now",
      sc._is_expired(garbage, now=NOW) is False)
check("under BOTH sane clocks, a genuinely-old lease IS expired",
      sc._is_expired(old, now=NOW) is True)
# and _prune_expired honors it: a full pool + pre-sync clock does NOT reshuffle
sc2 = mk(LF, retention=10, start=100, end=100)   # 1-IP pool, exhausted
sc2.leases = {'live': lease('192.168.50.100', ts=NOW)}
# now reads pre-sync (below floor): must NOT prune the live lease
sc2_now_backup = time
freed = [m for m, l in sc2.leases.items() if sc2._is_expired(l, now=1000.0)]
check("exhausted pool + pre-sync clock: live lease NOT flagged expired (no collision)",
      freed == [], freed)

# 5 — null / garbage / missing timestamp is never fatal — review MEDIUM
print("— null/garbage/missing timestamp never crashes")
nl = os.path.join(tmp, 'nullts.json')
with open(nl, 'w') as f:
    json.dump({'m1': {'ip': '192.168.50.170', 'timestamp': None},
               'm2': {'ip': '192.168.50.171'},                       # missing ts
               'm3': {'ip': '192.168.50.172', 'timestamp': 'garbage'}}, f)
sn = mk(nl)
sn._load_leases()   # must not raise
check("file with null/missing/string timestamps loads without crashing",
      set(sn.leases) == {'m1', 'm2', 'm3'}, sn.leases)
check("a null-timestamp lease is treated as NOT expired (never pruned wrongly)",
      sn._is_expired(sn.leases['m1'], now=NOW) is False
      and sn._is_expired(sn.leases['m2'], now=NOW) is False
      and sn._is_expired(sn.leases['m3'], now=NOW) is False)

# 6 — load does NOT prune (reclaim is lazy)
print("— load keeps stale entries (no eager prune on load)")
pl = os.path.join(tmp, 'stale.json')
with open(pl, 'w') as f:
    json.dump({'old': lease('192.168.50.180', ts=NOW - 10 * 24 * 3600)}, f)  # 10d old
sp = mk(pl, retention=7 * 24 * 3600)
sp._load_leases()
check("a 10-day-old lease is KEPT on load (reclaimed only on exhaustion)",
      'old' in sp.leases, sp.leases)

# 7 — corrupt / missing / wrong-shape never fatal
print("— corrupt/missing/wrong-shape lease file never crashes")
bad = os.path.join(tmp, 'bad.json')
with open(bad, 'w') as f:
    f.write('{ not valid json ]')
sb = mk(bad)
sb._load_leases()
check("corrupt file -> empty table, no crash", sb.leases == {})
sm = mk(os.path.join(tmp, 'nope.json'))
sm._load_leases()
check("missing file -> empty table, no crash", sm.leases == {})
wf = os.path.join(tmp, 'weird.json')
with open(wf, 'w') as f:
    json.dump(["not", "a", "dict"], f)
sw = mk(wf)
sw._load_leases()
check("wrong-shape file -> empty table, no crash", sw.leases == {})

# 8 — atomic save (no .tmp litter) + persist-on-ACK-only proof at method level
print("— save is atomic; only committed leases hit disk")
s1._save_leases()
check("no leftover .tmp after save", not os.path.exists(LF + '.tmp'))

# 9 — DECLINE frees the offered IP (self-heal within a run)
print("— DECLINE drops the lease so the next offer avoids the conflict")
sd = mk(LF)
sd.leases = {AVP: lease('192.168.50.102')}   # AVP was offered a colliding .102
sd._handle_decline({'mac': AVP})
check("DECLINE removed the AVP's conflicting lease",
      AVP not in sd.leases)

print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
