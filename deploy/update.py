#!/usr/bin/env python3
"""CabiNet updater — bring a hub AND its satellites current from the repo.

Run this ON THE HUB, from inside your CabiNet clone:

    python3 deploy/update.py              # ask before doing anything
    python3 deploy/update.py --dry-run    # show what would happen, touch nothing
    python3 deploy/update.py --yes        # no prompt (for a cron/ssh one-liner)

WHY THIS EXISTS, AND WHY IT IS NOT JUST `git pull`
--------------------------------------------------
CabiNet is a FLEET: the hub plus one satellite Pi per SAS machine, and the
`SAS/core/*.py` tree has to be IDENTICAL on both. Pulling on the hub alone
leaves every satellite running new `sas_host.py` against old core modules, and
that combination fails in two ways we have both actually hit:

  * LOUD  — `ImportError: cannot import name ...` and the service crash-loops
            until systemd gives up. At least it screams.
  * QUIET — the service reports `active`, logs ZERO tracebacks, the poll counter
            keeps climbing, and the SAS link is simply dead. `build_meter_poll`
            raises on any poll missing from `TYPE_R_POLLS`, so a stale
            `sas_meters.py` kills the census before the link ever establishes and
            the exception never reaches the journal. That one cost hours and a
            physical inspection of a perfectly good serial cable.

So this script updates the WHOLE set, gates it BEFORE restarting anything, and
rolls back if the floor does not come back.

WHAT IT WILL NOT DO
-------------------
  * It never touches `data/` — hub.db, voucher state, config inventory,
    registrations and nicknames are yours. (`G2S/data/` is gitignored, so a pull
    cannot clobber it either; the satellite sync excludes it explicitly.)
  * It never runs while a tournament is armed, counting down, or running.
  * It never restarts anything if the gates fail.
  * It does not touch OS packages, systemd unit files, or your network config.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HUB = "http://127.0.0.1:8081"
DEFAULT_REMOTE = "https://github.com/ajs1616/CabiNet.git"

# The self-contained gates: every one of these runs with no live host and no
# pytest. `avp_replay.py` is deliberately absent — the wire captures it replays
# are not part of the public distribution — and `test_epoch_race.py` needs a
# live host on a given port, so it is a bench tool, not an update gate.
G2S_GATES = (
    "test_link_demotion.py",
    "test_tournament.py",
    "test_hub_store.py",
    "test_hub_tito.py",
    "test_companion_rfid.py",
    "test_aft_push.py",
    "test_machine_linking.py",
)

# Satellites get the whole SAS tree minus data/caches. It is a SET, not a file:
# see the QUIET failure in the module docstring.
SAT_EXCLUDES = ("data/", "__pycache__/", "*.pyc", "logs/", "spec/", "tmp_stage_/")

# How long to let the floor come back before calling a restart failed. A real
# AVP/BB2 rejoin has been measured at ~50 s; this is deliberately generous.
REJOIN_TIMEOUT = 210


# Everything under G2S/data/ is YOURS: wallets, tickets, transfers, the machine
# registry, nicknames, DHCP leases. A pull cannot touch it (G2S/data/ is
# gitignored) and the satellite sync excludes data/ — but "we don't overwrite
# it" is NOT the same as "it is protected", because the new code MIGRATES it.
# hub.db carries a schema version and _migrate() runs automatically the moment
# the new host opens it. That makes a code-only rollback unsafe on its own:
# you would put old code back on top of an already-upgraded database. So every
# update takes a full snapshot FIRST, and a rollback restores code AND data
# together.
BACKUP_DIRNAME = "_backups"
BACKUPS_KEPT = 5

# Where adopt() puts ITS snapshot. Deliberately OUTSIDE the install: adoption
# rewrites the tree from the release, and a snapshot that lives at
# G2S/data/_backups is inside the blast radius of the very operation it exists
# to survive. (A normal update never rewrites the tree, so its snapshots stay
# next to the data they came from, where operators expect to find them.)
EXTERNAL_BACKUP_DIR = os.path.expanduser("~/.cabinet-backups")

# Data lives under <component>/data/. Named here because adoption has to PROVE
# it did not touch any of it, not merely intend not to.
DATA_DIRS = ("G2S/data", "SAS/data", "Companion/data")

# hub.db runs in WAL mode, so a plain copy can catch a torn database with its
# -wal unmerged. sqlite3's own backup API is online-safe; everything else here
# is written whole, so a copy is fine. The -wal/-shm siblings are deliberately
# NOT copied — the API folds them in.
SQLITE_FILES = ("hub.db",)
SKIP_SUFFIXES = ("-wal", "-shm")


class Fail(Exception):
    """A step failed in a way that should trigger rollback."""



def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("\n\033[1m== %s\033[0m" % msg)


def run(cmd, cwd=None, check=True, quiet=False, timeout=900, env=None):
    """Run a command, returning (rc, stdout+stderr)."""
    if not quiet:
        say("   $ %s" % (" ".join(cmd) if isinstance(cmd, list) else cmd))
    p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                       capture_output=True, text=True, timeout=timeout,
                       env=(dict(os.environ, **env) if env else None))
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise Fail("command failed (rc=%d): %s\n%s"
                   % (p.returncode, cmd, out.strip()[-2000:]))
    return p.returncode, out


def git(args, cwd, **kw):
    return run(["git"] + args, cwd=cwd, **kw)


def repo_root():
    """The CabiNet tree this script lives in — clone or not.

    Deliberately does NOT require a git clone. Plenty of hubs were stood up by
    copying files or unpacking a download, and telling those operators to go
    read a page of `git init` incantations is exactly the "where do I put the
    updated files?" frustration this tool exists to end. adopt() turns such a
    tree into a clone in place, so the instruction is always the same single
    command."""
    # self_update() re-execs a COPY of this script out of a temp directory, so
    # __file__ no longer sits in the install and the search below would start
    # from /tmp. It hands the root over explicitly instead of making the new
    # process guess. (This is why `cd /tmp && python3 /path/to/update.py` —
    # the documented cron/ssh one-liner — used to die on the second hop.)
    #
    # ONLY honoured on that hop. An env var that outranks every other way of
    # finding the tree is a foot-gun if it can leak in from a shell profile or
    # a stale export: it is read BEFORE any safety check, so a wrong value
    # would aim adopt() at a directory the operator never named.
    handed = os.environ.get("CABINET_UPDATER_ROOT")
    if handed and os.environ.get("CABINET_UPDATER_FRESH"):
        if os.path.isdir(os.path.join(handed, "G2S")) \
                and os.path.isdir(os.path.join(handed, "SAS")):
            return handed
        raise Fail("CABINET_UPDATER_ROOT=%r is not a CabiNet install" % handed)

    # WHERE THIS FILE SITS is the most trustworthy answer, and it is tried
    # first. Asking git for the top of the work tree used to come first, and
    # that is how a copied install nested in an unrelated repo got the
    # ENCLOSING repo handed back whenever that repo happened to have a G2S
    # directory of its own — after which every fetch/merge/reset drove the
    # wrong tree. The layout is known: this file is <root>/deploy/update.py.
    #
    # Be forgiving about where it was DROPPED, though — existing installs
    # predate deploy/update.py, so operators bootstrap it by downloading this
    # one file, and "put it in exactly the right folder" is the sort of
    # instruction that makes people give up. Accept the usual landing spots.
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.dirname(here), here, os.getcwd()):
        if os.path.isdir(os.path.join(cand, "G2S")) \
                and os.path.isdir(os.path.join(cand, "SAS")):
            return os.path.abspath(cand)
    # Last resort only: a clone whose layout we did not recognise.
    rc, out = run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                  check=False, quiet=True)
    if rc == 0 and os.path.isdir(os.path.join(out.strip(), "G2S")) \
            and os.path.isdir(os.path.join(out.strip(), "SAS")):
        return out.strip()
    raise Fail("can't find your CabiNet install from %s — it should hold G2S/ "
               "and SAS/. cd into it and run this again." % here)


def is_clone(root):
    """True only if `root` is the TOP of a git repo — not merely inside one.

    `--is-inside-work-tree` was the obvious check and it is wrong: an install
    copied into any tree that happens to be a git repo (a dotfiles repo, a
    parent project, an SD card imaged from one) answers yes, adoption is
    skipped, and every fetch/merge/reset/worktree afterwards drives the
    ENCLOSING repo. That run reports "✅ Updated" and restarts the floor while
    not one CabiNet file has changed."""
    rc, out = run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                  check=False, quiet=True)
    return rc == 0 and os.path.realpath(out.strip() or "/nonexistent") \
        == os.path.realpath(root)


def adopt(root, remote, assume_yes):
    """Turn a copied/unpacked install into a tracked clone, in place.

    The dangerous step is adopting released code over files that may have been
    edited in place, so this NEVER runs silently: it snapshots the data first
    (OUTSIDE the tree), proves no data file is tracked by the repo, shows
    exactly which local files the adoption would overwrite, asks, and then
    proves afterwards that every data file is still there.

    ⚠️ THE INDEX IS RADIOACTIVE HERE, AND THIS COST US THE WHOLE FEATURE ONCE.
    The preview below needs the worktree staged to be meaningful, but
    `git add -A` into the REAL index followed by `git checkout -f -B main
    origin/main` DELETES every staged path the release does not ship — which is
    all of G2S/data/ on any install whose .gitignore predates the blanket
    `G2S/data/` rule, or that arrived via `scp -r host:CabiNet/*` (no dotfiles,
    so no .gitignore at all). The snapshot used to live in G2S/data/_backups,
    so it went in the same command. The tool then printed "your data is not
    among them" and exited 0.
    Two independent defences, because losing a floor's money state is not a
    thing to defend once:
      1. the preview is staged into a THROWAWAY index file, so the real index
         stays empty and the checkout has no staged deletions to make. Untracked
         extras — notes, site scripts, old logs — are genuinely left alone now,
         because nothing ever told git about them.
      2. the data inventory is taken before and re-checked after. If anything
         vanished anyway, we restore it and fail loudly instead of reporting
         success."""
    step("This install is not a git clone yet — adopting it")
    say("   Your code will be replaced with the released version so that future")
    say("   updates are a single command. Your DATA is not touched.")

    ok, detail = db_healthy(root)
    if not ok:
        raise Fail("hub.db fails quick_check (%s) — fix or restore the "
                   "database before adopting." % detail)
    # OUTSIDE the tree — see the docstring.
    snap = backup_data(root, "pre-adopt", dest_base=EXTERNAL_BACKUP_DIR)
    before = data_inventory(root)

    # ADOPTION IS ALL-OR-NOTHING. Everything from here down is wrapped so that
    # any failure removes the .git we just created. A half-adopted install —
    # real .git, an origin, fetched refs, but an unborn HEAD and no branch — is
    # WORSE than never having started: is_clone() answers True, so adopt() is
    # never retried, and every later run dies in incoming() instead. That state
    # is only escapable by knowing to `rm -rf .git` by hand, which is not a
    # thing an operator should have to know.
    created_git = not os.path.isdir(os.path.join(root, ".git"))
    try:
        return _adopt_inner(root, remote, assume_yes, snap, before)
    except BaseException:
        if created_git and os.path.isdir(os.path.join(root, ".git")):
            say("   (undoing the partial adoption — removing the .git this "
                "created, so a re-run starts clean)")
            shutil_rmtree(os.path.join(root, ".git"))
        raise


def _adopt_inner(root, remote, assume_yes, snap, before):
    run(["git", "init", "-q"], cwd=root)
    rc, _ = run(["git", "remote", "add", "origin", remote], cwd=root,
                check=False, quiet=True)
    if rc != 0:
        run(["git", "remote", "set-url", "origin", remote], cwd=root,
            quiet=True)
    say("   fetching %s …" % remote)
    git(["fetch", "--prune", "origin"], root, timeout=600)

    # HARD INVARIANT: the repo must not track anything under data/. If that
    # ever stopped being true, adopting would overwrite live money state.
    rc, tracked = run(["git", "ls-tree", "-r", "--name-only", "origin/main"],
                      cwd=root, quiet=True)
    dangerous = [l for l in tracked.splitlines()
                 if "/data/" in l or l.startswith("data/")]
    if dangerous:
        raise Fail("the release tracks files under data/ (%s). Adopting would "
                   "overwrite live state — refusing." % ", ".join(dangerous[:3]))

    # Accurate preview, in a THROWAWAY index. HEAD is unborn and the index
    # empty right after `git init`, so a bare `git diff origin/main` compares
    # against NOTHING and reports the entire release as missing. Staging first
    # makes the comparison mean what it says — but staging into the real index
    # is what deleted people's data (see the docstring), so this index is a
    # temp file that is unlinked before anything is written to the worktree.
    import tempfile
    fd, idx = tempfile.mkstemp(prefix="cabinet-adopt-index-")
    os.close(fd)
    os.unlink(idx)                      # git wants to create it itself
    local = []
    try:
        run(["git", "add", "-A"], cwd=root, quiet=True, timeout=600,
            env={"GIT_INDEX_FILE": idx})
        rc, changed = run(["git", "diff", "--cached", "--name-only",
                           "origin/main"], cwd=root, check=False, quiet=True,
                          env={"GIT_INDEX_FILE": idx})
        local = [l for l in changed.split() if l]
    finally:
        if os.path.exists(idx):
            os.unlink(idx)

    # `local` is a SYMMETRIC diff: it lists files that differ from the release
    # in EITHER direction, so it also contains every local-only file git could
    # see — the operator's notes, their data if this install's .gitignore
    # predates the blanket `G2S/data/` rule, everything. Those are precisely
    # the files adoption does NOT touch.
    #
    # Two lessons are baked in here, both learned the expensive way:
    #  * Do not REFUSE over them. A guard that read this list as "the release
    #    would write over data" made adoption impossible on exactly the two
    #    install shapes the data-loss fix was written for (no .gitignore, or an
    #    old one) — it failed closed and safe, and it also meant those hubs
    #    could never update again. The real invariant, "the release must not
    #    track anything under data/", is checked above against origin/main,
    #    which is the only place it can be true or false.
    #  * Do not LIST them as "will be REPLACED". Only files the release
    #    actually ships can be replaced; anything else stays exactly where it
    #    is. Saying otherwise is the same class of untrue-printed-claim this
    #    whole pass exists to remove.
    rc, shipped = run(["git", "ls-tree", "-r", "--name-only", "origin/main"],
                      cwd=root, quiet=True)
    shipped_set = set(l for l in shipped.splitlines() if l)
    replaced = sorted(l for l in local if l in shipped_set)
    untouched_data = sorted(l for l in local
                            if any(l.startswith(d + "/") for d in DATA_DIRS))

    if replaced:
        say("\n   %d file(s) on disk differ from the release and will be "
            "REPLACED:" % len(replaced))
        for l in replaced[:25]:
            say("     · %s" % l)
        if len(replaced) > 25:
            say("     … and %d more" % (len(replaced) - 25))
        say("   (a copy of all your data is in the snapshot above)")
    else:
        say("\n   No file the release ships differs from what you have — "
            "adoption only starts tracking it.")
    if untouched_data:
        say("\n   Note: %d of your data file(s) are visible to git in this "
            "install" % len(untouched_data))
        say("   (its .gitignore predates the blanket data/ rule). They are NOT")
        say("   touched, and that is verified again once adoption finishes.")

    if not assume_yes:
        say("")
        if input("   Adopt the released code? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            raise Fail("aborted — nothing was changed (your data snapshot is "
                       "kept at %s)" % snap)

    # -f is REQUIRED here, and only here: in a copied install every file is
    # untracked, and plain `checkout` aborts rather than overwrite untracked
    # files. That refusal is right in general and wrong for exactly this step —
    # adopting the release IS the operation. Because the index is still EMPTY
    # (the preview used a throwaway one), the only paths this can write are
    # paths the release actually ships. Untracked extras have never been staged,
    # so git does not know about them and does not remove them.
    git(["checkout", "-f", "-B", "main", "origin/main"], root)
    git(["branch", "--set-upstream-to=origin/main", "main"], root, check=False,
        quiet=True)

    # POST-CONDITION. Everything above is reasoning about git's behaviour; this
    # is the part that checks. A missing data file here means the reasoning was
    # wrong, so put the data back and refuse to call this a success.
    lost = sorted(set(before) - set(data_inventory(root)))
    if lost:
        say("\n   ⚠️  %d data file(s) went missing during adoption — restoring"
            % len(lost))
        restore_data(root, snap)
        still = sorted(set(before) - set(data_inventory(root)))
        raise Fail("adoption removed data files (%s).\n"
                   "   The snapshot at %s covers G2S/data and has been put "
                   "back.%s\n"
                   "   This is a bug — please report it with this output."
                   % (", ".join(lost[:5]), snap,
                      "" if not still else
                      "\n   STILL MISSING (not covered by the snapshot): %s"
                      % ", ".join(still[:5])))

    say("\n   ✅ adopted — now tracking origin/main. Data snapshot: %s" % snap)
    say("      (kept outside the install on purpose, so an adoption cannot "
        "take it with it)")
    return snap


def hub_status(hub_url, timeout=8):
    try:
        with urllib.request.urlopen(hub_url + "/api/status", timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"__error__": str(e)}


def venv_python(root):
    """The interpreter that has pytest, if there is one. The hub's own service
    runs on stdlib system python, so pytest usually lives in a venv."""
    for cand in (os.path.expanduser("~/venvs/cabinet/bin/python"),
                 os.path.join(root, "venv/bin/python"),
                 os.path.join(root, ".venv/bin/python")):
        if os.path.isfile(cand):
            rc, _ = run([cand, "-m", "pytest", "--version"], check=False,
                        quiet=True, timeout=60)
            if rc == 0:
                return cand
    rc, _ = run([sys.executable, "-m", "pytest", "--version"], check=False,
                quiet=True, timeout=60)
    return sys.executable if rc == 0 else None


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def find_satellites(status):
    """Satellites self-report their own IP in the hub's status, so the fleet
    never has to be hardcoded. Returns [{smibId, peer, online}] deduped by IP."""
    out, seen = [], set()
    for key, ent in (status.get("sas") or {}).items():
        peer = str((ent or {}).get("peer") or "").strip()
        if not peer or peer in seen:
            continue
        seen.add(peer)
        out.append({"smibId": (ent or {}).get("smibId") or key.split("/")[0],
                    "peer": peer, "online": bool((ent or {}).get("online")),
                    # the satellite REPORTS the account it runs as — never
                    # guess it. None on older satellites; sat_user() falls back.
                    "user": (ent or {}).get("sshUser") or None})
    return out


def online_machines(status):
    """The EGMs that were joined BEFORE we touched anything — the baseline the
    post-restart check has to match. A machine already offline is not our
    problem and must not fail the update."""
    return sorted(k for k, v in status.items()
                  if isinstance(v, dict) and v.get("commsState") == "onLine")


#: Login used on the satellites. NEVER hardcode a person's username in shipped
#: code — this said "aj" and would simply have failed for everyone else. The
#: hub's own login is the right default (the same operator builds both boxes,
#: and the setup scripts already take user@host), overridable with --ssh-user.
SAT_USER = getpass.getuser()
SAT_USER_OVERRIDE = None   # set only by --ssh-user


def sat_user(sat):
    """The login for THIS satellite, in order of authority:
      1. an explicit --ssh-user (the operator overriding us)
      2. what the satellite REPORTED about itself — the truth, and the reason
         this is not a guess: an operator may have renamed the pi account long
         before deploying
      3. the hub's own login, for satellites too old to report it"""
    return SAT_USER_OVERRIDE or (sat or {}).get("user") or SAT_USER


def sat_ssh(peer, key, remote_cmd, check=True, timeout=120, user=None):
    return run(["ssh", "-i", key, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=8",
                "%s@%s" % (user or SAT_USER, peer), remote_cmd],
               check=check, timeout=timeout)


def sat_sas_dir(peer, key, user=None):
    """Read the satellite's own service file for its working directory rather
    than guessing the path — a wrong guess would 'succeed' into nowhere."""
    rc, out = sat_ssh(peer, key,
                      "grep -h '^WorkingDirectory=' "
                      "/etc/systemd/system/cabinet-sas.service 2>/dev/null "
                      "|| true", check=False, user=user)
    for line in out.splitlines():
        if line.startswith("WorkingDirectory="):
            d = line.split("=", 1)[1].strip()
            if d:
                return d
    return None


# ---------------------------------------------------------------------------
# data protection — not optional
# ---------------------------------------------------------------------------

def data_dir(root):
    return os.path.join(root, "G2S", "data")


def data_inventory(root):
    """Every data file that exists right now, as repo-relative paths.

    Used as a before/after fingerprint so a step that must not touch data can
    be CHECKED rather than trusted. Backup dirs are excluded — pruning one is
    normal and must not read as loss."""
    found = []
    for rel in DATA_DIRS:
        base = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in (BACKUP_DIRNAME, "__pycache__")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def schema_version(root):
    """hub.db's schema version, or None. Reported before AND after so a
    migration is something you SEE, not something you discover later."""
    db = os.path.join(data_dir(root), "hub.db")
    if not os.path.isfile(db):
        return None
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM schema_meta "
                              "WHERE key='version'").fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception:
        return None


def db_healthy(root):
    """PRAGMA quick_check on hub.db — the same probe the host itself uses."""
    db = os.path.join(data_dir(root), "hub.db")
    if not os.path.isfile(db):
        return True, "no hub.db yet"
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            ok = bool(row) and str(row[0]).lower() == "ok"
            return ok, (row[0] if row else "no result")
        finally:
            con.close()
    except Exception as e:
        return False, str(e)


def backup_data(root, tag, dest_base=None):
    """Snapshot every user data file BEFORE the update. Returns the directory.

    This is the thing that makes a rollback honest: restore it and the floor is
    byte-for-byte where it started, schema included.

    `dest_base` puts the snapshot somewhere other than G2S/data/_backups —
    adoption uses it to get the snapshot OUT of the tree it is about to
    rewrite. Normal updates leave it in place, next to the data it came from."""
    import shutil
    src = data_dir(root)
    if not os.path.isdir(src):
        say("   no G2S/data/ yet — nothing to protect")
        return None
    base = dest_base or os.path.join(src, BACKUP_DIRNAME)
    dest = os.path.join(base, "pre-update-%s-%s"
                        % (time.strftime("%Y%m%d-%H%M%S"), tag[:12]))
    os.makedirs(dest, exist_ok=True)
    n, total = 0, 0
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if name == BACKUP_DIRNAME or not os.path.isfile(path):
            continue
        if name.endswith(SKIP_SUFFIXES):
            continue
        try:
            if name in SQLITE_FILES:
                import sqlite3
                con = sqlite3.connect(path, timeout=15)
                try:
                    out = sqlite3.connect(os.path.join(dest, name))
                    try:
                        con.backup(out)          # WAL-safe, online
                    finally:
                        out.close()
                finally:
                    con.close()
            else:
                shutil.copy2(path, os.path.join(dest, name))
            n += 1
            total += os.path.getsize(os.path.join(dest, name))
        except Exception as e:
            raise Fail("could not back up %s (%s) — refusing to update without "
                       "a snapshot of your data" % (name, e))
    say("   snapshot: %d file(s), %.1f MB -> %s"
        % (n, total / 1048576.0, dest))
    prune_backups(base)
    return dest


def prune_backups(base):
    """Keep the most recent BACKUPS_KEPT snapshots. An SD card is small, but
    silently deleting someone's only copy is worse — so say what goes."""
    import shutil
    if not os.path.isdir(base):
        return
    snaps = sorted(d for d in os.listdir(base)
                   if os.path.isdir(os.path.join(base, d)))
    for old in snaps[:-BACKUPS_KEPT]:
        say("   pruning old snapshot %s (keeping the last %d)"
            % (old, BACKUPS_KEPT))
        shutil.rmtree(os.path.join(base, old), ignore_errors=True)


def restore_data(root, snapshot):
    """Put the snapshot back — used by rollback, because the new code may have
    already migrated hub.db and old code cannot be trusted against it.

    ⚠️ MUST NEVER ABORT PART-WAY. Some data files belong to root: the DHCP
    service writes dhcp_leases.json as root, and a plain copy from the service
    user dies with EPERM. The first version of this raised there and left the
    rollback half-done — the worst possible state, and the loudest lesson from
    the first live run. Now every file is attempted, root-owned ones retry
    through sudo, and anything still unrestorable is NAMED at the end rather
    than silently skipped."""
    import shutil
    if not snapshot or not os.path.isdir(snapshot):
        say("   ⚠️  no snapshot to restore — data left as-is")
        return False
    dst = data_dir(root)
    failed = []
    for name in sorted(os.listdir(snapshot)):
        s = os.path.join(snapshot, name)
        if not os.path.isfile(s):
            continue
        # drop a stale -wal/-shm beside a restored db or sqlite will replay it
        # ON TOP of the restore and undo it
        if name in SQLITE_FILES:
            for suf in SKIP_SUFFIXES:
                side = os.path.join(dst, name + suf)
                try:
                    if os.path.exists(side):
                        os.remove(side)
                except PermissionError:
                    run(["sudo", "rm", "-f", side], check=False, quiet=True)
        target = os.path.join(dst, name)
        try:
            shutil.copy2(s, target)
        except PermissionError:
            # root-owned (e.g. dhcp_leases.json) — preserve that ownership
            rc, _ = run(["sudo", "cp", "-p", s, target], check=False,
                        quiet=True)
            if rc != 0:
                failed.append(name)
        except Exception as e:
            failed.append("%s (%s)" % (name, e))
    if failed:
        say("   ⚠️  restored data from %s, EXCEPT: %s"
            % (snapshot, ", ".join(failed)))
        say("      those files are still in the snapshot — copy them back by "
            "hand if they matter (leases regenerate on their own).")
    else:
        say("   restored data from %s" % snapshot)
    return not failed


# ---------------------------------------------------------------------------
# the phases
# ---------------------------------------------------------------------------

def preflight(root, hub_url, allow_dirty, dry=False):
    step("Pre-flight")
    # TRACKED changes only (-uno). An UNTRACKED file cannot be clobbered by a
    # fast-forward, and refusing on one would block every user who ever left a
    # support bundle or a note in their clone. If an untracked file genuinely
    # collides with an incoming one, git's own ff-only error says so plainly.
    rc, dirty = git(["status", "--porcelain", "-uno"], root, quiet=True)
    if dirty.strip():
        say("   local modifications to TRACKED files:")
        for l in dirty.strip().splitlines()[:12]:
            say("     " + l)
        if not allow_dirty:
            raise Fail("refusing to pull over local edits — commit, stash, or "
                       "re-run with --allow-dirty if you are sure they are "
                       "disposable")
        say("   --allow-dirty given; continuing")

    # Are we even updating the tree the hub RUNS from? A second clone is a very
    # easy mistake (someone clones again to "have a look"), and updating it
    # would report success while the live floor stayed on old code forever.
    served = None
    for unit in ("/etc/systemd/system/cabinet-g2s.service",
                 "/lib/systemd/system/cabinet-g2s.service"):
        if os.path.isfile(unit):
            for line in open(unit, encoding="utf-8", errors="replace"):
                if line.startswith("WorkingDirectory="):
                    served = os.path.dirname(line.split("=", 1)[1].strip())
            break
    if served:
        if os.path.realpath(served) != os.path.realpath(root):
            msg = ("this clone is NOT the one the hub runs from.\n"
                   "     hub service runs: %s\n"
                   "     you ran this in : %s\n"
                   "   Updating here would change nothing on the floor. "
                   "Re-run it from the service's clone." % (served, root))
            # A dry run writes nothing, so let it look from anywhere — being
            # able to preview an update from a scratch clone is useful, and
            # the warning still says plainly that it is the wrong tree.
            if not dry:
                raise Fail(msg)
            say("   ⚠️  " + msg.replace("\n", "\n   "))
        else:
            say("   clone      : %s (matches the running service)" % root)
    else:
        say("   clone      : %s (no cabinet-g2s unit here — not a hub?)"
            % root)

    status = hub_status(hub_url)
    if "__error__" in status:
        say("   ⚠️  hub not answering at %s (%s)" % (hub_url, status["__error__"]))
        say("      continuing, but the floor cannot be verified afterwards")
        return status, [], []

    phase = ((status.get("tournament") or {}).get("phase") or "").lower()
    if phase in ("armed", "countdown", "running"):
        raise Fail("a tournament is %s — refusing to restart the floor under "
                   "it. Finish or cancel the round, then re-run." % phase)

    sats = find_satellites(status)
    machines = online_machines(status)
    say("   tournament : %s" % (phase or "idle"))
    say("   machines   : %s" % (", ".join(machines) or "none joined"))
    for s in sats:
        say("   satellite  : %-14s %-15s %s"
            % (s["smibId"], s["peer"], "online" if s["online"] else "OFFLINE"))
    if not sats:
        say("   satellite  : none reporting (nothing to push)")
    return status, sats, machines


def incoming(root):
    step("Fetching")
    git(["fetch", "--prune"], root)
    # A DETACHED HEAD has no upstream, and the rev-list below would fail and be
    # read as "nothing to install" — silently telling someone they are current
    # when they are stranded. Fail loudly instead. (Rollback now uses
    # `reset --hard` precisely so it never leaves you here, but a hand-run
    # `git checkout <sha>` still can.)
    rc, br = git(["rev-parse", "--abbrev-ref", "HEAD"], root, check=False,
                 quiet=True)
    if br.strip() == "HEAD":
        raise Fail("this clone is on a DETACHED HEAD, so there is no branch to "
                   "update. Re-attach first:\n"
                   "     git -C %s checkout main" % root)
    rc, out = git(["rev-list", "--count", "HEAD..@{u}"], root, check=False,
                  quiet=True)
    if rc != 0:
        raise Fail("no upstream configured for branch %r. Set one:\n"
                   "     git -C %s branch --set-upstream-to=origin/main %s"
                   % (br.strip(), root, br.strip()))
    try:
        n = int(out.strip() or "0")
    except ValueError:
        n = 0
    if not n:
        return 0, ""
    rc, log = git(["log", "--oneline", "--no-decorate", "HEAD..@{u}"], root,
                  quiet=True)
    return n, log.strip()


def gates(root, py):
    """Run the gate suite in a THROWAWAY WORKTREE, never in the live tree.

    ⚠️ THIS ISOLATION IS NOT OPTIONAL. Some gates build a REAL host —
    `test_link_demotion.py` calls `G2SHost(keepalive_ms=15000)` outright — and
    G2SHost.__init__ hardcodes its store paths to G2S/data/. Run in the live
    tree, that opens the operator's REAL hub.db, voucher, WAT and account
    stores, runs _migrate() against the live database, and does it while the
    running service has the same file open. That is precisely how GR-04 put 145
    fixture voucher ids into a production voucher_state.json.

    A `git worktree` checkout carries only tracked files, and data/ is
    gitignored — so the gates get a clean empty data dir of their own and can
    write whatever they like into it. Removed when we are done.

    ⚠️ IT GATES WHAT WILL SHIP, NOT HEAD. This used to check out HEAD, which
    makes the gates blind to uncommitted edits — and `push_satellites` rsyncs
    the WORKING TREE. So with --allow-dirty a modified `sas_host.py` that does
    not even parse sailed past "44 passed ✅" and onto the fleet. `git stash
    create` builds a commit object of the working tree without disturbing it,
    which is exactly the thing about to be deployed."""
    import tempfile
    step("Gates (in an isolated copy — never against your live data)")
    if not is_clone(root):
        # --gates-only on an install that was never cloned. There is no git to
        # build a worktree from, and running them in place is the one thing
        # this function exists to prevent, so copy the tree without any data/.
        _gates_in_copy(root, py)
        return
    ref, what = "HEAD", "HEAD (working tree is clean)"
    rc, dirty = run(["git", "status", "--porcelain", "-uno"], cwd=root,
                    check=False, quiet=True)
    if rc == 0 and dirty.strip():
        rc, out = run(["git", "stash", "create"], cwd=root, check=False,
                      quiet=True)
        # run() folds stderr into stdout, and `stash create` can warn (CRLF
        # conversion, for one) while still succeeding — so the sha is the last
        # line, not the whole output, and it has to LOOK like a sha before it
        # is handed to `git worktree add`.
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        sha = lines[-1] if lines else ""
        looks_like_sha = len(sha) >= 7 and all(c in "0123456789abcdef"
                                               for c in sha.lower())
        if rc == 0 and looks_like_sha:
            ref = sha
            what = ("your WORKING TREE (%d modified file(s) included)"
                    % len(dirty.strip().splitlines()))
        else:
            # NEVER silently gate something other than what will ship. Falling
            # back to HEAD here would restore the original bug in full — green
            # gates over uncommitted code that rsync is about to send to the
            # fleet — and would print "working tree is clean" while doing it.
            raise Fail(
                "your tree has uncommitted changes but `git stash create` "
                "could not snapshot them for gating (%s). Refusing to gate "
                "code other than what would ship. Commit or stash your changes "
                "and re-run." % (out.strip()[-300:] or "no output"))
    say("   gating: %s" % what)
    # `stash create` captures tracked modifications only, but the hub RUNS from
    # this tree and rsync ships the whole SAS/ tree — so a NEW untracked file
    # in either place goes ungated. It cannot be carried by a commit object;
    # say so rather than imply coverage we do not have.
    rc, extra = run(["git", "ls-files", "--others", "--exclude-standard", "-z",
                     "G2S/", "SAS/"], cwd=root, check=False, quiet=True)
    extra = [l for l in extra.split("\0") if l]      # -z: paths with spaces
    if extra:
        say("   ⚠️  %d untracked file(s) under G2S/ or SAS/ CANNOT be gated "
            "(they are not in any commit), but the hub runs from this tree "
            "and satellites are rsynced from it: %s"
            % (len(extra), ", ".join(extra[:5])))
    wt = tempfile.mkdtemp(prefix="cabinet-gate-")
    try:
        run(["git", "worktree", "add", "--detach", "--force", wt, ref],
            cwd=root, quiet=True)
        _gates_in(wt, py)
    finally:
        run(["git", "worktree", "remove", "--force", wt], cwd=root,
            check=False, quiet=True)
        shutil_rmtree(wt)


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _gates_in_copy(root, py):
    """Gate a non-clone install by copying it somewhere disposable first.

    Same guarantee as the worktree path — the gates get their own empty data
    dir — reached without git. `data` is excluded by NAME at every level, so a
    gate that opens G2S/data/hub.db creates a fresh one in the copy and the
    operator's real database is never opened."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="cabinet-gate-copy-")
    dest = os.path.join(tmp, "tree")
    try:
        say("   (not a clone — gating a throwaway copy at %s)" % dest)
        shutil.copytree(root, dest, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            "data", ".git", "__pycache__", "*.pyc", "logs",
                            "venv", ".venv"))
        _gates_in(dest, py)
    finally:
        shutil_rmtree(tmp)


def _gates_in(root, py):
    for g in G2S_GATES:
        path = os.path.join(root, "G2S", "tools", g)
        if not os.path.isfile(path):
            say("   %-26s absent in this release — skipped" % g)
            continue
        rc, out = run([sys.executable, os.path.join("tools", g)],
                      cwd=os.path.join(root, "G2S"), check=False, quiet=True)
        line = next((l for l in out.splitlines() if l.startswith("RESULT:")), "")
        if rc != 0 or "0 failed" not in line:
            say("   %-26s ❌ %s" % (g, line or "no RESULT line"))
            # No RESULT line usually means it died before it could print one —
            # an import error, a syntax error in something it pulls in. Show
            # the tail, or the operator is left staring at "no RESULT line"
            # with nothing to act on.
            tail = [l for l in out.strip().splitlines() if l.strip()][-12:]
            if tail:
                say("      ── why it failed " + "─" * 40)
                for l in tail:
                    say("      " + l[:160])
                say("      " + "─" * 56)
            raise Fail("gate %s failed — nothing was pushed and nothing was "
                       "restarted" % g)
        say("   %-26s ✅ %s" % (g, line))
    if py:
        rc, out = run([py, "-m", "pytest", "SAS/", "-q"], cwd=root,
                      check=False, quiet=True)
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:]
        if rc != 0:
            say("   %-26s ❌ %s" % ("pytest SAS/", tail[0] if tail else ""))
            raise Fail("the SAS test suite failed on the new code — nothing "
                       "was restarted")
        say("   %-26s ✅ %s" % ("pytest SAS/", tail[0] if tail else "passed"))
    else:
        say("   pytest not installed — SAS suite SKIPPED (the G2S gates above "
            "still ran). Install pytest in a venv to close this gap.")


def push_satellites(root, sats, key, dry, touched=None):
    """`touched` is a dict this marks the INSTANT a satellite's files are
    about to change. Rollback needs to know that a push began even if it then
    failed on satellite 2 of 3 — a return code cannot express "partly done"."""
    if not sats:
        return
    step("Pushing the SAS tree to %d satellite(s)" % len(sats))
    src = os.path.join(root, "SAS") + "/"
    for s in sats:
        peer = s["peer"]
        u = sat_user(s)
        dest = sat_sas_dir(peer, key, u)
        if not dest:
            raise Fail("%s: could not read WorkingDirectory from its "
                       "/etc/systemd/system/cabinet-sas.service — is it a "
                       "CabiNet satellite?" % peer)
        say("   %s -> %s:%s" % (s["smibId"], peer, dest))
        cmd = ["rsync", "-a", "--no-owner", "--no-group",
               "-e", "ssh -i %s -o BatchMode=yes "
                     "-o StrictHostKeyChecking=accept-new" % key]
        for ex in SAT_EXCLUDES:
            cmd += ["--exclude", ex]
        if dry:
            cmd.append("--dry-run")
        cmd += [src, "%s@%s:%s/" % (u, peer, dest.rstrip("/"))]
        if touched is not None and not dry:
            touched["pushed"] = True
        run(cmd, timeout=600)


def restart_fleet(sats, key, dry, touched=None):
    step("Restarting services")
    if dry:
        say("   (dry run — nothing restarted)")
        return
    # From here on the NEW code is what is running, which is what makes a data
    # restore necessary on rollback: the new host opens hub.db and _migrate()
    # runs. Before this point the new code has never executed.
    if touched is not None:
        touched["restarted"] = True
    for s in sats:
        say("   satellite %s" % s["smibId"])
        sat_ssh(s["peer"], key, "sudo systemctl restart cabinet-sas",
                user=sat_user(s))
    say("   hub cabinet-g2s")
    run(["sudo", "systemctl", "restart", "cabinet-g2s"], timeout=120)


def verify(hub_url, sats, machines, dry):
    step("Verifying the floor came back")
    if dry:
        say("   (dry run — nothing to verify)")
        return True
    deadline = time.time() + REJOIN_TIMEOUT
    want_m, want_s = set(machines), {s["smibId"] for s in sats if s["online"]}
    last = ""
    while time.time() < deadline:
        st = hub_status(hub_url)
        if "__error__" not in st:
            back_m = set(online_machines(st))
            back_s = {x["smibId"] for x in find_satellites(st) if x["online"]}
            last = ("machines %d/%d back, satellites %d/%d online"
                    % (len(back_m & want_m), len(want_m),
                       len(back_s & want_s), len(want_s)))
            if want_m <= back_m and want_s <= back_s:
                say("   ✅ %s" % last)
                return True
        time.sleep(6)
    say("   ❌ timed out after %ds — %s" % (REJOIN_TIMEOUT, last or "hub silent"))
    return False


def rollback(root, old, sats, key, hub_url, machines, snapshot=None,
             touched=None):
    """Undo exactly as much as was actually done — no more, no less.

    `touched` carries two facts the return codes cannot express:

      pushed    — at least one satellite's files were being written. Their
                  SAS tree has to be put back or the fleet is split.
      restarted — the NEW code is what is running. This is the ONLY thing that
                  makes a data restore necessary: the new host opens hub.db and
                  _migrate() runs the moment it starts, so old code would then
                  be reading a database it was never written for.

    Getting this wrong in either direction is expensive. Restoring data when
    the new code never ran copies hub.db over a database the RUNNING service
    still has open — a real risk taken for nothing, and the earlier version of
    this did it whenever a satellite was merely powered off. Restarting the
    floor after a gate failure costs a ~50 s rejoin, stale kiosks and a frozen
    tile if someone is mid-config at a machine, and puts back byte-identical
    code."""
    touched = touched or {}
    pushed, restarted = touched.get("pushed"), touched.get("restarted")
    step("ROLLING BACK to %s" % old[:12])
    try:
        # A dirty tree is about to be discarded by `reset --hard`. That is the
        # right call — it is how we get back to known-good code — but the
        # operator's uncommitted work should not vanish without a trace, and
        # with the gates now testing the working tree a local edit is the
        # ordinary REASON a gate fails. Park it where git can find it again.
        rc, out = run(["git", "status", "--porcelain", "-uno"], cwd=root,
                      check=False, quiet=True)
        parked = None
        if rc == 0 and out.strip():
            rc2, o2 = run(["git", "stash", "create"], cwd=root, check=False,
                          quiet=True)
            lines = [l.strip() for l in o2.splitlines() if l.strip()]
            if rc2 == 0 and lines:
                parked = lines[-1]

        # reset --hard, NOT checkout <sha>: checkout detaches HEAD, which
        # strands the clone with no upstream so the NEXT run cannot tell it is
        # behind. reset keeps you on your branch, tracking intact, and a retry
        # simply fast-forwards again.
        git(["reset", "--hard", old], root)
        if parked:
            say("\n   Your uncommitted changes were discarded by the reset, but"
                " git still\n   has them. Recover with:")
            say("     git -C %s stash apply %s" % (root, parked))

        if not pushed and not restarted:
            say("\n   Nothing had been pushed and nothing restarted, so the tree"
                " is simply back at %s." % old[:12])
            say("   Your floor was never touched, and your data was left "
                "exactly as it is.")
            if snapshot:
                say("   (snapshot kept anyway: %s)" % snapshot)
            return

        # Data BEFORE services, and ONLY if the new code actually ran.
        if restarted and snapshot:
            restore_data(root, snapshot)
        elif snapshot:
            say("   data left as-is — the new code never started, so nothing "
                "migrated it (snapshot kept at %s)" % snapshot)

        if pushed:
            push_satellites(root, sats, key, dry=False)
        if restarted:
            restart_fleet(sats, key, dry=False)
            ok = verify(hub_url, sats, machines, dry=False)
            say("\n   rollback %s. You are back on %s."
                % ("succeeded" if ok else "restarted, but the floor did not "
                   "fully return — check `journalctl -u cabinet-g2s -n 50`",
                   old[:12]))
        else:
            say("\n   rollback succeeded — old code is back on the hub and on "
                "every satellite, and nothing needed restarting because the "
                "new code never started.")
    except Exception as e:
        say("\n   ⚠️  ROLLBACK ITSELF FAILED: %s" % e)
        say("   Recover by hand:")
        say("     git -C %s reset --hard %s" % (root, old))
        if restarted:
            say("     sudo systemctl restart cabinet-g2s")


def self_update(root, argv):
    """Re-exec from the INCOMING version of this script, if it differs.

    ⚠️ THE UPDATER UPDATES ITSELF, and without this the old copy can never
    install its own fixes: the running script is the one already on disk, so a
    release that FIXES the updater is applied by the buggy updater. That is not
    hypothetical — the first live run hit three defects, pushed the fixes, and
    then hit the same three again because the old script was still driving.

    Done BEFORE anything is touched, so nothing has to survive the hand-off:
    the new script simply runs the whole update from the top. The env flag
    bounds it to one hop, so a broken new version cannot loop."""
    if os.environ.get("CABINET_UPDATER_FRESH"):
        return
    try:
        p = subprocess.run(["git", "show", "@{u}:deploy/update.py"], cwd=root,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0 or not p.stdout.strip():
            return                      # no upstream copy — carry on as-is
        incoming_src = p.stdout
        mine = open(os.path.abspath(__file__), encoding="utf-8").read()
        if incoming_src == mine:
            return
    except Exception:
        return                          # never block an update on this check
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="cabinet-update-", suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(incoming_src)
    say("\n\033[1m== The updater itself has changed — re-running the new "
        "one\033[0m")
    say("   (%s)" % tmp)
    # Hand the install over explicitly. The new process runs from /tmp, so it
    # cannot find the tree by looking around __file__ — and the documented
    # one-liner (`cd /tmp && python3 ~/CabiNet/deploy/update.py --yes`) gives it
    # no useful cwd either. Without this it dies with "can't find your CabiNet
    # install from /tmp" on exactly the hop that installs updater fixes.
    env = dict(os.environ, CABINET_UPDATER_FRESH="1",
               CABINET_UPDATER_ROOT=root)
    os.execve(sys.executable, [sys.executable, tmp] + list(argv[1:]), env)


def main():
    global SAT_USER_OVERRIDE
    ap = argparse.ArgumentParser(
        description="Bring a CabiNet hub and its satellites current.")
    ap.add_argument("--hub-url", default=DEFAULT_HUB,
                    help="hub API base (default %s)" % DEFAULT_HUB)
    ap.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/smib"),
                    help="key the hub uses to reach satellites")
    ap.add_argument("--ssh-user", default=None,
                    help="force the login on every satellite (default: each "
                         "satellite reports its own; falls back to %r)"
                         % SAT_USER)
    ap.add_argument("--dry-run", action="store_true",
                    help="show everything, change nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--no-satellites", action="store_true",
                    help="hub only (leaves satellites STALE — see the header)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="pull over local modifications")
    ap.add_argument("--gates-only", action="store_true",
                    help="run the gates against your working tree (uncommitted "
                         "edits included) in an isolated copy, then exit — no "
                         "fetch, no pull, no adopt, no restart")
    ap.add_argument("--remote", default=DEFAULT_REMOTE,
                    help="repo to adopt/track (default %s)" % DEFAULT_REMOTE)
    a = ap.parse_args()

    SAT_USER_OVERRIDE = a.ssh_user

    # If self_update() re-exec'd us, we ARE the temp copy. Clean it up on the
    # way out so /tmp does not slowly fill with cabinet-update-*.py, one per
    # release, forever.
    me = os.path.abspath(__file__)
    if os.environ.get("CABINET_UPDATER_FRESH") \
            and os.path.basename(me).startswith("cabinet-update-"):
        import atexit
        atexit.register(lambda: os.path.exists(me) and os.unlink(me))

    root = repo_root()
    say("CabiNet updater — %s" % root)

    # --gates-only is a READ-ONLY request and must stay one. This used to sit
    # below the adopt block, so `--gates-only --yes` on a copied install would
    # git-init it, fetch, overwrite edited files and only then run the gates —
    # the opposite of what someone asking to "just run the tests" expects.
    if a.gates_only:
        gates(root, venv_python(root))
        say("\n✅ Gates pass on the current tree.")
        return 0

    # An install that was copied or unpacked rather than cloned gets turned
    # into a clone here, so "how do I apply this fix?" has ONE answer forever.
    adopted = False
    if not is_clone(root):
        if a.dry_run:
            say("\n   (dry run) this install is not a git clone; a real run "
                "would offer to adopt it from %s" % a.remote)
            return 0
        adopt(root, a.remote, a.yes)
        adopted = True

    # Fetch first so @{u} is current, then hand off to the incoming updater if
    # this release changes it. Both are cheap and read-only.
    if not a.dry_run:
        git(["fetch", "--prune"], root, check=False, quiet=True)
        self_update(root, sys.argv)

    status, sats, machines = preflight(root, a.hub_url, a.allow_dirty, a.dry_run)
    if a.no_satellites:
        say("\n   ⚠️  --no-satellites: they will keep running OLD SAS code "
            "against your new hub. Only do this if you have none.")
        sats = []
    if sats and not os.path.isfile(a.ssh_key):
        # The hub mints ~/.ssh/smib itself at startup and satellites authorize
        # it during setup, so this is the "upgraded from an older build" case.
        # Mint it here rather than dead-ending, and say exactly what to run.
        say("\n   no satellite key at %s — creating one now" % a.ssh_key)
        os.makedirs(os.path.dirname(a.ssh_key), mode=0o700, exist_ok=True)
        rc, _ = run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                     "-C", "cabinet-hub", "-f", a.ssh_key], check=False)
        if rc != 0 or not os.path.isfile(a.ssh_key):
            raise Fail("could not create %s — make one by hand, or re-run with "
                       "--no-satellites if this hub has none" % a.ssh_key)
        raise Fail(
            "created %s. Your satellite(s) have not authorized it yet, so run "
            "this ONCE per satellite and then re-run the update:\n"
            "     %s"
            % (a.ssh_key,
               "\n     ".join("ssh-copy-id -i %s.pub %s@%s   # %s"
                              % (a.ssh_key, sat_user(s), s["peer"], s["smibId"])
                              for s in sats)))

    n, log = incoming(root)
    if not n and not adopted:
        say("\n✅ Already current — nothing to do.")
        return 0
    if n:
        step("%d new commit(s) to install" % n)
        for l in log.splitlines():
            say("   " + l)
    else:
        # Adoption already checked out origin/main, so there is nothing left to
        # FETCH — but the hub's code was just replaced and the satellites still
        # run whatever they ran this morning. Exiting here with "already
        # current" would leave exactly the hub-new/satellite-old split this
        # tool exists to prevent, after swapping 60 files.
        step("Adopted the released code — gating it and syncing the fleet")

    if not a.yes and not a.dry_run and not adopted:
        say("\nThis will update the hub%s, run the gates, and restart the "
            "floor." % ("" if not sats else " and %d satellite(s)" % len(sats)))
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            say("Aborted — nothing changed.")
            return 1
    elif adopted:
        # Do NOT ask again here. You already said yes to adoption, and adoption
        # has ALREADY replaced the tree — so an abort at this point would not
        # be "nothing changed", it would strand the hub on new code with every
        # satellite on old, which is the split this tool exists to prevent, and
        # with no in-tool way back.
        say("\n   Carrying straight on: the tree is adopted, so the gates and "
            "the satellite sync run now.")

    rc, out = git(["rev-parse", "HEAD"], root, quiet=True)
    old = out.strip()
    say("\n   rollback point: %s" % old[:12])

    snapshot = None
    if not a.dry_run:
        step("Protecting your data")
        ok, detail = db_healthy(root)
        if not ok:
            raise Fail("hub.db fails PRAGMA quick_check BEFORE we touch "
                       "anything (%s). Fix or restore the database first — "
                       "updating on top of a damaged db would bury the "
                       "problem." % detail)
        sv = schema_version(root)
        say("   hub.db     : quick_check ok, schema v%s" % (sv or "?"))
        snapshot = backup_data(root, old)

    if a.dry_run:
        step("Dry run — showing the pull and satellite sync only")
        rc, diff = git(["--no-pager", "diff", "--stat", "HEAD..@{u}"], root,
                       check=False, quiet=True)
        for l in diff.strip().splitlines():
            say("   " + l)
        push_satellites(root, sats, a.ssh_key, dry=True)
        say("\n✅ Dry run complete — nothing was changed.")
        return 0

    step("Updating the working tree")
    try:
        git(["merge", "--ff-only", "@{u}"], root)
    except Fail as e:
        say("   %s" % e)
        raise Fail("fast-forward failed. Either your clone has diverged "
                   "from the remote, or local edits collide with an incoming "
                   "file (the git error above says which). Nothing was "
                   "changed; resolve it with git first.")

    before_schema = schema_version(root)
    # Flipped the instant we are committed to touching the fleet. Everything
    # before this point is reversible with a `reset --hard` and nothing else —
    # see rollback().
    touched = {"pushed": False, "restarted": False}
    try:
        gates(root, venv_python(root))
        push_satellites(root, sats, a.ssh_key, dry=False, touched=touched)
        restart_fleet(sats, a.ssh_key, dry=False, touched=touched)
        if not verify(a.hub_url, sats, machines, dry=False):
            raise Fail("the floor did not come back")
        # The host has now opened hub.db, so any migration has already run.
        # Prove the database survived it before calling this a success.
        ok, detail = db_healthy(root)
        if not ok:
            raise Fail("hub.db fails quick_check AFTER the update (%s)"
                       % detail)
        after_schema = schema_version(root)
        if after_schema != before_schema:
            say("\n   📦 schema MIGRATED v%s -> v%s (snapshot of the old one "
                "is in %s)" % (before_schema, after_schema,
                               os.path.join(BACKUP_DIRNAME,
                                            os.path.basename(snapshot or "?"))))
        else:
            say("\n   schema unchanged (v%s)" % (after_schema or "?"))
    except Fail as e:
        say("\n❌ %s" % e)
        rollback(root, old, sats, a.ssh_key, a.hub_url, machines, snapshot,
                 touched=touched)
        return 2
    except KeyboardInterrupt:
        # Ctrl-C after the push began is the WORST moment to take the outer
        # handler's word for it and print "nothing further was changed": the
        # hub is fast-forwarded and satellites may already carry new code.
        say("\n\n❌ interrupted mid-update — rolling back rather than leaving "
            "the fleet split")
        rollback(root, old, sats, a.ssh_key, a.hub_url, machines, snapshot,
                 touched=touched)
        return 130
    except Exception as e:
        say("\n❌ unexpected error: %r" % (e,))
        rollback(root, old, sats, a.ssh_key, a.hub_url, machines, snapshot,
                 touched=touched)
        return 2

    rc, out = git(["rev-parse", "HEAD"], root, quiet=True)
    say("\n✅ Updated to %s. Floor is back." % out.strip()[:12])
    if snapshot:
        say("   Your data snapshot is kept at %s" % snapshot)
    say("   If a kiosk/tablet is showing the UI, hard-reload it — the browser "
        "will be holding the old page.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        say("\n❌ %s" % e)
        sys.exit(1)
    except KeyboardInterrupt:
        say("\nInterrupted — nothing further was changed.")
        sys.exit(130)
