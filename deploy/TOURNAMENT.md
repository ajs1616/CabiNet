# Tournament night 🏆

A timed, whole-floor slot tournament for your game room: every machine
becomes a seat, everyone starts with the same credits on the house, and a
wall TV runs the show — countdown, live leaderboard, winner ceremony. It's
all fun-money: the entry credits come from your House bank and whatever's
left goes back to it. Nobody's wallet is ever debited to play.

## What you need

- Machines joined to the host with their **money rails working** — WAT
  transfers on a G2S machine, AFT on a SAS machine (see
  [`DEPLOY.md`](DEPLOY.md) and [`AVP_SETUP.md`](AVP_SETUP.md) for enabling
  those). A machine whose transfers don't work yet sits out with the reason
  shown — fix the rail, re-arm. **A SAS machine needs AFT in BOTH
  directions:** *to-machine* (in-house AFT) so the arm can fund the seat, and
  *from-EGM* ("soft cash-out to host") so the arm can pull any parked glass
  credits home first — a machine holding credits with from-EGM cash-out
  **off** parks its seat `failed` and blocks the whole floor from reaching
  *ready* until it's emptied or that menu option is enabled.
- **Non-penny SAS machines: set the denomination first.** The leaderboard
  scores a SAS seat off its raw credit meter × its denomination; if the
  denomination is unset the hub scores it at **1¢/credit** (right for a penny
  machine, but a `$1`/nickel/quarter box then folds its wins 5×–100× low and
  can't win). The seat shows a `denom unset` flag until you set it — tap the
  machine's name → *Denomination* (whole cents). See the *SAS money-scale*
  note in [`../COMPATIBILITY.md`](../COMPATIBILITY.md).
- **Any TV with a browser** for the spectator board — point it at
  `http://192.168.50.2:8081/board`. Between tournaments it runs an attract
  rotation (now playing, standings, record to beat); during one, it *is* the
  show.
- Players, optionally carded in with their RFID fobs. A carded seat plays
  under the player's own name; an **uncarded seat draws a funny name from
  the roster** (editable — see below).

## Set it up

**Settings → Tournament** on the host web UI:

- **Entry credits ($)** — what every seat starts with, funded from the
  House bank.
- **Duration** — 30 seconds to 120 minutes.
- **Countdown** — 3 to 60 seconds of full-screen drama on the board before
  the horn.
- **Player-name roster** — one name per line; uncarded seats draw from it.
  Empty the box to restore the stock roster.

Reconfiguring while the floor is already armed is allowed, but it only
affects the *next* round — the seats on the glass keep the amount they were
actually funded with.

## Run one

1. **Arm.** Every eligible machine becomes a seat. The host first **clears**
   any leftover credits on the glass (they're returned to the ledger, not
   lost — a carded player's balance goes back to *their* wallet at the end,
   uncarded leftovers go to the House), then **funds** each seat with the
   entry credits, then **locks** it. Transfers never run against a locked
   machine — that ordering is deliberate.
2. **Start.** The board takes over with the full-screen countdown. Machines
   unlock at zero (SAS unlocks are staged about a second early over the
   satellite's report cycle, so a SAS or linked seat can wake a beat before
   the horn — that's normal).
3. **Play.** The leaderboard races on the board and counts **wins only** —
   losses never subtract, and a ticket or handpay mid-run can't corrupt the
   score.
4. **Finish** — when the timer runs out or you press **End**. Machines lock,
   whatever's left on each glass sweeps back to the House, and the board
   crowns the winner (*"…'s buying dinner!"*).

**Cancel** (mid-run bail-out) unlocks the floor first, then sweeps the
credits home. **Reset** clears a finished tournament and returns the board
to its attract show.

## Which machines get seats

Live G2S machines, linked (dual-protocol) cabinets, and SAS machines all
seat. Machines that are offline, stale, parked, dark, or sitting in a
handpay lockup are **skipped with the reason shown** on the Settings card —
nothing sits out silently. A machine holding credits still seats: the arm's
clear stage banks them first.

## Money truth

Entry credits come from — and return to — the **House bank**. Player
wallets are never debited for a tournament; the only wallet movements are
refunds of a carded player's own cleared balance. Every transfer rides the
same once-only ledger paths as normal play, so a hub restart mid-tournament
loses the *scoreboard*, never the money.

## Floor lock

**Settings → Floor lock** is the same per-machine lock machinery without
the tournament: one switch locks or unlocks every machine on the floor
(linked cabinets over their SAS leg, pure-G2S cabinets over G2S). It's
refused while a tournament is anywhere but idle — the tournament owns the
floor's locks then. The reply lists the per-machine outcome, and machines
that can't take the command are named, never silently skipped.
