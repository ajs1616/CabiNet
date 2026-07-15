"""
hub_store.py — the CasinoNet hub's SQLite spine.

The unified, cross-protocol store the whole floor rides on. Phase 1 (this
file's first slice) is the MACHINE REGISTRY + server-side NICKNAMES: one row
per machine (G2S EGM or SAS SMIB/address), the operator's chosen name, and
first/last-seen. That alone fixes the per-browser-localStorage names problem
— a name set on the phone now shows on the DSI kiosk because the hub, not the
browser, is the source of truth.

Design (matches the settled "SQLite-backed single-node" stack decision):
- stdlib `sqlite3` only, no ORM, no new deps (ARM/SBC-safe).
- WAL journal + synchronous=NORMAL: crash-safe (a power-pull loses at most the
  last uncommitted txn, never corrupts the db) without the fsync-per-write
  cost the Pi's SD card would feel.
- ONE connection, `check_same_thread=False`, guarded by a single re-entrant-
  free Lock. The host is multithreaded (HTTP handlers + per-assoc workers);
  a lone sqlite3 Connection is NOT safe for concurrent use, so every call
  takes the lock. Throughput here is a few ops/sec — a global lock is free.
- Schema migrations are versioned (schema_meta.version); adding a table/column
  later bumps the version and runs forward-only steps. Never destructive.
- Corruption posture mirrors the JSON stores (GR-03), reviewed adversarially:
  the open probe is a real PRAGMA quick_check (SELECT 1 never touched the
  schema pages, so a partially-corrupt db sailed through and _migrate killed
  the host); _migrate itself is inside the net (quarantine + one fresh
  retry); quarantine moves the -wal/-shm sidecars with the db and closes the
  failed connection; and if even a FRESH db cannot be created (SD card gone
  read-only / full — the exact storage state that accompanies corruption)
  the store degrades to an in-memory db so the floor keeps running with
  ephemeral names instead of not running at all. Reads (names/get_nickname/
  machines) are best-effort like touch_machine: a runtime db error degrades
  to empty, never kills /api/status or SAS report ingest.

Phase 2 (2026-07-08): the TITO VALIDATION LEDGER — the cross-machine ticket
authority (`tito_tickets` + `tito_meta`, schema v2). SAS-minted tickets live
HERE (not on the satellite) so a ticket printed on the BB2 can redeem in the
AVP and vice versa; G2S-issued vouchers stay in the g2s host's VoucherStore
and the union is adjudicated by TitoAuthority (g2s_host.py). Amounts are
MILLICENTS (the hub.db money rule). The ledger mirrors the live-proven
semantics of SAS/core/sas_ticket_store.py: minted -> issued -> redeemPending
-> redeemed (+ void), idempotent retries, holder-checked closes, the ISSUED
amount is authoritative. Money methods (tito_*) RAISE on db errors rather
than best-effort-degrade — the HTTP edge catches and answers ok:false so the
satellite takes its safe fallback (local mint / reject-and-return-ticket).

Schema v7's house-hold settlement spine (meter_marks + hold_journal) was
REMOVED from the code 2026-07-15 (collector de-cage: a home game room's Bank
is the settable bankroll minus handouts — machine take-tracking was casino
cage accounting). The v7 DDL stays in _migrate so the version chain and
rerun-safety story remain honest; the tables sit dormant. Historical ledger
refs "hold:<machine>:<rowid>" in account_state.json are money history and
once=True dedupe keys — never clean them. Do not rebuild.

Still future: accounts + ledger (migrate the WAT AccountStore/WatStore),
events (leaderboard history).
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time

log = logging.getLogger("g2s.hub_store")

SCHEMA_VERSION = 12

# Hard bounds — /api/names is unauthenticated (anyone on the floor VLAN/AP),
# so the registry must stay bounded: without these, one hostile poster loops
# random machineKeys and grows hub.db (SD-card fill) AND the /api/status
# "names" payload the UI polls every 2 s (live-probed: one 1 MB key took
# /api/status from ~300 B to 558 KB). A home floor is a few dozen machines.
MAX_MACHINES = 128
MAX_KEY_LEN = 128

# R2: per-machine display denomination, CENTS per credit (operator-set in
# the nickname modal; DISPLAY-ONLY — never used in money math). Matches
# the UI/endpoint bound.
MAX_DENOM_CENTS = 10_000

# v5: host-wide options shelf (host_settings). WRITES are whitelisted to
# known keys — /api/settings is unauthenticated on the floor AP, so the
# shelf must never become an arbitrary K/V dump — and bounded like the
# machine registry. First tenant: 'sysval_fallback' ('on'|'off'; an ABSENT
# row reads as 'on' — the pre-v5 behavior).
# Ticket-header tenants (C1, no schema bump — host_settings is k/v): the
# casino/property name + address/tagline lines + cash-ticket title pushed
# to EVERY machine's printed tickets (G2S voucherTextFields + SAS 0x7D).
# ABSENT = unset — machines keep their machine-side text; the hub NEVER
# pushes empty strings over existing on-glass config, so an empty write
# DELETES the row (see set_host_setting). 'ticket_rev' is the save
# counter the SAS satellites compare against their last-applied rev.
# 'house_allow_negative' ('on'|'off', absent = on): the collector "can my
# bank go into the red?" switch. ON (default) = the bank always covers a
# friend card (never refuses, shown neutrally); OFF = the bank floors at $0
# and funding past the bankroll is refused with a friendly "top up" nudge.
# (v7's 'house_hold' + 'pnl_baseline_mc' tenants were RETIRED with the
# take-tracking removal 2026-07-15 — stale rows in an existing hub.db are
# simply never read again.)
HOST_SETTING_KEYS = ("sysval_fallback", "house_allow_negative",
                     "ticket_prop_name", "ticket_line1", "ticket_line2",
                     "ticket_title_cash", "ticket_rev",
                     # printed-ticket expiration in DAYS, 0/absent = never
                     # (the collector-right default). One value drives both
                     # protocols: the SAS 0x7D expiration byte (0..255) and
                     # the G2S voucher options G2S_expireCashPromo/NonCash.
                     "ticket_expire_days",
                     # the collector's GAMEROOM name (Settings tab) painted in
                     # lights on the player-facing glass/kiosk; absent/"" =
                     # neutral fallback. CabiNet is the product; this is theirs.
                     "gameroom_name")
TICKET_TEXT_KEYS = ("ticket_prop_name", "ticket_line1", "ticket_line2",
                    "ticket_title_cash")
MAX_TICKET_FIELD_LEN = 64
MAX_SETTING_KEY_LEN = 64
MAX_SETTING_VAL_LEN = 256

# v6: the game-name override layer (the games modal's ✎ button). Keys are
# the catalog keys the UI's prettyTheme consults (6-digit family + 3-char
# game code, e.g. '014001H18'); titles override webui/igt_games.json on
# every device. /api/gamenames is unauthenticated on the floor AP, so
# key/title are bounded and the table is capped like the machine registry
# (the shipped catalog is 896 titles — 512 hand-typed overrides is
# generous headroom, not a real limit anyone hits).
MAX_GAME_NAMES = 512
MAX_GAME_KEY_LEN = 64
MAX_GAME_TITLE_LEN = 80

# -- TITO ledger bounds (phase 2) --------------------------------------------
# Mint/authorize ceiling in MILLICENTS — the 5-byte-BCD wire max
# ($99,999,999.99), same ceiling as the SAS TicketStore's
# DEFAULT_MAX_TICKET_CENTS (9_999_999_999 c), in hub.db units. This is
# protocol physics (can't BCD-encode more), NOT a spend policy — a home
# game room bets whatever it likes (collector de-cage, 2026-07-15; the old
# $100k value refused a real $168,250 jackpot to paper).
MAX_TICKET_MILLICENTS = 9_999_999_999 * 1000
_SEQ_DIGITS = 14                 # vn16 = [2-digit systemId][14-digit seq]
MINTED_TTL_SEC = 48 * 3600       # unprinted mints pruned after this
# /api/tito/mint is unauthenticated on the floor AP (same posture as
# /api/names): without a cap, a hostile poster varying (origin, amount)
# defeats the reuse dedupe and grows hub.db one row per POST for 48h.
# A real floor has a handful of machines with at most one open cash-out
# each — 256 open mints is decades of headroom.
MAX_OPEN_MINTS = 256
# Total-row cap (issued/redeemed included): /api/tito/issued and /sync are
# unauthenticated, so the ledger must stay bounded like the machine
# registry. At the cap the oldest REDEEMED rows (paper already consumed)
# are pruned to make room; if nothing is prunable, new inserts refuse.
MAX_TICKETS = 10_000
# Expired-ticket reaper (v12): an ISSUED row whose per-row expire_days
# snapshot (stamped from the ticket_expire_days setting AT ISSUE TIME —
# what the paper actually printed) has passed gets reaped GRACE days later.
# NULL/0 expire_days = the paper printed no expiration = immortal (the
# collector-right default). The grace window keeps the ledger honoring a
# just-expired paper a while longer than the machines do (clock skew,
# drawer-found tickets) before the row — the drawer-ticket class that
# would otherwise fill MAX_TICKETS with unprunable rows over the years —
# is finally let go.
EXPIRE_REAP_GRACE_DAYS = 30
# A 'minted' row is only reused for the 0x57 re-fire of the SAME cash-out —
# seconds apart. Past this window a matching mint is STALE (its issued-push
# may simply not have landed yet) and reusing it could put one validation
# number on two different papers.
MINT_REUSE_TTL_SEC = 600
# /api/tito/sync floors the mint sequence to the satellite's reported seq.
# Cap the accepted floor: one hostile POST with seq=10^14-1 would otherwise
# exhaust the 14-digit mint space permanently. A billion is decades of
# collector cash-outs.
MAX_SYNC_SEQ = 10 ** 9
MAX_SYNC_TICKETS = 500           # /api/tito/sync bound (satellite ledger cap)
MAX_SYNC_CLOSES = 100
# State rank for the never-downgrade sync merge (terminal states win; equal
# rank keeps the hub's row).
_TITO_STATE_RANK = {"minted": 0, "issued": 1, "redeemPending": 2,
                    "redeemed": 3, "void": 3}

# (v7 house-hold settlement bounds REMOVED 2026-07-15 — collector de-cage;
# the v7 tables remain in _migrate, dormant.)

# -- fob registry bounds (v8) -------------------------------------------------
# RFID fobs/player cards (the Companion tap path). uid is the tag's UID as
# CANONICAL hex (PN532 read, e.g. '6CB16F06' — see _norm_fob_uid). tier gates
# what a tap DOES (player/attendant/manager = carded session on the machine's
# glass; reset = SAS handpay reset), mirrored by the table's CHECK constraint
# so even a hand-edited row stays honest. /api/fobs is unauthenticated on the
# floor AP (the /api/names posture), so uid/label/account_id are bounded and
# the table is capped — a home floor is a handful of cards, 256 is headroom.
MAX_FOBS = 256
MAX_FOB_UID_LEN = 32
MAX_FOB_LABEL_LEN = 64
FOB_TIERS = ("player", "attendant", "manager", "reset")
_FOB_UID_RE = re.compile(r"^[0-9A-F]{2,32}$")

# v11: the satellite-assignment registry (zero-config onboarding). A Companion
# (or SAS SMIB) self-IDs by a stable device id (default = Pi serial, no operator
# input) and shows up unassigned; the operator assigns it to a machine FROM THE
# UI, and the hub — not the unit's ExecStart flags — becomes the authority for
# the binding. sat_id is the reported companionId/smibId (normalized). The
# binding columns (egm_id / sas_smib / sas_address) override what the satellite
# reports; label is the friendly name the operator types (the device's display
# name before assignment is just its serial tail). /api/settings is
# unauthenticated on the floor AP (the /api/names posture), so id/label are
# bounded and the table is capped — a home floor is a handful of satellites.
MAX_SATELLITES = 64
MAX_SAT_ID_LEN = 64
MAX_SAT_LABEL_LEN = 64
SAT_KINDS = ("companion", "sas")
_SAT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _now_iso():
    # Naive LOCAL time (no offset), matching SAS/core/sas_ticket_store's
    # _now_iso — the tito ledger's timestamps stay in one dialect. NOTE: the
    # voucher store stamps UTC-with-'Z'; the Cage UI merges both by Date.parse,
    # which reads an offset-less stamp as the VIEWER's local time. That is
    # correct here because the kiosk runs ON the Pi and any phone viewer shares
    # the host's timezone (one physical room) — a cross-timezone viewer would
    # see hub vs voucher rows interleave slightly off. Colocated by design, so
    # not worth a UTC migration of existing naive rows.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _norm_fob_uid(uid):
    """Canonicalize a fob UID: strip ':', '-' and spaces, uppercase, and
    require 2..32 hex characters — returns the canonical string or None.
    EVERY fob path (methods here + the Companion tap ingest in g2s_host)
    normalizes through this one function, so '6c:b1:6f:06', '6C-B1-6F-06'
    and '6CB16F06' are all the SAME card."""
    if not isinstance(uid, str):
        return None
    clean = (uid.replace(":", "").replace("-", "")
                .replace(" ", "").upper())
    return clean if _FOB_UID_RE.match(clean) else None


def _norm_sat_id(sat_id):
    """Canonicalize a satellite device id (companionId / smibId): strip
    surrounding space, require 1..64 of the id charset [A-Za-z0-9._:-].
    Returns the canonical string or None. The satellite self-reports this
    every cycle (default = 'companion-<serial-tail>'); every satellites
    method + the g2s_host resolve path normalizes through here so the id on
    the wire and the id in the table are the same string."""
    if not isinstance(sat_id, str):
        return None
    clean = sat_id.strip()
    return clean if _SAT_ID_RE.match(clean) else None


def _int_or_none(v):
    """Coerce to int, else None (None / bool / non-numeric). AFT report
    fields are attacker-typed at the /api/sas/report edge before the g2s
    clamp — a stray type must degrade to NULL, never raise into ingest."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class HubStore:
    """The hub's SQLite spine. Thread-safe via a single guarding lock."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._warned = {}                 # op -> last warn ts (rate-limit)
        # True when we degraded to a :memory: db — names may run ephemeral,
        # but MINTING MONEY may not (a restart would reset the sequence and
        # re-mint duplicate validation numbers). tito_mint refuses while set.
        self.ephemeral = False
        # Mint-sequence high-water sidecar (CRITICAL double-mint guard): a
        # corrupt hub.db gets quarantined and replaced FRESH — without this,
        # the fresh db would restart seq_01 at 0 and re-mint validation
        # numbers already printed on paper. The sidecar is written on every
        # mint and re-floors tito_meta after ANY (re)open.
        self.seq_sidecar = path + ".seq"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = self._open_or_quarantine(path)
        try:
            with self.lock:
                self._migrate()
        except sqlite3.DatabaseError as e:
            # A partially-corrupt db can pass the open probe and only fail
            # here — same GR-03 posture: quarantine + ONE fresh retry. A
            # fresh (or in-memory) db cannot fail migration.
            log.error("hub db failed migration (%s) — quarantining, "
                      "starting fresh: %s", path, e)
            self._close_quiet(self._conn)
            self._quarantine_files(path)
            self._conn = self._fresh_conn_or_memory(path)
            with self.lock:
                self._migrate()
        log.info("hub store ready: %s (schema v%d)", path, SCHEMA_VERSION)

    # -- connection plumbing (GR-03 posture) ---------------------------------

    @staticmethod
    def _configure(conn):
        """One pragma set for EVERY connect path, so they cannot drift."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _close_quiet(conn):
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass

    def _quarantine_files(self, path):
        """Move the db AND its -wal/-shm sidecars aside so the quarantined
        trio stays together and the replacement starts truly fresh (a stale
        WAL next to a fresh db would shadow it with corrupt pages)."""
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            dst = path + ".corrupt" + suffix
            try:
                if os.path.exists(src):
                    os.replace(src, dst)
            except OSError as e:
                log.error("hub db quarantine of %s failed: %s", src, e)

    def _fresh_conn_or_memory(self, path):
        """Open a fresh db at path; if even THAT fails (SD remounted
        read-only, disk full — the storage states that accompany corruption)
        degrade to an in-memory store: names become ephemeral but the host
        RUNS. A host that won't boot is the worst wedge."""
        try:
            conn = self._configure(
                sqlite3.connect(path, check_same_thread=False))
            self.ephemeral = False
            return conn
        except sqlite3.Error as e:
            log.critical("hub db cannot be (re)created at %s (%s) — running "
                         "with an EPHEMERAL in-memory store; names will not "
                         "survive a restart and TITO MINTING IS REFUSED",
                         path, e)
            self.ephemeral = True
            return self._configure(
                sqlite3.connect(":memory:", check_same_thread=False))

    def _open_or_quarantine(self, path):
        conn = None
        try:
            conn = self._configure(
                sqlite3.connect(path, check_same_thread=False))
            # A REAL probe: quick_check walks the btrees. (SELECT 1 only
            # touched page 1, so partial corruption survived to _migrate.)
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"quick_check: {row[0] if row else 'no result'}")
            return conn
        except sqlite3.DatabaseError as e:
            # GR-03: quarantine an unreadable db, start fresh (names are
            # re-enterable; a corrupt file must not wedge the host).
            log.error("hub db UNREADABLE (%s) — quarantined to %s.corrupt, "
                      "starting fresh: %s", path, path, e)
            self._close_quiet(conn)
            self._quarantine_files(path)
            return self._fresh_conn_or_memory(path)

    def _warn_limited(self, op, e):
        """Log a degraded-read warning at most once a minute per op — a
        dying SD card must not also flood the log (GR-02)."""
        now = time.time()
        if now - self._warned.get(op, 0) >= 60:
            self._warned[op] = now
            log.warning("hub db %s failed (degraded to empty): %s", op, e)

    def _migrate(self):
        """Forward-only schema migration. MUST hold self.lock."""
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS schema_meta ("
                  "key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM schema_meta WHERE key='version'"
                        ).fetchone()
        current = int(row["value"]) if row else 0
        if current < 1:
            # v1: the machine registry + nicknames.
            c.execute("""
                CREATE TABLE IF NOT EXISTS machines (
                    machine_key TEXT PRIMARY KEY,   -- egmId (G2S) or smibId/addr (SAS)
                    protocol    TEXT NOT NULL,      -- 'G2S' | 'SAS'
                    nickname    TEXT,               -- operator-set name (may be NULL)
                    first_seen  TEXT NOT NULL,
                    last_seen   TEXT NOT NULL,
                    meta        TEXT                -- JSON: identity/address/port/...
                )""")
        if current < 2:
            # v2: the TITO validation ledger (cross-machine ticket authority).
            # canonical = the 18-digit barcode digits (what a machine scans);
            # vn16 = the SAS 16-digit validation number (NULL for non-SAS
            # entries, none yet). Amounts in MILLICENTS.
            c.execute("""
                CREATE TABLE IF NOT EXISTS tito_tickets (
                    canonical     TEXT PRIMARY KEY,
                    vn16          TEXT UNIQUE,
                    system_id     INTEGER,
                    seq           INTEGER,
                    origin        TEXT,     -- machine key that minted/issued
                    address       INTEGER,  -- SAS address at the origin
                    amount_mc     INTEGER,
                    state         TEXT NOT NULL,  -- minted|issued|redeemPending|redeemed|void
                    ticket_number INTEGER,
                    pending_json  TEXT,     -- {"machine","txn","amountMc","at"}
                    minted_at     TEXT,
                    issued_at     TEXT,
                    redeemed_at   TEXT,
                    redeemed_by   TEXT,
                    extra_json    TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS tito_state_idx "
                      "ON tito_tickets(state)")
            c.execute("CREATE TABLE IF NOT EXISTS tito_meta ("
                      "k TEXT PRIMARY KEY, v TEXT)")
        if current < 3:
            # v3: per-machine AFT registration, keyed by MACHINE IDENTITY
            # (smibId/address — the same string the SAS registry uses). The
            # asset_number is READ FROM THE MACHINE (0x7B/0x74/0x73 echo) and
            # stored AS REPORTED — the hub NEVER assigns it, there is no asset
            # pool. reg_key_fp is a 12-char fingerprint only (the full 20-byte
            # registration key lives satellite-side and never rides the
            # unauthenticated report wire). NO FK to machines: a throttled /
            # evictable registry row must never cascade-delete a registration.
            c.execute("""
                CREATE TABLE IF NOT EXISTS aft_registrations (
                    machine_key     TEXT PRIMARY KEY,   -- smibId/address
                    smib_id         TEXT,               -- command routing
                    address         INTEGER,
                    asset_number    INTEGER,            -- as READ; NULL until reported
                    reg_key_fp      TEXT,               -- 12-char key fingerprint
                    pos_id          INTEGER,
                    status          TEXT,               -- registered|not_registered|pending|asset_unknown|unknown
                    status_code     INTEGER,            -- raw 0x73 status (0x01/0x80/0x40/0x00)
                    registered_at   TEXT,               -- stamped on transition INTO registered
                    updated_at      TEXT NOT NULL,
                    last_attempt_at TEXT,               -- auto-trigger cooldown / backoff
                    attempts        INTEGER NOT NULL DEFAULT 0
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS aft_status_idx "
                      "ON aft_registrations(status)")
        if current < 4:
            # v4: operator-owned per-machine prefs as COLUMNS (the nickname
            # precedent — meta is satellite-reported and rewritten wholesale
            # by touch_machine, so operator state must never live there):
            # denom_cents (R2, display-only) + lock_state (R3, the
            # remembered SAS lock face). ALTER TABLE has no IF NOT EXISTS
            # and sqlite DDL autocommits BEFORE the version stamp commits —
            # a crash between them would re-run the ALTER on next boot,
            # raise OperationalError (a DatabaseError subclass), and
            # quarantine a healthy db. Guard each column via PRAGMA
            # table_info so the step is rerun-safe like the CREATEs above.
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(machines)").fetchall()}
            if "denom_cents" not in cols:
                c.execute("ALTER TABLE machines ADD COLUMN "
                          "denom_cents INTEGER")
            if "lock_state" not in cols:
                c.execute("ALTER TABLE machines ADD COLUMN lock_state TEXT")
        if current < 5:
            # v5: the per-machine SAS kill-switch (Settings tab) + the
            # host-wide options shelf. sas_enabled is operator-owned like
            # denom/lock — NULL or 1 = enabled, 0 = disabled, so every
            # pre-v5 row (NULL) stays enabled. Same rerun-safe PRAGMA
            # guard as v4 (ALTER has no IF NOT EXISTS and DDL autocommits
            # BEFORE the version stamp — a crash between them must not
            # quarantine a healthy db on the re-run). host_settings holds
            # host options as K/V text (first tenant: sysval_fallback).
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(machines)").fetchall()}
            if "sas_enabled" not in cols:
                c.execute("ALTER TABLE machines ADD COLUMN "
                          "sas_enabled INTEGER")
            c.execute("CREATE TABLE IF NOT EXISTS host_settings ("
                      "k TEXT PRIMARY KEY, v TEXT)")
        if current < 6:
            # v6: game-name overrides (the games modal ✎ button) — a
            # durable {key: title} layer the UI merges over the shipped
            # igt_games.json catalog. CREATE IF NOT EXISTS keeps the step
            # rerun-safe like every other (DDL autocommits BEFORE the
            # version stamp — the v4 landmine).
            c.execute("CREATE TABLE IF NOT EXISTS game_names ("
                      "k TEXT PRIMARY KEY, title TEXT NOT NULL, "
                      "updated_at TEXT)")
        if current < 7:
            # v7: the (former) house-hold settlement spine. The feature was
            # REMOVED 2026-07-15 (collector de-cage) — nothing reads or
            # writes these tables anymore — but the DDL STAYS so the schema
            # version chain is honest and an old-vs-fresh hub.db carries the
            # same tables. CREATE IF NOT EXISTS keeps the step rerun-safe
            # (the v4 DDL-autocommit landmine).
            c.execute("""
                CREATE TABLE IF NOT EXISTS meter_marks (
                    machine_key  TEXT NOT NULL,
                    meter_key    TEXT NOT NULL,   -- component key (§HOLD_COMPONENT_SIGN)
                    raw_value    INTEGER NOT NULL, -- last observed, NATIVE units
                    baselined_at TEXT NOT NULL,   -- first sight / last re-baseline
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (machine_key, meter_key)
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS hold_journal (
                    id             INTEGER PRIMARY KEY,  -- ledger ref tail
                    machine_key    TEXT NOT NULL,
                    delta_mc       INTEGER NOT NULL,     -- signed House delta
                    breakdown_json TEXT,
                    observed_at    TEXT NOT NULL,
                    applied        INTEGER NOT NULL DEFAULT 0
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS hold_applied_idx "
                      "ON hold_journal(applied)")
        if current < 8:
            # v8: the fob registry (RFID player/operator cards — the
            # Companion tap path). uid is the CANONICAL hex UID
            # (_norm_fob_uid strips separators + uppercases); account_id
            # is a soft TEXT link into the g2s host's JSON AccountStore
            # ("house", "p1", ... — deliberately no FK, accounts do not
            # live in hub.db). CREATE IF NOT EXISTS keeps the step
            # rerun-safe (the v4 DDL-autocommit landmine).
            c.execute("""
                CREATE TABLE IF NOT EXISTS fobs (
                    uid        TEXT PRIMARY KEY,   -- canonical hex UID
                    tier       TEXT NOT NULL DEFAULT 'player'
                        CHECK(tier IN ('player','attendant','manager',
                                       'reset')),
                    label      TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL DEFAULT '',  -- AccountStore id
                    created_at TEXT NOT NULL,
                    last_seen  TEXT,               -- stamped on every tap
                    meta       TEXT NOT NULL DEFAULT '{}'
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS fobs_account_idx "
                      "ON fobs(account_id)")
        if current < 9:
            # v9: dual-protocol machine linking (task #20). sas_link on a
            # G2S machine's row names the SAS machine_key that is the SAME
            # physical cabinet — when set, the SAS leg is the meter/money
            # authority and G2S hold settlement is suppressed. NULL/absent
            # = unlinked (today's behavior, byte-identical). Operator-owned
            # like nickname/denom, so a COLUMN, never meta. Same rerun-safe
            # PRAGMA guard as v4/v5 (ALTER has no IF NOT EXISTS and DDL
            # autocommits BEFORE the version stamp — a crash between them
            # must not quarantine a healthy db on the re-run).
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(machines)").fetchall()}
            if "sas_link" not in cols:
                c.execute("ALTER TABLE machines ADD COLUMN sas_link TEXT")
        if current < 10:
            # v10: originally a per-machine "SAS host-cashout ready" bench gate.
            # RETIRED 2026-07-12 — that gate is no longer a manual flag; it
            # SELF-DETERMINES from the machine's live 0x74 from-EGM bit
            # (g2s_host.sas_from_egm_ready), so nothing reads or writes this
            # column anymore. The ADD stays (idempotent, rerun-safe PRAGMA guard
            # like v5/v9) purely to keep the schema version monotonic and old
            # dbs untouched — the column is inert.
            cols = {r["name"] for r in
                    c.execute("PRAGMA table_info(machines)").fetchall()}
            if "sas_cashout_ready" not in cols:
                c.execute("ALTER TABLE machines ADD COLUMN "
                          "sas_cashout_ready INTEGER")
        if current < 11:
            # v11: the satellite-assignment registry (zero-config onboarding).
            # sat_id is the satellite's self-reported companionId/smibId
            # (normalized). The binding columns override what the unit reports —
            # the HUB is now the binding authority, not the ExecStart flags — so
            # a collector flashes ONE identical image, the device shows up
            # unassigned, and they bind it to a machine from the UI (no unit
            # edit, no restart). Modeled on the v8 fobs table (a small,
            # UI-edited device registry with soft, FK-less links: egm_id is a
            # G2S egmId, sas_smib/sas_address a SAS leg — all resolved by the
            # g2s host, none live in hub.db). CREATE IF NOT EXISTS keeps the
            # step rerun-safe (the v4 DDL-autocommit landmine).
            c.execute("""
                CREATE TABLE IF NOT EXISTS satellites (
                    sat_id      TEXT PRIMARY KEY,   -- reported companionId/smibId
                    kind        TEXT NOT NULL DEFAULT 'companion'
                        CHECK(kind IN ('companion','sas')),
                    egm_id      TEXT NOT NULL DEFAULT '',  -- G2S egmId binding
                    sas_smib    TEXT NOT NULL DEFAULT '',  -- SAS leg smibId
                    sas_address TEXT NOT NULL DEFAULT '',  -- SAS leg address
                    label       TEXT NOT NULL DEFAULT '',  -- operator name
                    updated_at  TEXT NOT NULL,
                    meta        TEXT NOT NULL DEFAULT '{}'
                )""")
        if current < 12:
            # v12: per-ticket expiration snapshot. expire_days is the
            # ticket_expire_days setting AT ISSUE TIME (what that paper
            # actually printed); NULL/0 = printed no expiration = the row
            # is immortal. Every pre-v12 row is NULL — history is never
            # retro-killed by a later setting. The reaper deletes issued
            # rows expire_days + EXPIRE_REAP_GRACE_DAYS after issue.
            try:
                c.execute("ALTER TABLE tito_tickets "
                          "ADD COLUMN expire_days INTEGER")
            except sqlite3.OperationalError:
                pass    # rerun-safe (column already there)
        if current < SCHEMA_VERSION:
            c.execute("INSERT INTO schema_meta(key,value) VALUES('version',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(SCHEMA_VERSION),))
            c.commit()
            log.info("hub db migrated %d -> %d", current, SCHEMA_VERSION)
        # Re-floor the mint sequences from the sidecar on EVERY open — this
        # is what makes a quarantine-reset (fresh db) unable to re-mint a
        # number that is already out on paper.
        try:
            with open(self.seq_sidecar, "r", encoding="utf-8") as f:
                floors = json.load(f)
        except (OSError, ValueError):
            floors = {}
        for k, v in floors.items() if isinstance(floors, dict) else []:
            if not (isinstance(k, str) and k.startswith("seq_")):
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            row = c.execute("SELECT v FROM tito_meta WHERE k=?",
                            (k,)).fetchone()
            if v > (int(row["v"]) if row else 0):
                c.execute("INSERT INTO tito_meta(k,v) VALUES(?,?) "
                          "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, str(v)))
                log.warning("tito mint sequence %s floored to %d from the "
                            "sidecar (db was reset/behind — quarantine "
                            "recovery)", k, v)
        c.commit()

    def _write_seq_sidecar_locked(self):
        """Best-effort persist of every seq_* high-water. MUST hold lock."""
        try:
            rows = self._conn.execute(
                "SELECT k, v FROM tito_meta WHERE k LIKE 'seq_%'").fetchall()
            tmp = self.seq_sidecar + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({r["k"]: int(r["v"]) for r in rows}, f)
            os.replace(tmp, self.seq_sidecar)
        except (OSError, ValueError, sqlite3.Error) as e:
            self._warn_limited("seq_sidecar", e)

    # -- machine registry ----------------------------------------------------

    def _room_for_new_row(self, c):
        """MUST hold self.lock. True if a NEW machine row may be inserted.
        At the cap, evicts the stalest UNNAMED row (registry-full must not
        become a permanent lockout — 128 junk posts would otherwise block
        every future real machine). Operator-NAMED rows are never evicted;
        only an all-named-at-cap registry refuses."""
        n = c.execute("SELECT COUNT(*) AS n FROM machines").fetchone()["n"]
        if n < MAX_MACHINES:
            return True
        row = c.execute(
            "SELECT machine_key FROM machines WHERE nickname IS NULL "
            "ORDER BY last_seen ASC LIMIT 1").fetchone()
        if row is None:
            return False
        c.execute("DELETE FROM machines WHERE machine_key=?",
                  (row["machine_key"],))
        log.warning("hub registry full (%d) — evicted stalest unnamed row %r",
                    MAX_MACHINES, row["machine_key"])
        return True

    def touch_machine(self, machine_key, protocol, meta=None):
        """Upsert a machine's registry row: create it (stamping first_seen)
        or refresh last_seen + meta. NEVER overwrites the nickname (that's
        operator-owned). meta is a small JSON-able dict (identity/addr/port).
        Best-effort — a db error is logged, never raised into the hot path."""
        if not machine_key or not isinstance(machine_key, str):
            return
        if len(machine_key) > MAX_KEY_LEN:
            log.warning("hub touch_machine: key too long (%d chars), skipped",
                        len(machine_key))
            return
        now = _now_iso()
        meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
        try:
            with self.lock:
                known = self._conn.execute(
                    "SELECT 1 FROM machines WHERE machine_key=?",
                    (machine_key,)).fetchone()
                if known is None and not self._room_for_new_row(self._conn):
                    log.warning("hub registry full (all %d rows named) — "
                                "not adding %s", MAX_MACHINES, machine_key)
                    return
                self._conn.execute("""
                    INSERT INTO machines
                        (machine_key, protocol, nickname, first_seen,
                         last_seen, meta)
                    VALUES (?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(machine_key) DO UPDATE SET
                        protocol=excluded.protocol,
                        last_seen=excluded.last_seen,
                        meta=COALESCE(excluded.meta, machines.meta)
                """, (machine_key, protocol, now, now, meta_json))
                self._conn.commit()
        except sqlite3.DatabaseError as e:
            log.warning("hub touch_machine(%s) failed: %s", machine_key, e)

    def set_nickname(self, machine_key, nickname, protocol="unknown"):
        """Set (or clear, with a falsy nickname) a machine's operator name.
        Creates the row if the machine hasn't reported yet (so a name can be
        pre-assigned) — but only within the MAX_MACHINES cap (stalest unnamed
        row evicted at the cap), and only for a sane key, so the
        unauthenticated endpoint can't grow the registry without bound.
        Returns the stored nickname (None when cleared). Raises ValueError
        on bad input / a truly full registry; sqlite errors propagate (the
        endpoint answers 400 — an honest write failure, never a 500)."""
        if not machine_key or not isinstance(machine_key, str):
            raise ValueError("machine_key required (string)")
        machine_key = machine_key.strip()
        if not machine_key or len(machine_key) > MAX_KEY_LEN:
            raise ValueError(
                f"machine_key must be 1..{MAX_KEY_LEN} characters")
        if nickname is not None and not isinstance(nickname, str):
            raise ValueError("name must be a string")
        name = (nickname or "").strip()[:64] or None
        now = _now_iso()
        with self.lock:
            known = self._conn.execute(
                "SELECT 1 FROM machines WHERE machine_key=?",
                (machine_key,)).fetchone()
            if known is None and not self._room_for_new_row(self._conn):
                raise ValueError(
                    f"machine registry full ({MAX_MACHINES}, all named)")
            self._conn.execute("""
                INSERT INTO machines
                    (machine_key, protocol, nickname, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(machine_key) DO UPDATE SET nickname=excluded.nickname
            """, (machine_key, protocol, name, now, now))
            self._conn.commit()
        return name

    def get_nickname(self, machine_key):
        """Best-effort read: a runtime db error degrades to None (this sits
        on the SAS report ingest path — it must never kill a report)."""
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT nickname FROM machines WHERE machine_key=?",
                    (machine_key,)).fetchone()
            return row["nickname"] if row else None
        except sqlite3.Error as e:
            self._warn_limited("get_nickname", e)
            return None

    def set_denom(self, machine_key, denom_cents, protocol="unknown"):
        """Set (or clear) a machine's display denomination in CENTS per
        credit (R2, display-only — never money math). set_nickname's
        posture: creates the row if the machine hasn't reported yet (within
        the MAX_MACHINES cap), RAISES ValueError on bad input so the
        endpoint answers 400. 0 / None / '' / '0' clears (stores NULL).
        Returns the stored value (int or None)."""
        if not machine_key or not isinstance(machine_key, str):
            raise ValueError("machine_key required (string)")
        machine_key = machine_key.strip()
        if not machine_key or len(machine_key) > MAX_KEY_LEN:
            raise ValueError(
                f"machine_key must be 1..{MAX_KEY_LEN} characters")
        # bool is an int subclass — reject it FIRST (the _int_or_none
        # lesson: True would otherwise store as denom 1).
        if isinstance(denom_cents, bool):
            raise ValueError("denomCents must be an integer")
        if denom_cents in (None, "", 0, "0"):
            value = None
        else:
            try:
                value = int(denom_cents)
            except (TypeError, ValueError):
                raise ValueError("denomCents must be an integer")
            if not 1 <= value <= MAX_DENOM_CENTS:
                raise ValueError(
                    f"denomCents must be 1..{MAX_DENOM_CENTS} (0 clears)")
        now = _now_iso()
        with self.lock:
            known = self._conn.execute(
                "SELECT 1 FROM machines WHERE machine_key=?",
                (machine_key,)).fetchone()
            if known is None and not self._room_for_new_row(self._conn):
                raise ValueError(
                    f"machine registry full ({MAX_MACHINES}, all named)")
            self._conn.execute("""
                INSERT INTO machines
                    (machine_key, protocol, denom_cents, first_seen,
                     last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(machine_key) DO UPDATE SET
                    denom_cents=excluded.denom_cents
            """, (machine_key, protocol, value, now, now))
            self._conn.commit()
        return value

    def set_lock_state(self, machine_key, lock_state):
        """Persist the remembered SAS lock face (R3). BEST-EFFORT
        (touch_machine posture — rides report ingest, never raises):
        silently no-ops on anything but a sane key and a known face."""
        if lock_state not in ("locked", "enabled"):
            return
        if not machine_key or not isinstance(machine_key, str) \
                or len(machine_key) > MAX_KEY_LEN:
            return
        now = _now_iso()
        try:
            with self.lock:
                known = self._conn.execute(
                    "SELECT 1 FROM machines WHERE machine_key=?",
                    (machine_key,)).fetchone()
                if known is None and not self._room_for_new_row(self._conn):
                    return
                self._conn.execute("""
                    INSERT INTO machines
                        (machine_key, protocol, lock_state, first_seen,
                         last_seen)
                    VALUES (?, 'SAS', ?, ?, ?)
                    ON CONFLICT(machine_key) DO UPDATE SET
                        lock_state=excluded.lock_state
                """, (machine_key, lock_state, now, now))
                self._conn.commit()
        except sqlite3.DatabaseError as e:
            self._warn_limited("set_lock_state", e)

    def set_sas_enabled(self, machine_key, enabled):
        """Set the per-machine SAS kill-switch (v5, the Settings tab
        toggle). set_denom's posture: creates the row if the machine hasn't
        reported yet (within the MAX_MACHINES cap), RAISES ValueError on
        bad input so /api/settings answers 400. Stores 1/0 — NULL is never
        written here, so a pre-v5 row reads as enabled until the operator
        first touches the toggle. Returns the stored bool."""
        # bool is an int subclass — a bool KEY must fail the string check
        # (isinstance str), and enabled must be a REAL bool (the
        # _int_or_none lesson inverted: 1/0 are not consent for a toggle
        # that parks a machine's serial poll).
        if not machine_key or not isinstance(machine_key, str):
            raise ValueError("machine_key required (string)")
        machine_key = machine_key.strip()
        if not machine_key or len(machine_key) > MAX_KEY_LEN:
            raise ValueError(
                f"machine_key must be 1..{MAX_KEY_LEN} characters")
        if not isinstance(enabled, bool):
            raise ValueError("sasEnabled must be a boolean")
        now = _now_iso()
        with self.lock:
            known = self._conn.execute(
                "SELECT 1 FROM machines WHERE machine_key=?",
                (machine_key,)).fetchone()
            if known is None and not self._room_for_new_row(self._conn):
                raise ValueError(
                    f"machine registry full ({MAX_MACHINES}, all named)")
            self._conn.execute("""
                INSERT INTO machines
                    (machine_key, protocol, sas_enabled, first_seen,
                     last_seen)
                VALUES (?, 'SAS', ?, ?, ?)
                ON CONFLICT(machine_key) DO UPDATE SET
                    sas_enabled=excluded.sas_enabled
            """, (machine_key, 1 if enabled else 0, now, now))
            self._conn.commit()
        return enabled

    def set_sas_link(self, machine_key, sas_key):
        """Link a G2S machine's row to its SAS leg (v9, task #20) — the
        same physical cabinet speaking both protocols. set_sas_enabled's
        posture: creates the row if the machine hasn't reported yet
        (within the MAX_MACHINES cap), RAISES ValueError on bad input so
        /api/settings answers 400. Empty/None sas_key CLEARS the link
        (stores NULL = unlinked, today's behavior). Returns the stored
        value (str | None)."""
        # bool is an int subclass — a bool key/value must fail the string
        # checks (isinstance str), same lesson as set_sas_enabled.
        if not machine_key or not isinstance(machine_key, str):
            raise ValueError("machine_key required (string)")
        machine_key = machine_key.strip()
        if not machine_key or len(machine_key) > MAX_KEY_LEN:
            raise ValueError(
                f"machine_key must be 1..{MAX_KEY_LEN} characters")
        if sas_key is None:
            sas_key = ""
        if not isinstance(sas_key, str):
            raise ValueError("sasLink must be a string (or null to clear)")
        sas_key = sas_key.strip()
        if len(sas_key) > MAX_KEY_LEN:
            raise ValueError(
                f"sasLink must be <= {MAX_KEY_LEN} characters")
        stored = sas_key or None          # empty = clear -> NULL
        now = _now_iso()
        with self.lock:
            known = self._conn.execute(
                "SELECT 1 FROM machines WHERE machine_key=?",
                (machine_key,)).fetchone()
            if known is None and not self._room_for_new_row(self._conn):
                raise ValueError(
                    f"machine registry full ({MAX_MACHINES}, all named)")
            self._conn.execute("""
                INSERT INTO machines
                    (machine_key, protocol, sas_link, first_seen,
                     last_seen)
                VALUES (?, 'G2S', ?, ?, ?)
                ON CONFLICT(machine_key) DO UPDATE SET
                    sas_link=excluded.sas_link
            """, (machine_key, stored, now, now))
            self._conn.commit()
        return stored

    def sas_links(self):
        """{machine_key: sas_link} for every LINKED machine (v9) — the
        g2s host loads this once at startup into its in-memory link map.
        Best-effort: degrades to {} on a db error (rides the status
        path, must never raise)."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT machine_key, sas_link FROM machines "
                    "WHERE sas_link IS NOT NULL").fetchall()
            return {r["machine_key"]: r["sas_link"] for r in rows}
        except sqlite3.Error as e:
            self._warn_limited("sas_links", e)
            return {}

    def host_setting(self, key, default=None):
        """Best-effort read of ONE host option (v5). An absent row — and
        any db error (_warn_limited, dying-SD posture: this sits behind the
        report-reply path) — degrades to `default`; the sysval_fallback
        tenant reads absent as 'on' (the pre-v5 behavior)."""
        if not isinstance(key, str) or not key \
                or len(key) > MAX_SETTING_KEY_LEN:
            return default
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT v FROM host_settings WHERE k=?",
                    (key,)).fetchone()
            return row["v"] if row else default
        except sqlite3.Error as e:
            self._warn_limited("host_setting", e)
            return default

    def set_host_setting(self, key, value):
        """Write one host option (v5). WHITELISTED keys only + bounded str
        value — /api/settings is unauthenticated, so the shelf must not
        grow arbitrary rows (ValueError -> the endpoint answers 400).
        sqlite errors propagate like set_nickname (an honest write
        failure, never a 500). Returns the stored value."""
        if key not in HOST_SETTING_KEYS:
            raise ValueError(f"unknown host setting {key!r}")
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        if key in TICKET_TEXT_KEYS:
            # Ticket-header tenants (C1): a printed-ticket line is 64 chars
            # at most, and an EMPTY write means "unset" — DELETE the row so
            # ticket_header() reads None and the hub never pushes a blank
            # over a machine's existing on-glass text.
            if len(value) > MAX_TICKET_FIELD_LEN:
                raise ValueError(
                    f"{key} must be <= {MAX_TICKET_FIELD_LEN} characters")
            if not value:
                with self.lock:
                    self._conn.execute(
                        "DELETE FROM host_settings WHERE k=?", (key,))
                    self._conn.commit()
                return value
        if key == "ticket_expire_days" and not value:
            # empty = "never expires" (0) -> DELETE the row, the same
            # unset-is-a-delete posture as the ticket text tenants;
            # ticket_header() reads absent as expireDays 0.
            with self.lock:
                self._conn.execute(
                    "DELETE FROM host_settings WHERE k=?", (key,))
                self._conn.commit()
            return value
        if key == "gameroom_name" and not value:
            # empty = the collector cleared their gameroom name -> DELETE the
            # row so host_setting reads "" and the glass falls back to the
            # neutral label (same unset-is-a-delete posture as ticket text).
            with self.lock:
                self._conn.execute(
                    "DELETE FROM host_settings WHERE k=?", (key,))
                self._conn.commit()
            return value
        if len(value) > MAX_SETTING_VAL_LEN:
            raise ValueError(
                f"value must be <= {MAX_SETTING_VAL_LEN} characters")
        with self.lock:
            self._conn.execute(
                "INSERT INTO host_settings(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self._conn.commit()
        return value

    def bump_ticket_rev(self):
        """Increment the 'ticket_rev' counter tenant (C2/C3) and return the
        new int. Every ACCEPTED /api/settings ticket-header write bumps it;
        the SAS satellites re-apply their 0x7D ticket data when the report
        reply's rev differs from the last APPLIED rev, so the bump is what
        makes a save propagate. Read+increment under the one store lock
        (atomic vs concurrent settings POSTs); non-numeric junk in the row
        (only this method ever writes it) restarts the counter instead of
        raising. sqlite errors propagate like set_host_setting (an honest
        write failure, never a silent no-bump)."""
        with self.lock:
            row = self._conn.execute(
                "SELECT v FROM host_settings WHERE k='ticket_rev'"
            ).fetchone()
            try:
                rev = int(row["v"]) if row else 0
            except (TypeError, ValueError):
                rev = 0
            rev += 1
            self._conn.execute(
                "INSERT INTO host_settings(k,v) VALUES('ticket_rev',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(rev),))
            self._conn.commit()
        return rev

    def ticket_header(self):
        """One-SELECT best-effort read of the ticket-header tenants (C1):
        {"propName","line1","line2","titleCash","expireDays","rev"} — absent
        fields None, rev an int (0 until the first save), expireDays an int
        clamped 0..255 (0/absent = tickets never expire). Sits behind the
        engine's TTL-cached report-reply path like host_setting, so a db
        error (_warn_limited, dying-SD posture) degrades to all-unset —
        which the callers read as "push nothing" — rather than raising."""
        out = {"propName": None, "line1": None, "line2": None,
               "titleCash": None, "expireDays": 0, "rev": 0}
        colmap = {"ticket_prop_name": "propName", "ticket_line1": "line1",
                  "ticket_line2": "line2", "ticket_title_cash": "titleCash"}
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT k, v FROM host_settings WHERE k IN "
                    "('ticket_prop_name','ticket_line1','ticket_line2',"
                    "'ticket_title_cash','ticket_expire_days',"
                    "'ticket_rev')").fetchall()
        except sqlite3.Error as e:
            self._warn_limited("ticket_header", e)
            return out
        for r in rows:
            if r["k"] == "ticket_rev":
                try:
                    out["rev"] = int(r["v"])
                except (TypeError, ValueError):
                    pass
            elif r["k"] == "ticket_expire_days":
                try:
                    out["expireDays"] = max(0, min(255, int(r["v"])))
                except (TypeError, ValueError):
                    pass
            elif r["v"]:            # "" reads as unset too (defensive —
                out[colmap[r["k"]]] = r["v"]   # the writer deletes empties)
        return out

    # -- game-name overrides (v6) ---------------------------------------------

    def game_name_set(self, key, title):
        """Set (or clear, with an empty/None title) ONE game-name override
        (v6, the games modal ✎ button). key = the catalog key the UI's
        prettyTheme consults (family6+code3, e.g. '014001H18'). RAISES
        ValueError on bad input so /api/gamenames answers 400 — the
        endpoint is unauthenticated on the floor AP, so key/title are
        bounded and the table is capped at MAX_GAME_NAMES (an UPDATE of
        an existing key always succeeds at the cap; only NEW rows
        refuse — a full table must never lock an operator out of fixing
        a name that already exists). sqlite errors propagate like
        set_nickname (an honest write failure, never a 500). Returns
        the stored title (None when cleared)."""
        if not key or not isinstance(key, str):
            raise ValueError("key required (string)")
        key = key.strip()
        if not key or len(key) > MAX_GAME_KEY_LEN:
            raise ValueError(
                f"key must be 1..{MAX_GAME_KEY_LEN} characters")
        if title is not None and not isinstance(title, str):
            raise ValueError("title must be a string")
        clean = (title or "").strip()
        if len(clean) > MAX_GAME_TITLE_LEN:
            raise ValueError(
                f"title must be <= {MAX_GAME_TITLE_LEN} characters")
        with self.lock:
            if not clean:
                # empty/None title = "reset to catalog": the row goes away
                # (idempotent — deleting an absent key is a clean no-op).
                self._conn.execute(
                    "DELETE FROM game_names WHERE k=?", (key,))
                self._conn.commit()
                return None
            known = self._conn.execute(
                "SELECT 1 FROM game_names WHERE k=?", (key,)).fetchone()
            if known is None:
                n = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM game_names").fetchone()["n"]
                if n >= MAX_GAME_NAMES:
                    raise ValueError(
                        f"game-name overrides full ({MAX_GAME_NAMES})")
            self._conn.execute(
                "INSERT INTO game_names(k, title, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET "
                "title=excluded.title, updated_at=excluded.updated_at",
                (key, clean, _now_iso()))
            self._conn.commit()
        return clean

    def game_names(self):
        """{key: title} for every game-name override. Best-effort: a
        runtime db error degrades to {} (_warn_limited) — this sits
        behind GET /api/gamenames on every UI boot, it must never
        raise."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT k, title FROM game_names").fetchall()
            return {r["k"]: r["title"] for r in rows}
        except sqlite3.Error as e:
            self._warn_limited("game_names", e)
            return {}

    # -- fob registry (v8) ----------------------------------------------------
    # RFID cards/fobs for the Companion tap path. tier gates what a tap DOES
    # (player/attendant/manager = carded session, reset = SAS handpay reset);
    # account_id is a soft TEXT link to the g2s host's JSON AccountStore (no
    # FK — accounts don't live in hub.db). This is NOT money movement, so the
    # write path answers (row, err) tuples instead of raising — /api/fobs
    # relays err as {ok:false,error:...} and a dying SD degrades to an honest
    # error string, never a 500 — and reads are best-effort like names()/
    # game_names(). Every method normalizes the uid through _norm_fob_uid
    # first, so separator/case variants always land on the same row.

    def _fob_row_out(self, r):
        """sqlite row -> the camelCase wire dict (meta JSON-decoded
        defensively — a hand-edited bad blob degrades to {})."""
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (ValueError, TypeError):
            meta = {}
        return {"uid": r["uid"], "tier": r["tier"], "label": r["label"],
                "accountId": r["account_id"], "createdAt": r["created_at"],
                "lastSeen": r["last_seen"], "meta": meta}

    def fob_set(self, uid, tier=None, label=None, account_id=None):
        """Register or PARTIALLY update one fob: None = keep the existing
        value (or the column default on first insert). Returns
        (row_dict, None) on success, (None, error_string) on refusal.
        /api/fobs is unauthenticated on the floor AP, so uid/tier are
        validated, label/account_id clamped, and the table is capped at
        MAX_FOBS — an UPDATE of a known uid always succeeds at the cap;
        only NEW rows refuse (a full table must never lock the operator
        out of re-tiering a card that already exists).

        Tier <-> link invariant (atomic under self.lock — an API-edge
        check-then-write can race a concurrent re-tier; this layer
        cannot): only PLAYER fobs hold an account_id. Linking a non-
        player tier refuses; a re-tier AWAY from player clears the link
        (a reset key must never double as a wallet card)."""
        norm = _norm_fob_uid(uid)
        if norm is None:
            return None, f"uid must be 2..{MAX_FOB_UID_LEN} hex characters"
        if tier is not None and tier not in FOB_TIERS:
            return None, "tier must be one of " + "/".join(FOB_TIERS)
        if label is not None:
            if not isinstance(label, str):
                return None, "label must be a string"
            label = label.strip()[:MAX_FOB_LABEL_LEN]
        if account_id is not None:
            if not isinstance(account_id, str):
                return None, "accountId must be a string"
            account_id = account_id.strip()[:MAX_FOB_LABEL_LEN]
        now = _now_iso()
        try:
            with self.lock:
                known = self._conn.execute(
                    "SELECT tier, account_id FROM fobs WHERE uid=?",
                    (norm,)).fetchone()
                if known is None:
                    n = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM fobs").fetchone()["n"]
                    if n >= MAX_FOBS:
                        return None, f"fob registry full ({MAX_FOBS})"
                eff_tier = tier if tier is not None else (
                    known["tier"] if known is not None else "player")
                if eff_tier != "player":
                    if account_id:
                        return None, ("only player-tier fobs link to an "
                                      "account")
                    account_id = ""     # re-tier away from player (or a
                    #                     non-player upsert) CLEARS the link
                self._conn.execute("""
                    INSERT INTO fobs(uid, tier, label, account_id,
                                     created_at)
                    VALUES (?, COALESCE(?,'player'), COALESCE(?,''),
                            COALESCE(?,''), ?)
                    ON CONFLICT(uid) DO UPDATE SET
                        tier=COALESCE(?, fobs.tier),
                        label=COALESCE(?, fobs.label),
                        account_id=COALESCE(?, fobs.account_id)
                """, (norm, tier, label, account_id, now,
                      tier, label, account_id))
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM fobs WHERE uid=?", (norm,)).fetchone()
            return (self._fob_row_out(row) if row else None), None
        except sqlite3.Error as e:
            self._warn_limited("fob_set", e)
            return None, "db write failed"

    def fob_delete(self, uid):
        """Delete a fob. True if a row went away; False for an unknown or
        malformed uid, or a db error (best-effort — a delete is re-doable
        from the UI, never worth killing the endpoint over)."""
        norm = _norm_fob_uid(uid)
        if norm is None:
            return False
        try:
            with self.lock:
                cur = self._conn.execute(
                    "DELETE FROM fobs WHERE uid=?", (norm,))
                self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            self._warn_limited("fob_delete", e)
            return False

    def fob_get(self, uid):
        """One fob as the camelCase wire dict, or None (unknown uid,
        malformed uid, db error). Best-effort — this sits on the tap
        ingest path, it must never kill a Companion report."""
        norm = _norm_fob_uid(uid)
        if norm is None:
            return None
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT * FROM fobs WHERE uid=?", (norm,)).fetchone()
            return self._fob_row_out(row) if row else None
        except sqlite3.Error as e:
            self._warn_limited("fob_get", e)
            return None

    def fob_touch(self, uid, when_iso):
        """Stamp last_seen on a tap. No-op for an unknown/malformed uid or
        a junk timestamp — NEVER creates a row (unknown cards are only
        registered by the operator through fob_set) and never raises into
        the report ingest."""
        norm = _norm_fob_uid(uid)
        if norm is None or not isinstance(when_iso, str) or not when_iso:
            return
        try:
            with self.lock:
                self._conn.execute(
                    "UPDATE fobs SET last_seen=? WHERE uid=?",
                    (when_iso[:32], norm))
                self._conn.commit()
        except sqlite3.Error as e:
            self._warn_limited("fob_touch", e)

    def fobs(self):
        """Every registered fob (camelCase dicts) in registration order,
        bounded by MAX_FOBS. Best-effort: degrades to [] on a runtime db
        error — this sits behind GET /api/fobs on the Settings tab, it
        must never raise."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT * FROM fobs ORDER BY created_at, uid "
                    "LIMIT ?", (MAX_FOBS,)).fetchall()
            return [self._fob_row_out(r) for r in rows]
        except sqlite3.Error as e:
            self._warn_limited("fobs", e)
            return []

    # ---- v11 satellite assignments (zero-config onboarding) -------------
    def _sat_row_out(self, r):
        """sqlite row -> camelCase wire dict. Empty binding columns surface
        as None (the UI's 'unassigned' state), meta JSON-decoded
        defensively (a hand-edited bad blob degrades to {})."""
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
            if not isinstance(meta, dict):
                meta = {}
        except (ValueError, TypeError):
            meta = {}
        return {"satId": r["sat_id"], "kind": r["kind"],
                "g2sEgmId": r["egm_id"] or None,
                "sasSmib": r["sas_smib"] or None,
                "sasAddress": r["sas_address"] or None,
                "label": r["label"], "updatedAt": r["updated_at"],
                "meta": meta}

    def sat_set(self, sat_id, kind=None, egm_id=None, sas_smib=None,
                sas_address=None, label=None):
        """Assign or PARTIALLY update one satellite: None = keep the
        existing value (or the column default on first insert), so the UI
        can set the machine binding and the friendly label independently.
        Returns (row_dict, None) on success, (None, error_string) on
        refusal. /api/settings is unauthenticated on the floor AP, so id/
        kind are validated, the string fields clamped, and the table capped
        at MAX_SATELLITES — an UPDATE of a known sat_id always succeeds at
        the cap; only NEW rows refuse (a full table must never lock the
        operator out of re-binding a device that already exists). Pass ''
        (empty) for a binding field to CLEAR it (unassign)."""
        norm = _norm_sat_id(sat_id)
        if norm is None:
            return None, f"satId must be 1..{MAX_SAT_ID_LEN} id characters"
        if kind is not None and kind not in SAT_KINDS:
            return None, "kind must be one of " + "/".join(SAT_KINDS)
        for name, val in (("egmId", egm_id), ("sasSmib", sas_smib),
                          ("sasAddress", sas_address), ("label", label)):
            if val is not None and not isinstance(val, str):
                return None, f"{name} must be a string"
        if egm_id is not None:
            egm_id = egm_id.strip()[:MAX_KEY_LEN]
        if sas_smib is not None:
            sas_smib = sas_smib.strip()[:MAX_KEY_LEN]
        if sas_address is not None:
            sas_address = sas_address.strip()[:MAX_KEY_LEN]
        if label is not None:
            label = label.strip()[:MAX_SAT_LABEL_LEN]
        now = _now_iso()
        try:
            with self.lock:
                known = self._conn.execute(
                    "SELECT 1 FROM satellites WHERE sat_id=?",
                    (norm,)).fetchone()
                if known is None:
                    n = self._conn.execute(
                        "SELECT COUNT(*) AS n FROM satellites"
                    ).fetchone()["n"]
                    if n >= MAX_SATELLITES:
                        return None, (f"satellite registry full "
                                      f"({MAX_SATELLITES})")
                self._conn.execute("""
                    INSERT INTO satellites(sat_id, kind, egm_id, sas_smib,
                                           sas_address, label, updated_at)
                    VALUES (?, COALESCE(?,'companion'), COALESCE(?,''),
                            COALESCE(?,''), COALESCE(?,''), COALESCE(?,''), ?)
                    ON CONFLICT(sat_id) DO UPDATE SET
                        kind=COALESCE(?, satellites.kind),
                        egm_id=COALESCE(?, satellites.egm_id),
                        sas_smib=COALESCE(?, satellites.sas_smib),
                        sas_address=COALESCE(?, satellites.sas_address),
                        label=COALESCE(?, satellites.label),
                        updated_at=?
                """, (norm, kind, egm_id, sas_smib, sas_address, label, now,
                      kind, egm_id, sas_smib, sas_address, label, now))
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM satellites WHERE sat_id=?",
                    (norm,)).fetchone()
            return (self._sat_row_out(row) if row else None), None
        except sqlite3.Error as e:
            self._warn_limited("sat_set", e)
            return None, "db write failed"

    def sat_delete(self, sat_id):
        """Forget a satellite assignment. True if a row went away; False
        for an unknown/malformed id or a db error (best-effort — re-doable
        from the UI, never worth killing the endpoint over)."""
        norm = _norm_sat_id(sat_id)
        if norm is None:
            return False
        try:
            with self.lock:
                cur = self._conn.execute(
                    "DELETE FROM satellites WHERE sat_id=?", (norm,))
                self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            self._warn_limited("sat_delete", e)
            return False

    def sat_get(self, sat_id):
        """One satellite assignment as the camelCase wire dict, or None
        (unknown/malformed id, db error). Best-effort."""
        norm = _norm_sat_id(sat_id)
        if norm is None:
            return None
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT * FROM satellites WHERE sat_id=?",
                    (norm,)).fetchone()
            return self._sat_row_out(row) if row else None
        except sqlite3.Error as e:
            self._warn_limited("sat_get", e)
            return None

    def sat_all(self):
        """Every satellite assignment (camelCase dicts) in id order,
        bounded by MAX_SATELLITES. Best-effort: degrades to [] on a db
        error (rides the status path, must never raise)."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT * FROM satellites ORDER BY sat_id "
                    "LIMIT ?", (MAX_SATELLITES,)).fetchall()
            return [self._sat_row_out(r) for r in rows]
        except sqlite3.Error as e:
            self._warn_limited("sat_all", e)
            return []

    def sat_bindings(self):
        """{sat_id: row_dict} for every assigned satellite — the g2s host
        loads this once at startup into its in-memory map and refreshes the
        one row it writes, exactly like sas_links(). Keyed by the SAME
        normalized id the satellite reports, so companion_report can resolve
        an operator-assigned binding by companionId with a dict lookup and
        no lock. Best-effort: degrades to {} on a db error."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT * FROM satellites LIMIT ?",
                    (MAX_SATELLITES,)).fetchall()
            return {r["sat_id"]: self._sat_row_out(r) for r in rows}
        except sqlite3.Error as e:
            self._warn_limited("sat_bindings", e)
            return {}

    def machine_prefs(self, machine_key):
        """Operator prefs in ONE best-effort read (nickname + denomCents +
        lockState + sasEnabled + sasLink) — replaces the ingest's
        get_nickname call at zero extra cost (one query per report either
        way). Degrades to None on any db error (report ingest must never
        die on a dying SD). sasEnabled is presented as a bool with NULL ->
        True (C1: NULL or 1 = enabled, 0 = disabled); sasLink is the
        linked SAS machine_key, None = unlinked (v9)."""
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT nickname, denom_cents, lock_state, sas_enabled,"
                    " sas_link "
                    "FROM machines WHERE machine_key=?",
                    (machine_key,)).fetchone()
            if row is None:
                return None
            return {"nickname": row["nickname"],
                    "denomCents": row["denom_cents"],
                    "lockState": row["lock_state"],
                    "sasEnabled": (True if row["sas_enabled"] is None
                                   else bool(row["sas_enabled"])),
                    "sasLink": row["sas_link"]}
        except sqlite3.Error as e:
            self._warn_limited("machine_prefs", e)
            return None

    def names(self):
        """{machine_key: nickname} for every named machine (nickname set).
        The UI merges this over its local cache so kiosk + phone agree.
        Best-effort: a runtime db error degrades to {} — this sits on the
        2 s /api/status poll the whole UI depends on, it must never raise."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT machine_key, nickname FROM machines "
                    "WHERE nickname IS NOT NULL").fetchall()
            return {r["machine_key"]: r["nickname"] for r in rows}
        except sqlite3.Error as e:
            self._warn_limited("names", e)
            return {}

    def machines(self):
        """Full registry snapshot (for /api/names debug + future UI).
        Best-effort: degrades to [] on a runtime db error."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT machine_key, protocol, nickname, denom_cents, "
                    "lock_state, sas_enabled, sas_link, "
                    "first_seen, last_seen, meta "
                    "FROM machines ORDER BY machine_key"
                ).fetchall()
        except sqlite3.Error as e:
            self._warn_limited("machines", e)
            return []
        out = []
        for r in rows:
            d = dict(r)
            if d.get("meta"):
                try:
                    d["meta"] = json.loads(d["meta"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out

    # -- AFT registration (per-machine, keyed by SAS identity) ---------------
    # A DURABLE audit record of the OBSERVED on-machine AFT registration state
    # (from the satellite's /api/sas/report block + aft_register command
    # results), written on every change. NOTE (2026-07-09): the live fleet view
    # and the auto-register cooldown are served from IN-MEMORY state
    # (sas_snapshot's entry["aft"] and self._aft_autoreg respectively), so this
    # table is currently a write-side inspection/audit record — aft_get/
    # aft_snapshot are the read side, available but not yet wired into
    # /api/status (tracked follow-up). A hub restart therefore resets the
    # cooldown, which is HARMLESS: auto_register is idempotent (an already-
    # registered machine just no-ops). This is NOT money movement — it mirrors
    # observed state — so every method is best-effort like get_nickname/
    # touch_machine: a dying SD degrades to None/{}, never raises into the
    # report/status paths. The asset_number is stored AS READ FROM THE MACHINE;
    # the hub NEVER assigns it (no pool). The full 20-byte key never lives here
    # — only reg_key_fp (12-char fingerprint).

    def _aft_room_for_new_row(self, c):
        """MUST hold self.lock. True if a NEW aft_registrations row may be
        inserted. Reuses MAX_MACHINES as the cap. At the cap it evicts the
        stalest NON-registered row (by updated_at); a 'registered' row is
        NEVER evicted (discarding a live registration record is the money-
        class harm). All-registered-at-cap refuses the insert."""
        n = c.execute("SELECT COUNT(*) AS n FROM aft_registrations"
                      ).fetchone()["n"]
        if n < MAX_MACHINES:
            return True
        row = c.execute(
            "SELECT machine_key FROM aft_registrations "
            "WHERE status IS NULL OR status != 'registered' "
            "ORDER BY updated_at ASC LIMIT 1").fetchone()
        if row is None:
            return False
        c.execute("DELETE FROM aft_registrations WHERE machine_key=?",
                  (row["machine_key"],))
        log.warning("aft table full (%d) — evicted stalest non-registered "
                    "row %r", MAX_MACHINES, row["machine_key"])
        return True

    @staticmethod
    def _aft_status_text(status_code, asset_number):
        """Map the raw 0x73 status byte -> a stable label. A NOT_REGISTERED
        machine that reports no operator asset is 'asset_unknown' (auto-
        register cannot fire without an asset — this label drives the UI's
        'assign an asset' hint and kills the hot retry loop)."""
        if status_code == 0x01:
            return "registered"
        if status_code == 0x40:
            return "pending"
        if status_code == 0x80:
            return "not_registered" if asset_number else "asset_unknown"
        return "unknown"

    def aft_get(self, machine_key):
        """Best-effort read of one machine's AFT registration row (dict) or
        None. Degrades to None on a runtime db error (report/status paths)."""
        try:
            with self.lock:
                row = self._conn.execute(
                    "SELECT * FROM aft_registrations WHERE machine_key=?",
                    (machine_key,)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            self._warn_limited("aft_get", e)
            return None

    def aft_upsert_observed(self, machine_key, smib_id, address,
                            asset_number, status_code,
                            reg_key_fp=None, pos_id=None):
        """Upsert the OBSERVED AFT state for a machine. COALESCE preserves a
        known asset / key fingerprint / POS across a later report that omits
        them (a report needn't re-carry the asset every cycle), registered_at
        is stamped only on the transition INTO registered, and attempts reset
        to 0 on that transition (a fresh RAM-clear episode later gets fresh
        retries). Best-effort — a db error degrades to None (this rides the
        unauthenticated report ingest; it must never raise)."""
        if not machine_key or not isinstance(machine_key, str):
            return None
        if len(machine_key) > MAX_KEY_LEN:
            return None
        smib_id = str(smib_id)[:MAX_KEY_LEN] if smib_id else None
        address = _int_or_none(address)
        asset_number = _int_or_none(asset_number)
        status_code = _int_or_none(status_code)
        pos_id = _int_or_none(pos_id)
        reg_key_fp = str(reg_key_fp)[:16] if reg_key_fp else None
        status = self._aft_status_text(status_code, asset_number)
        now = _now_iso()
        try:
            with self.lock:
                c = self._conn
                existing = c.execute(
                    "SELECT status, registered_at FROM aft_registrations "
                    "WHERE machine_key=?", (machine_key,)).fetchone()
                # registered_at: stamp on the transition INTO registered,
                # keep the historical stamp otherwise (never cleared).
                if status == "registered":
                    reg_at = (existing["registered_at"]
                              if existing and existing["registered_at"]
                              and existing["status"] == "registered"
                              else now)
                else:
                    reg_at = existing["registered_at"] if existing else None
                if existing is None and not self._aft_room_for_new_row(c):
                    log.warning("aft table full (all %d registered) — not "
                                "recording %s", MAX_MACHINES, machine_key)
                    return None
                c.execute("""
                    INSERT INTO aft_registrations
                        (machine_key, smib_id, address, asset_number,
                         reg_key_fp, pos_id, status, status_code,
                         registered_at, updated_at, attempts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(machine_key) DO UPDATE SET
                        smib_id=COALESCE(excluded.smib_id,
                                         aft_registrations.smib_id),
                        address=COALESCE(excluded.address,
                                         aft_registrations.address),
                        asset_number=COALESCE(excluded.asset_number,
                                              aft_registrations.asset_number),
                        reg_key_fp=COALESCE(excluded.reg_key_fp,
                                            aft_registrations.reg_key_fp),
                        pos_id=COALESCE(excluded.pos_id,
                                        aft_registrations.pos_id),
                        status=excluded.status,
                        status_code=excluded.status_code,
                        registered_at=excluded.registered_at,
                        updated_at=excluded.updated_at,
                        attempts=CASE WHEN excluded.status='registered'
                                      THEN 0
                                      ELSE aft_registrations.attempts END
                """, (machine_key, smib_id, address, asset_number,
                      reg_key_fp, pos_id, status, status_code, reg_at, now))
                c.commit()
        except sqlite3.DatabaseError as e:
            self._warn_limited("aft_upsert_observed", e)
            return None

    def aft_mark_attempt(self, machine_key):
        """Stamp last_attempt_at + bump attempts (auto-trigger cooldown /
        backoff so an old smib that keeps rejecting can't loop hot). Creates
        a bare 'unknown' row for a never-observed machine (within the cap) so
        an attempt is still counted. Best-effort."""
        if not machine_key or not isinstance(machine_key, str):
            return None
        if len(machine_key) > MAX_KEY_LEN:
            return None
        now = _now_iso()
        try:
            with self.lock:
                c = self._conn
                existing = c.execute(
                    "SELECT 1 FROM aft_registrations WHERE machine_key=?",
                    (machine_key,)).fetchone()
                if existing is None and not self._aft_room_for_new_row(c):
                    return None
                c.execute("""
                    INSERT INTO aft_registrations
                        (machine_key, status, updated_at, last_attempt_at,
                         attempts)
                    VALUES (?, 'unknown', ?, ?, 1)
                    ON CONFLICT(machine_key) DO UPDATE SET
                        last_attempt_at=excluded.last_attempt_at,
                        attempts=aft_registrations.attempts + 1,
                        updated_at=excluded.updated_at
                """, (machine_key, now, now))
                c.commit()
        except sqlite3.DatabaseError as e:
            self._warn_limited("aft_mark_attempt", e)
            return None

    def aft_snapshot(self):
        """{machine_key: {...}} of every AFT registration row for /api/status
        + the Home UI. Best-effort: degrades to {} on a runtime db error
        (rides the 2 s status poll; must never raise). Only reg_key_fp (the
        fingerprint) is ever exposed — never the full registration key."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT machine_key, smib_id, address, asset_number, "
                    "reg_key_fp, pos_id, status, status_code, registered_at, "
                    "updated_at, last_attempt_at, attempts "
                    "FROM aft_registrations").fetchall()
        except sqlite3.Error as e:
            self._warn_limited("aft_snapshot", e)
            return {}
        return {r["machine_key"]: dict(r) for r in rows}

    # -- TITO validation ledger (phase 2) -------------------------------------
    # The cross-machine ticket authority. Money methods (mint/record/
    # authorize/close) RAISE on db errors — the HTTP edge catches and answers
    # ok:false so the satellite takes its safe fallback (local sid-02 mint /
    # reject-and-return-ticket). Semantics mirror the live-proven
    # SAS/core/sas_ticket_store.py: minted -> issued -> redeemPending ->
    # redeemed (+ void); a rejected close resets redeemPending -> issued;
    # idempotent retries; only the pending HOLDER can close; the ISSUED
    # amount is authoritative. Amounts in MILLICENTS everywhere here.

    @staticmethod
    def _tito_reject(reason):
        return {"authorized": False, "amountMc": 0,
                "reason": reason, "retry": False}

    def _refuse_ephemeral(self, op):
        """An in-memory-degraded ledger must refuse EVERY money write, not
        just mints: a redeemPending/redeemed transition held only in RAM
        evaporates on restart — the exact double-redeem window the durable
        ledger exists to close. Callers surface ok:false so the satellite
        takes its safe fallback (reject the paper / journal the close)."""
        if self.ephemeral:
            raise RuntimeError(f"hub db is EPHEMERAL (storage failed) — "
                               f"refusing tito {op}")

    def _expire_days_now(self, c):
        """The ticket_expire_days setting as the per-row stamp value:
        1..255 -> that int; 0/absent/junk -> None (never expires — the row
        outlives every reaper). Snapshotted at ISSUE time so later setting
        changes never retro-kill paper printed under a different policy.
        Reads DIRECTLY on the caller's connection — every stamp site holds
        self.lock, and host_setting() would re-take it (a plain Lock:
        self-deadlock, caught by the gate the first time)."""
        try:
            row = c.execute("SELECT v FROM host_settings WHERE "
                            "k='ticket_expire_days'").fetchone()
            v = int(row["v"]) if row else 0
        except (TypeError, ValueError, sqlite3.Error):
            v = 0
        return v if 0 < v <= 255 else None

    #: SQL for "this issued row's printed expiration (+ optional grace
    #: days) has passed" — issued_at is the local-naive ISO the store
    #: writes, which sqlite's datetime() parses directly.
    _EXPIRED_SQL = ("state='issued' AND expire_days IS NOT NULL AND "
                    "expire_days > 0 AND datetime(issued_at, '+' || "
                    "(expire_days + ?) || ' days') < "
                    "datetime('now','localtime')")

    def _reap_expired_locked(self, c):
        """MUST hold lock. Reap issued rows whose printed expiration passed
        EXPIRE_REAP_GRACE_DAYS ago. Returns rows reaped. After the reap the
        paper is unknown to the hub — exactly what its face promised."""
        gone = c.execute(
            "DELETE FROM tito_tickets WHERE " + self._EXPIRED_SQL,
            (EXPIRE_REAP_GRACE_DAYS,)).rowcount
        if gone:
            log.info("tito ledger reaped %d expired ticket(s) "
                     "(printed expiry + %dd grace)", gone,
                     EXPIRE_REAP_GRACE_DAYS)
        return gone

    def _make_room_locked(self, c):
        """MUST hold lock. True if an INSERT may proceed under MAX_TICKETS.
        At the cap, prunes the oldest REDEEMED rows (their paper is already
        consumed), then reaps expired issued rows — never live money."""
        n = c.execute("SELECT COUNT(*) AS n FROM tito_tickets"
                      ).fetchone()["n"]
        if n < MAX_TICKETS:
            return True
        gone = c.execute(
            "DELETE FROM tito_tickets WHERE canonical IN ("
            "SELECT canonical FROM tito_tickets WHERE state='redeemed' "
            "ORDER BY redeemed_at ASC LIMIT 100)").rowcount
        if not gone:
            gone = self._reap_expired_locked(c)
        if gone:
            log.warning("tito ledger at cap (%d) — pruned %d row(s)",
                        MAX_TICKETS, gone)
            return True
        log.warning("tito ledger FULL (%d rows, none prunable) — refusing "
                    "new inserts", MAX_TICKETS)
        return False

    @staticmethod
    def _tito_pending(row):
        try:
            return json.loads(row["pending_json"]) \
                if row["pending_json"] else {}
        except (ValueError, TypeError):
            return {}

    def _tito_lookup(self, c, validation):
        """MUST hold self.lock. Find a ledger row by the 18-digit canonical
        (what a machine scans), the 16-digit SAS vn, or the trailing 16
        digits of an 18-digit scan — the self-healing fallback for a
        mispredicted barcode prefix; on that hit the row LEARNS the scanned
        canonical (caller commits)."""
        v = str(validation or "").strip()
        if not v.isdigit() or not 8 <= len(v) <= 18:
            return None
        row = c.execute("SELECT * FROM tito_tickets WHERE canonical=?",
                        (v,)).fetchone()
        if row is None:
            row = c.execute("SELECT * FROM tito_tickets WHERE vn16=?",
                            (v,)).fetchone()
        if row is None and len(v) == 18:
            row = c.execute("SELECT * FROM tito_tickets WHERE vn16=?",
                            (v[-16:],)).fetchone()
            if row is not None and row["canonical"] != v:
                clash = c.execute(
                    "SELECT 1 FROM tito_tickets WHERE canonical=?",
                    (v,)).fetchone()
                if clash is None:
                    c.execute("UPDATE tito_tickets SET canonical=? "
                              "WHERE canonical=?", (v, row["canonical"]))
                    log.info("tito ledger learned canonical %s for vn %s "
                             "(predicted %s)", v, row["vn16"],
                             row["canonical"])
                    row = c.execute(
                        "SELECT * FROM tito_tickets WHERE canonical=?",
                        (v,)).fetchone()
        return row

    @staticmethod
    def _predict_canonical(vn):
        """Barcode digits for a SAS vn: bench-proven the BB2 prints
        [2-digit systemId][16-digit vn] where the prefix repeats the vn's
        leading systemId pair. Mispredictions self-heal in _tito_lookup."""
        return vn if len(vn) == 18 else vn[:2] + vn

    def tito_mint(self, origin, address, amount_mc, system_id=1):
        """Mint — or atomically REUSE an open mint of the same
        origin+address+amount (the 0x57 re-fire case) — a system-validation
        number. The sequence is durable in tito_meta, the number is
        uniqueness-checked against every existing row (a synced satellite
        fallback mint can occupy a number), and an EPHEMERAL store refuses
        outright (a :memory: sequence would re-mint duplicates after a
        restart). Returns {"validationNumber","systemId","seq","canonical",
        "amountMc","reused"}."""
        sid = int(system_id)
        if not 1 <= sid <= 99:
            raise ValueError(f"system_id must be 01..99 (got {system_id})")
        amt = int(amount_mc)
        if amt <= 0:
            raise ValueError(f"mint amount must be positive millicents, "
                             f"got {amount_mc!r}")
        if amt > MAX_TICKET_MILLICENTS:
            raise ValueError(f"mint amount {amt}mc exceeds the "
                             f"{MAX_TICKET_MILLICENTS}mc ceiling")
        if self.ephemeral:
            raise RuntimeError("hub db is EPHEMERAL (in-memory degrade) — "
                               "refusing to mint validation numbers")
        origin = str(origin or "")[:MAX_KEY_LEN]
        now = _now_iso()
        with self.lock:
            c = self._conn
            # ledger hygiene: unprinted mints beyond the TTL are dead weight
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() - MINTED_TTL_SEC))
            gone = c.execute("DELETE FROM tito_tickets WHERE state='minted' "
                             "AND minted_at < ?", (cutoff,)).rowcount
            if gone:
                log.info("tito ledger pruned %d stale unprinted mint(s)",
                         gone)
            self._reap_expired_locked(c)
            reuse_cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() - MINT_REUSE_TTL_SEC))
            row = c.execute(
                "SELECT * FROM tito_tickets WHERE state='minted' AND "
                "origin=? AND address=? AND amount_mc=? AND minted_at >= ? "
                "ORDER BY minted_at DESC LIMIT 1",
                (origin, int(address), amt, reuse_cutoff)).fetchone()
            if row is not None:
                c.commit()
                return {"validationNumber": row["vn16"],
                        "systemId": row["system_id"], "seq": row["seq"],
                        "canonical": row["canonical"], "amountMc": amt,
                        "reused": True}
            n_open = c.execute("SELECT COUNT(*) AS n FROM tito_tickets "
                               "WHERE state='minted'").fetchone()["n"]
            if n_open >= MAX_OPEN_MINTS:
                raise RuntimeError(f"open-mint cap reached ({MAX_OPEN_MINTS})"
                                   " — refusing new mints (spam guard)")
            key = f"seq_{sid:02d}"
            r = c.execute("SELECT v FROM tito_meta WHERE k=?",
                          (key,)).fetchone()
            seq = int(r["v"]) if r else 0
            for _ in range(64):
                seq += 1
                if seq >= 10 ** _SEQ_DIGITS:
                    raise ValueError("validation sequence space exhausted "
                                     f"({_SEQ_DIGITS} digits)")
                vn = f"{sid:02d}{seq:0{_SEQ_DIGITS}d}"
                canonical = f"{sid:02d}" + vn
                if c.execute("SELECT 1 FROM tito_tickets WHERE canonical=? "
                             "OR vn16=?", (canonical, vn)).fetchone() is None:
                    break
            else:
                raise RuntimeError(
                    "no free validation number in 64 tries — ledger anomaly")
            c.execute(
                "INSERT INTO tito_tickets (canonical, vn16, system_id, seq, "
                "origin, address, amount_mc, state, minted_at) "
                "VALUES (?,?,?,?,?,?,?,'minted',?)",
                (canonical, vn, sid, seq, origin, int(address), amt, now))
            c.execute("INSERT INTO tito_meta(k,v) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (key, str(seq)))
            c.commit()
            self._write_seq_sidecar_locked()
            return {"validationNumber": vn, "systemId": sid, "seq": seq,
                    "canonical": canonical, "amountMc": amt, "reused": False}

    def tito_record_issued(self, validation_number, amount_mc, origin,
                           address, ticket_number=None, source="egm",
                           issued_at=None, extra=None):
        """Record a ticket the machine reports printed (the 0x3D/0x4D
        capture). Reconciles a matching 'minted' row to 'issued'; IDEMPOTENT
        for any later state — the vn seen again returns duplicate=True and
        changes NOTHING (a stale re-report can never resurrect a redeemed
        ticket). Returns {"state","duplicate","canonical"}."""
        vn = str(validation_number or "").strip()
        if not vn.isdigit() or not 8 <= len(vn) <= 18:
            raise ValueError(f"validation number must be 8..18 digits: "
                             f"{validation_number!r}")
        if not vn.strip("0"):
            raise ValueError("all-zero validation number is the guide's "
                             "'no validation data' sentinel — not recordable")
        amt = int(amount_mc)
        if amt <= 0:
            raise ValueError(f"ticket amount must be positive millicents, "
                             f"got {amount_mc!r}")
        self._refuse_ephemeral("record_issued")
        origin = str(origin or "")[:MAX_KEY_LEN]
        now = _now_iso()
        with self.lock:
            c = self._conn
            row = self._tito_lookup(c, vn)
            if row is not None:
                if row["state"] == "minted":
                    if row["amount_mc"] not in (None, amt):
                        log.warning(
                            "tito capture amount %dmc != minted %dmc for "
                            "vn %s — using the captured (printed) amount",
                            amt, row["amount_mc"], vn)
                    c.execute(
                        "UPDATE tito_tickets SET state='issued', "
                        "amount_mc=?, ticket_number=?, issued_at=?, "
                        "expire_days=?, extra_json=COALESCE(?, extra_json) "
                        "WHERE canonical=?",
                        (amt, ticket_number, issued_at or now,
                         self._expire_days_now(c),
                         json.dumps(extra, separators=(",", ":"))
                         if extra else None, row["canonical"]))
                    c.commit()
                    log.info("🎫 tito ledger: vn %s ISSUED %dmc by %s "
                             "(reconciled mint, source=%s)",
                             vn, amt, origin, source)
                    return {"state": "issued", "duplicate": False,
                            "canonical": row["canonical"]}
                # A capture whose amount MATCHES the stored row is a true
                # idempotent re-read (retried exception / re-poll of the same
                # ticket): change nothing, so a stale re-report can never
                # resurrect a redeemed ticket.
                stored_amt = int(row["amount_mc"] or 0)
                if stored_amt == amt:
                    c.commit()   # a lookup may have learned the canonical
                    return {"state": row["state"], "duplicate": True,
                            "canonical": row["canonical"]}
                # COLLISION: same validation number, DIFFERENT amount. This is
                # a NEW physical ticket that reused a number — enhanced self-
                # mint (sid=00) regenerates numbers after a RAM clear, so a
                # cash-out can land on one a spent ticket already used. NEVER
                # silently drop it (the old bug: the real ticket vanished onto
                # a redeemed row and was unredeemable).
                prior_state = row["state"]
                if prior_state in ("redeemed", "void"):
                    # The prior paper is spent (redeemed) or dead (void): the
                    # recurring number can only belong to a fresh ticket ->
                    # RE-ISSUE the row to it so the paper in hand is redeemable
                    # for ITS real amount. Loud + audited.
                    note = json.dumps(
                        {"reissuedFrom": prior_state,
                         "priorAmountMc": stored_amt, "reissuedAt": now},
                        separators=(",", ":"))
                    c.execute(
                        "UPDATE tito_tickets SET state='issued', amount_mc=?, "
                        "ticket_number=?, issued_at=?, minted_at=?, "
                        "expire_days=?, redeemed_at=NULL, redeemed_by=NULL, "
                        "pending_json=NULL, extra_json=? WHERE canonical=?",
                        (amt, ticket_number, issued_at or now, now,
                         self._expire_days_now(c), note, row["canonical"]))
                    c.commit()
                    log.warning(
                        "🎫 tito COLLISION: vn %s reused (%s %dmc -> fresh "
                        "%dmc) — RE-ISSUED the spent row; the paper is "
                        "redeemable. Enhanced self-mint recurrence; a system-"
                        "validation machine would not repeat.",
                        vn, prior_state, stored_amt, amt)
                    return {"state": "issued", "duplicate": False,
                            "canonical": row["canonical"],
                            "collision": "reissued", "priorState": prior_state,
                            "priorAmountMc": stored_amt}
                # The colliding row is a LIVE ticket (issued / redeemPending)
                # of a different amount: two unredeemed tickets cannot safely
                # share one number, and overwriting a live/pending row could
                # corrupt a still-valid paper. Keep the existing row and refuse
                # the capture LOUDLY (never silent) so it surfaces for the
                # operator to resolve (redeem/void the live one, then reprint).
                c.commit()
                log.error(
                    "🎫 tito COLLISION-CONFLICT: vn %s reused while the prior "
                    "ticket is still LIVE (%s %dmc) vs fresh %dmc — kept the "
                    "live row, flagged for attention.",
                    vn, prior_state, stored_amt, amt)
                return {"state": prior_state, "duplicate": True,
                        "canonical": row["canonical"], "collision": "conflict",
                        "priorState": prior_state, "priorAmountMc": stored_amt}
            canonical = self._predict_canonical(vn)
            if c.execute("SELECT 1 FROM tito_tickets WHERE canonical=?",
                         (canonical,)).fetchone():
                raise ValueError(f"canonical {canonical} already occupied — "
                                 "refusing ambiguous issue record")
            if not self._make_room_locked(c):
                raise RuntimeError("tito ledger full — insert refused")
            c.execute(
                "INSERT INTO tito_tickets (canonical, vn16, system_id, "
                "origin, address, amount_mc, state, ticket_number, "
                "issued_at, expire_days, extra_json) "
                "VALUES (?,?,?,?,?,?,'issued',?,?,?,?)",
                (canonical, vn if len(vn) == 16 else None,
                 int(vn[:2]) if len(vn) == 16 else None, origin,
                 int(address), amt, ticket_number, issued_at or now,
                 self._expire_days_now(c),
                 json.dumps(extra, separators=(",", ":")) if extra else None))
            c.commit()
            log.info("🎫 tito ledger: vn %s ISSUED %dmc by %s (unminted "
                     "capture, source=%s)", vn, amt, origin, source)
            return {"state": "issued", "duplicate": False,
                    "canonical": canonical}

    def tito_authorize(self, machine, txn, validation, reported_mc=None):
        """The authorize-or-reject decision for a ticket inserted anywhere
        on the floor. Success moves issued -> redeemPending recording WHICH
        (machine, txn) holds it — no second machine can redeem the same
        ticket until the redemption closes. Idempotent: a retry with the
        same (machine, txn) re-draws the identical authorization. Returns
        {"authorized","amountMc","reason","retry"}."""
        machine = str(machine or "")[:MAX_KEY_LEN]
        txn = str(txn or "")[:192]
        if self.ephemeral:
            # fail-safe reject, not a raise: the paper is returned intact
            return self._tito_reject(
                "hub ledger is ephemeral (storage failed) — ticket returned")
        now = _now_iso()
        with self.lock:
            c = self._conn
            row = self._tito_lookup(c, validation)
            c.commit()               # persist a learned canonical either way
            if row is None:
                return self._tito_reject(
                    "unknown validation number (never issued here)")
            state = row["state"]
            pend = self._tito_pending(row)
            if state == "redeemPending":
                if pend.get("machine") == machine and \
                        pend.get("txn") == txn:
                    return {"authorized": True,
                            "amountMc": int(pend.get("amountMc", 0)),
                            "reason": "retry — same machine re-drew the "
                                      "same authorization",
                            "retry": True}
                return self._tito_reject(
                    "redemption already in process at %s"
                    % pend.get("machine"))
            if state == "redeemed":
                return self._tito_reject("ticket already redeemed")
            if state == "void":
                return self._tito_reject("ticket voided")
            if state == "minted":
                return self._tito_reject(
                    "validation number minted but no ticket was ever "
                    "printed against it")
            if state != "issued":
                return self._tito_reject(f"unredeemable state {state!r}")
            amount = int(row["amount_mc"] or 0)
            if amount <= 0:
                return self._tito_reject("issued amount unknown/invalid")
            note = "authorized"
            if reported_mc not in (None, 0) and int(reported_mc) != amount:
                note = ("authorized (issued amount %d overrides machine-"
                        "reported %d)" % (amount, int(reported_mc)))
            c.execute(
                "UPDATE tito_tickets SET state='redeemPending', "
                "pending_json=? WHERE canonical=?",
                (json.dumps({"machine": machine, "txn": txn,
                             "amountMc": amount, "at": now},
                            separators=(",", ":")), row["canonical"]))
            c.commit()
            return {"authorized": True, "amountMc": amount,
                    "reason": note, "retry": False}

    def tito_close(self, machine, txn, validation, redeemed):
        """Close a redemption: redeemed=True CONSUMES the ticket,
        redeemed=False RESETS a matching redeemPending row back to issued
        (the paper stays valid — the machine rejected/returned it). Only
        the pending HOLDER (machine+txn) can close in either direction;
        idempotent for duplicate closes. Returns the resulting state, or
        None for a ticket this ledger never issued."""
        machine = str(machine or "")[:MAX_KEY_LEN]
        txn = str(txn or "")[:192]
        self._refuse_ephemeral("close")
        with self.lock:
            c = self._conn
            row = self._tito_lookup(c, validation)
            c.commit()
            if row is None:
                return None
            pend = self._tito_pending(row)
            holds = (pend.get("machine") == machine
                     and pend.get("txn") == txn)
            if redeemed:
                if holds:
                    c.execute(
                        "UPDATE tito_tickets SET state='redeemed', "
                        "redeemed_at=?, redeemed_by=?, pending_json=NULL "
                        "WHERE canonical=?",
                        (_now_iso(), machine, row["canonical"]))
                    c.commit()
                    log.info("🎟️ tito ledger: %s REDEEMED at %s "
                             "(%dmc)", row["canonical"], machine,
                             int(row["amount_mc"] or 0))
                    return "redeemed"
            elif row["state"] == "redeemPending" and holds:
                c.execute("UPDATE tito_tickets SET state='issued', "
                          "pending_json=NULL WHERE canonical=?",
                          (row["canonical"],))
                c.commit()
                return "issued"
            return row["state"]

    def tito_sync_merge(self, origin, validation_seq, tickets):
        """Satellite ledger sync (startup import + post-outage reconcile).
        NEVER-DOWNGRADE merge: a higher-ranked state wins, equal keeps the
        hub's row, a hub row AHEAD of the satellite is a logged conflict.
        Floors the mint sequences to the satellite's reported seq (capped —
        one hostile POST must not exhaust the 14-digit space) so the hub can
        never re-mint a number the satellite already used.

        An incoming 'redeemPending' is coerced to 'issued': a pending hold
        needs a HOLDER, and a satellite-side pending (pre-hub history or a
        crash mid-redemption) has none the hub could honor — a holderless
        redeemPending row would be permanently unredeemable AND unclosable.
        Coercing keeps the paper valid; a live re-insert re-authorizes here.

        Journaled closes are NOT replayed here — they go through the
        TitoAuthority union at the /api/tito/sync edge (an AVP-issued
        voucher's close must reach the VoucherStore, which this ledger
        cannot see). Bounded (MAX_SYNC_TICKETS). Returns
        {"inserted","updated","conflicts"}."""
        self._refuse_ephemeral("sync_merge")
        origin = str(origin or "")[:MAX_KEY_LEN]
        inserted = updated = conflicts = 0
        with self.lock:
            c = self._conn
            for t in (tickets or [])[:MAX_SYNC_TICKETS]:
                try:
                    vn = str(t.get("vn", "")).strip()
                    state = t.get("state")
                    if state == "redeemPending":
                        state = "issued"        # see docstring — no holder
                    if not vn.isdigit() or not 8 <= len(vn) <= 18 or \
                            not vn.strip("0") or \
                            state not in _TITO_STATE_RANK:
                        continue
                    amt = int(t.get("amountMc") or 0)
                    if amt <= 0 or amt > MAX_TICKET_MILLICENTS:
                        continue
                    tno = t.get("ticketNumber")
                    tno = int(tno) if isinstance(tno, int) \
                        and not isinstance(tno, bool) \
                        and 0 <= tno < 2 ** 31 else None
                    row = self._tito_lookup(c, vn)
                    if row is None:
                        canonical = self._predict_canonical(vn)
                        if c.execute("SELECT 1 FROM tito_tickets WHERE "
                                     "canonical=?", (canonical,)).fetchone():
                            conflicts += 1
                            continue
                        if not self._make_room_locked(c):
                            conflicts += 1
                            continue
                        c.execute(
                            "INSERT INTO tito_tickets (canonical, vn16, "
                            "system_id, origin, address, amount_mc, state, "
                            "ticket_number, issued_at, redeemed_at, "
                            "redeemed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (canonical, vn if len(vn) == 16 else None,
                             int(vn[:2]) if len(vn) == 16 else None,
                             origin, int(t.get("address") or 0), amt, state,
                             tno, t.get("issuedAt"),
                             t.get("redeemedAt"),
                             str(t.get("redeemedBy"))
                             if t.get("redeemedBy") is not None else None))
                        inserted += 1
                    else:
                        have = _TITO_STATE_RANK.get(row["state"], 0)
                        want = _TITO_STATE_RANK[state]
                        if want > have:
                            c.execute(
                                "UPDATE tito_tickets SET state=?, "
                                "redeemed_at=COALESCE(?, redeemed_at), "
                                "redeemed_by=COALESCE(?, redeemed_by), "
                                "pending_json=NULL WHERE canonical=?",
                                (state, t.get("redeemedAt"),
                                 str(t.get("redeemedBy"))
                                 if t.get("redeemedBy") is not None
                                 else None, row["canonical"]))
                            updated += 1
                        elif row["state"] != state:
                            conflicts += 1
                except (ValueError, TypeError, OverflowError):
                    # OverflowError: sqlite3 raises it for ints past 2^63 —
                    # one hostile field must not abort the whole merge
                    continue
            try:
                rep = int(validation_seq or 0)
            except (TypeError, ValueError):
                rep = 0
            rep = min(rep, MAX_SYNC_SEQ)
            if rep > 0:
                r = c.execute("SELECT v FROM tito_meta WHERE k='seq_01'"
                              ).fetchone()
                if rep > (int(r["v"]) if r else 0):
                    c.execute("INSERT INTO tito_meta(k,v) VALUES('seq_01',?)"
                              " ON CONFLICT(k) DO UPDATE SET "
                              "v=excluded.v", (str(rep),))
            c.commit()
            self._write_seq_sidecar_locked()
        if conflicts:
            log.warning("tito sync from %s: %d conflict(s) — hub kept "
                        "its rows", origin, conflicts)
        if inserted or updated:
            log.info("tito sync from %s: +%d inserted, %d updated",
                     origin, inserted, updated)
        return {"inserted": inserted, "updated": updated,
                "conflicts": conflicts}

    def tito_void(self, validation):
        """Bench unstick (mirrors VoucherStore.void_id): park a ledger row
        at 'void' so any future authorize rejects it; clears a pending
        marker (that IS the unstick). Returns the prior state, or None for
        a row this ledger never held."""
        self._refuse_ephemeral("void")
        with self.lock:
            c = self._conn
            row = self._tito_lookup(c, validation)
            if row is None:
                c.commit()
                return None
            prior = row["state"]
            c.execute("UPDATE tito_tickets SET state='void', "
                      "pending_json=NULL WHERE canonical=?",
                      (row["canonical"],))
            c.commit()
            log.warning("tito ledger: %s VOIDED (was %s)",
                        row["canonical"], prior)
            return prior

    def tito_counts(self):
        """Best-effort {total, <state>: n} for /api/status — must never
        raise into the status poll."""
        try:
            with self.lock:
                rows = self._conn.execute(
                    "SELECT state, COUNT(*) AS n FROM tito_tickets "
                    "GROUP BY state").fetchall()
            out = {"total": sum(r["n"] for r in rows)}
            out.update({r["state"]: r["n"] for r in rows})
            return out
        except sqlite3.Error as e:
            self._warn_limited("tito_counts", e)
            return {}

    def tito_list(self, limit=50, offset=0, state=None, vid=None):
        """GET /api/tito/tickets backend — the Cage UI's ticket list.
        vid= is an EXACT read-only lookup on the canonical OR the 16-digit
        vn (limit/offset/state ignored, total 0 or 1 — deliberately NOT
        _tito_lookup, which can WRITE a learned canonical; a GET must not
        mutate). Otherwise newest-first by latest activity
        (COALESCE(redeemed, issued, minted)) with an exact ?state= filter
        (indexed) and limit/offset paging clamped like api_vouchers
        (1..500 / >=0). Every state SHOWS, including 'minted' — a minted
        row is a real open cash-out, there is no VoucherStore-style
        'unused' analogue to hide. BEST-EFFORT read posture (tito_counts
        template): this feeds a UI list, never a money decision, so a
        runtime db error degrades to an empty envelope — a dying SD card
        must not 500 the Cage page."""
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
        order = "ORDER BY COALESCE(redeemed_at, issued_at, minted_at) DESC"
        try:
            with self.lock:
                c = self._conn
                if vid is not None:
                    rows = c.execute(
                        "SELECT * FROM tito_tickets WHERE canonical=? "
                        "OR vn16=?", (str(vid).strip(),) * 2).fetchall()
                    total = len(rows)
                elif state == "expired":
                    # derived DISPLAY state: issued + past its printed
                    # expiry (no grace — the paper already reads expired;
                    # the reaper's grace only delays the delete)
                    cond = self._EXPIRED_SQL
                    total = c.execute(
                        "SELECT COUNT(*) AS n FROM tito_tickets WHERE "
                        + cond, (0,)).fetchone()["n"]
                    rows = c.execute(
                        f"SELECT * FROM tito_tickets WHERE {cond} {order} "
                        "LIMIT ? OFFSET ?", (0, limit, offset)).fetchall()
                elif state is not None:
                    total = c.execute(
                        "SELECT COUNT(*) AS n FROM tito_tickets "
                        "WHERE state=?", (state,)).fetchone()["n"]
                    rows = c.execute(
                        f"SELECT * FROM tito_tickets WHERE state=? {order} "
                        "LIMIT ? OFFSET ?",
                        (state, limit, offset)).fetchall()
                else:
                    total = c.execute("SELECT COUNT(*) AS n "
                                      "FROM tito_tickets").fetchone()["n"]
                    rows = c.execute(
                        f"SELECT * FROM tito_tickets {order} "
                        "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        except sqlite3.Error as e:
            self._warn_limited("tito_list", e)
            return {"total": 0, "tickets": []}
        # Row conversion OUTSIDE the lock (machines() pattern) — fetchall
        # already materialized the rows. amount_mc -> amountMc stays
        # MILLICENTS (the hub.db money rule; UI renders $ via /100000).
        # Display derivation: an issued row past its printed expiry SHOWS
        # as 'expired' (the UI's chip/filter; the raw state stays issued
        # in the db until the reaper's grace runs out — this list feeds a
        # UI, never a money decision).
        def _row_state(r):
            try:
                ed = r["expire_days"]
            except (KeyError, IndexError):
                ed = None
            if r["state"] == "issued" and ed and r["issued_at"]:
                try:
                    exp = time.mktime(time.strptime(
                        r["issued_at"], "%Y-%m-%dT%H:%M:%S"))                         + int(ed) * 86400
                    if time.time() > exp:
                        return "expired"
                except (ValueError, TypeError, OverflowError):
                    pass
            return r["state"]
        return {"total": total, "tickets": [{
            "canonical": r["canonical"], "vn16": r["vn16"],
            "state": _row_state(r), "amountMc": r["amount_mc"],
            "origin": r["origin"], "address": r["address"],
            "ticketNumber": r["ticket_number"], "mintedAt": r["minted_at"],
            "issuedAt": r["issued_at"], "redeemedAt": r["redeemed_at"],
            "redeemedBy": r["redeemed_by"],
            "pending": self._tito_pending(r) or None,
        } for r in rows]}

    def close(self):
        with self.lock:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass
