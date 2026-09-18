# Changelog

## v0.3.1
- **A machine whose clock runs ahead of the hub can now join.** Such a
  machine treated every request from the hub as already expired (the
  "Time-to-live Expired" refusal), including the one that completes the
  handshake, so it sat at "waiting to join" forever. Found on a Bally
  Alpha 2 Pro Curve running 70 seconds fast. The hub now measures the
  lead from the machine's own timestamps and gives its requests that much
  extra time to live; once the machine has joined, the existing clock
  sync sets its clock right and the extra time is no longer needed.
  Machines running behind the hub were never affected.

## v0.3.0
- **Log in with a PIN, no card needed.** A machine's player screen now
  shows ENTER PIN next to "tap your fob": type your PIN on the keypad and
  you are logged in exactly as if you had tapped your card (wallet,
  cash-out, log out, hide window). Give a player a PIN on the Players
  tab (Set PIN; change or clear it there too). PINs are 6 digits by
  default, never shown again once set, and no two players can share one.
- **The player screen on a SMIB takes a PIN too** — the screen beside a
  SAS machine, which has no card reader of its own. The button sits under
  "tap your fob" there as well, uses the same keypad with the digits
  masked, and logs the player in exactly as a fob tap does.
- **Settings ▸ Gameroom ▸ PIN login**: allow 4-digit PINs instead of 6,
  and choose whether an admin account that logs in by PIN gets the admin
  menu. Off by default: a PIN typed on a public keypad is easy to watch,
  so admin stays card-only unless you switch it on.
- Guessing is throttled per machine: five wrong PINs in a row lock that
  machine's keypad for a minute, doubling each time it happens again. A
  right PIN clears it. Wrong attempts are in the hub log; the PIN itself
  never is.
- The ENTER PIN button only appears once at least one player has a PIN.
- **The SMIB player screen now fits a wide, low panel** such as a
  1280x400 strip display in a player-tracking bracket. Everything moves up
  into the top of the screen: name and balance in a column on the left,
  the menu as a grid of buttons beside it, and the amount keypad as two
  rows instead of a tall tower. A normal 1024x600 screen is unchanged and
  needs no setting: the screen picks the right layout for the panel it
  runs on.

## v0.2.0
- **The on-glass player screen now fits wide, low windows** such as the
  service window at the bottom of an IGT CrystalCurve (840×292). The hub
  passes the machine's own description of the window to the screen and
  it lays itself out as a wide band: balance and greeting on the left,
  buttons in a grid on the right, a two-row keypad. Machines with the
  tall side window (the AVP) are untouched.
- **Currency is now a setting.** Settings ▸ Gameroom ▸ Money symbol:
  leave it blank and the floor follows the machines' own currency
  (a euro machine shows €), or type a symbol to force one. Every amount
  on every screen follows it — the hub UI, the player glass, the SMIB
  screen, and the wording of lock and handpay messages.
- Machine info on the player screen now shows the window size and
  layout it is using, which helps when a screen looks wrong.
- **HIDE WINDOW on the player screen.** A carded player can now put the
  game back to full screen without logging out: the new HIDE WINDOW
  button (next to LOG OUT) hides the service window while the session
  stays open, so everything tracked against it (balance, points, the
  running game) continues. Until now a second tap of the fob was the
  only way to get the menu off the screen, and that logged the player
  out.
- **Tapping the same fob again brings the hidden menu back** instead of
  logging out. A tap on a menu that is still on screen keeps its old
  meaning (log out), and a different fob still switches players.
- Admin fobs, which see nine buttons, get a denser grid so the extra
  button still fits on both the tall side window and the wide bottom
  band.
- **The SERVICE button now works every time on IGT's Windows-era cabinets**
  (CrystalCurve, CrystalSlant). Those machines only tell the hub when the
  service lamp goes on or off, not when the button is pressed. With the
  operator setting "Application handles service button" on YES the lamp
  came on at the first press and stayed on, so the button opened the menu
  once and then went dead. Set it to **NO** on these cabinets (see
  deploy/AVP_SETUP.md): the lamp then toggles with every press and the hub
  opens or closes the menu on both the "lamp on" and the "lamp off" report.
  The original AVP keeps working with YES.
- Two presses close together are still treated as one, but a press that
  flips the lamp the other way always counts, even right after the last one.

## v0.1.0
First explicit version number: the host now reports it (with its platform and
its own address) in the `_engine` block of `/api/status`.

- **New install option: lite mode.** The host can now run inside your
  existing network, behind your own router, instead of owning an isolated
  network segment — so you no longer need a dedicated switch. One extra
  setting on the router (a DHCP option) lets the machines still find the host
  automatically. Full mode is unchanged and stays the default. Guide:
  `deploy/LITE_DEPLOY.md`.
- **New setting for the host's own address** (`--host-base`). Lite installs
  set it once so on-glass pages and the machines' comm settings point at the
  right address; full-mode installs never need to touch it. If a machine
  reaches the host on a different address than the one advertised, the host
  logs a warning once per machine.
- **The satellite Pis (card readers, SAS bridges) now verify the address they
  found automatically really is the host.** If it is not (in a lite install
  they would otherwise be talking to the router), they hold back everything
  that normally goes to the host — status reports and, on the SAS side,
  ticket handling — and leave a clear warning in their journal instead of
  quietly sending data to the wrong device. An explicitly configured hub
  address is trusted as before.
- **The hub can now run on a Windows PC**, in lite mode only (your router
  hands out addresses; see `deploy/WINDOWS_HUB.md`). A launcher in
  `deploy/windows/` finds a suitable Python, reads this PC's address from a
  one-line settings file and starts the hub; the guide covers the firewall
  rule and the router setting.
- On Windows the hub refuses to start with the full-mode network address and
  tells you to pass this PC's own address instead, so it can never advertise
  screens the machines cannot reach.
- Settings ▸ Updates on a Windows hub shows the running version and says
  plainly that updating from the card isn't possible there; the fleet doctor
  card is not offered. A Linux hub keeps everything, including the choice
  between full and lite mode.
- Saving wallets, vouchers and settings on a Windows hub no longer logs a
  false "save FAILED" after every successful save.
- The satellite setup scripts (card readers, SAS bridges) no longer require
  `rsync` on the computer you run them from: without it they copy the files
  over the same SSH connection another way. That is what lets you set up
  satellites straight from a Windows hub's Git Bash.
