# Changelog

## v0.3.33
- **Wi-Fi in Settings.** A new card puts the hub on your house Wi-Fi so
  a phone on the couch can open these pages: the networks the hub can
  hear, join with a password (the on-screen keyboard works), forget. A
  Raspberry Pi keeps its radio off until it knows its country; the card
  asks. The machines stay on the hub's cable either way. Hidden on a hub
  with no Wi-Fi radio. Re-run `sudo ./deploy/hub_setup.sh` once on an
  existing hub if the card says it is not allowed to change the Wi-Fi.
- **A ready-to-flash hub image.** `deploy/image/` builds an SD card
  image from Raspberry Pi OS Lite (64-bit): flash it, put it in a Pi
  with its Ethernet on the slot switch, and it sets itself up as the hub
  on first boot, with the touch kiosk when a screen is attached. No
  typing on the Pi and no internet needed; Wi-Fi is then set from the
  card above. Publishing a release builds the image and attaches it.
  First boot keeps its own log on the card
  (`/var/log/cabinet-hub-firstboot.log`). Proven on a Pi 4.

## v0.3.32
- **HANDPAY on a companion screen lists every machine, and stays
  current.** An admin's HANDPAY screen showed this machine's handpays,
  read once when it opened, so one on another machine never showed and
  one that had been cleared stayed until you left and came back. It is
  now one card per machine with a handpay pending: the machine, who is
  carded in there and the amount. Tap a card, confirm, and that
  machine's handpay is reset. The list is read again every two seconds.
  A card just reset shows RESET SENT until the machine answers. Only an
  admin carded in normally can reset another machine's handpay.
- **A handpay too big for the credit meter says where it goes.** The
  machine will not put it on the meter, so the reset pays it by hand and
  the hub books it to the wallet of whoever is carded in there (the
  House when nobody is). Its card reads TAP TO PAY TO WALLET and the
  confirm names the wallet.
- **MY WALLET opens on your last amount.** The amount a player last
  added from their wallet is kept with their account, and both screens
  (the companion screen and the IGT glass) open on it, on any machine,
  instead of the middle preset.
- **Activity says how a handpay ended.** The machine sends the same
  event however a handpay ends, so the feed read "Handpay KEYED OFF"
  for all of them. It now reads "Handpay reset to credits", "Handpay
  paid by hand · attendant key" and so on, with the amount.

## v0.3.31
- **The three preset amounts on a player's MY WALLET are yours to set**:
  Settings, Host options, under the admin's quick amounts. They start as
  $1, $5 and $20, and follow the same rules as the admin's (whole
  amounts; large ones show short on the chip).
- **A machine whose own card reader device is disabled is no longer sent
  cards.** The hub pushes each card-in and card-out to the machine's own
  card reader device. A machine with no reader fitted (an IGT AVP here)
  refuses every one, "Command Not Processed, Device Disabled", and the
  hub logged a warning each time. After the first refusal the hub stops
  sending that device cards until the machine signs on again or says the
  device is enabled, and says so once, as information. Nothing changes
  for the player: the card session, the wallet and the glass are the
  hub's own.

## v0.3.30
New admin menus on the machines' screens. The old admin buttons were
mostly read-only information nobody uses on the floor.
- **Removed from both screens:** MACHINE, TICKETS, GAMES and SETTINGS.
  They are still on the web pages.
- **An admin's fob tapped over a carded friend opens SERVICE for that
  friend**: their wallet, Free Slot Play and the credits on the machine,
  with CLEAR HANDPAY (when one is pending, with its amount), ADD TO
  WALLET, ADD FREE SLOT PLAY, SET PIN and DONE. The hub takes the friend
  from the machine's card session, so a tap-over cannot reach anyone
  else, and it cannot add to the admin's own wallet.
- **An admin carded in on a companion screen gets an ADMIN button**:
  HANDPAY, PLAYERS and NEW PLAYER. A player's page has ADD FUNDS, TAKE
  BACK, ADD FREE SLOT PLAY, SET PIN, CLEAR PIN, ADD FOB, REMOVE FOB and
  RENAME. Names are typed on an on-screen keyboard.
- **ADD FOB**: the screen waits 20 seconds for the new fob to be tapped
  on that machine's reader and links it to the player. That tap cards
  nobody in. A fob that belongs to another player is refused.
- **The digital glass stays basic**: an admin there has their own wallet
  buttons, HANDPAY, HIDE WINDOW and LOG OUT, and the SERVICE screen on a
  tap-over. It has no keyboard and no player management.
- Money added from a screen comes from the House, as on the Players web
  page, and the ledger note names the admin and the machine. There is no
  house limit on an amount: the keypad takes anything up to
  $99,999,999.99. A repeated tap cannot pay twice.
- **The three quick amounts on the admin's add pad are yours to set**:
  Settings, Host options, under the PIN switches. They start as $20, $50
  and $100. Large ones show short on the chip (25K, 2.5M).
- Making someone an admin and deleting a player stay on the web page.
- **A PIN typed at a keypad, or set on the Players web page, is no longer
  written to the hub's wire log**, which support bundles copy. Logs and
  bundles made before this version may still hold PINs.

## v0.3.29
CASH OUT TO WALLET on a machine's screen waits for a machine that is
busy. A machine in the middle of a game, or just coming out of a handpay,
does not lock for a transfer at that moment. The pull was tried once and
failed about a second after the tap.
- **The satellite keeps the pull and tries again every second, for up to
  fifteen seconds.** Nothing is held meanwhile: the machine is not locked
  between tries and the satellite goes on polling it as usual. The
  credits move on the first try the machine accepts.
- **The screen says it is waiting** ("Sending to your wallet as soon as
  the machine is ready.") and the button rests until there is an answer.
  A second tap in that time sends nothing more.
- A machine that stays busy for the whole fifteen seconds: the player is
  told nothing was sent.
- **A player who cards out while it is waiting takes the request with
  them.** The satellite stops trying, so nothing of the next player's is
  pulled. A try already under way at that moment is credited to the
  player who asked.
- The credits of a pull asked for from the screen go to the player who
  asked, whatever the machine's own CASH OUT button is set to by then.
- A status read the machine did not answer is no longer reported as "no
  cashable credits".
- Tournament sweeps are tried once, as before.

Needs the satellite's own update, which the hub sends after its own.
Checked against a stand-in machine that will not lock: no machine on
the test floor has turned a pull down for being busy (ten pulls on a
Bluebird 2, all taken on the first try).

## v0.3.28
A handpay on a SAS machine has an amount, and is booked when it is paid
by hand. Until now a SAS machine told the hub only that a handpay was
pending: a machine with no G2S side showed no amount and booked nothing.
- **The satellite reads the machine's handpay record** (what it was
  for, the amount, whether the machine can put it on the credit meter)
  and reports it with the pending handpay. The Floor's HANDPAY banner
  and the Gameroom Board show the amount on a SAS machine.
- **How a handpay ended is reported**: put on the credit meter (by the
  hub's reset or the machine's reset-to-credit key), paid by hand (the
  handpay key), cancelled by the player, or never there. SAS does not
  name the key; the machine says it by where it counts the win: a
  reset to the credit meter is not added to its jackpot meter, a
  handpay paid by the attendant is. The satellite reads both meters a
  few seconds after the reset. If neither moved by the win it says so
  and nothing is booked.
- **A handpay cleared with the attendant key lands in the wallet of the
  player who is carded in at the machine.** If no player is carded in
  it goes to the House. Once, however many times it is reported. The
  same rule a G2S machine's hand-paid key-off already had.
- **A machine with both sides is booked from its SAS side.** Its G2S
  key-off is then information only. With a satellite older than this,
  which reports no record, the G2S key-off books it as before.
- A handpay too large for the machine's credit meter says so on the
  banner.
- **The admin handpay view on the machine's own screen lists a SAS
  machine's handpay from its SAS record**, and its CLEAR goes over SAS
  whether or not the machine has a G2S side. It read the G2S record
  only, so a SAS-only machine listed nothing there.
- A bonus sent to a linked machine is sized by the same denomination
  as its credit meter (a cent while none is set for it).
- **CASH OUT TO WALLET on a machine's screen is no longer refused on an
  old status read.** The hub went by the machine's last "can a transfer
  come off it" reading, up to 30 seconds old, and a reading from a bad
  moment answered "can't send its credits to the wallet right now"
  about a machine that could. The pull is now sent whenever the SAS
  side is reporting, and the satellite reads the machine live. If the
  machine does not make it, the reason shows under the button in both
  menus for half a minute; before, the tap answered "pending" and a
  failure said nothing.
- A refused action from a machine's screen is logged with what the
  player was told.
Needs the satellite's own update, which the hub sends after its own.

## v0.3.27
A machine with a SAS companion takes its commands over SAS. G2S on it is
for reading: events, meters, status, the handpay record.
- **Nothing is sent to it over G2S by the hub itself** beyond what keeps
  the G2S link up and reporting. At a sign-on the hub no longer changes
  its options (the remote handpay reset switch, the ticket header, the
  ticket expiry: the SAS side already sets the ticket text and expiry,
  and keeps the clock), sets its clock, or reads a transfer device it
  does not have. At a card-in it no longer sends the card or the glass
  window.
- **A command SAS can carry goes over SAS whichever button asked.** A
  handpay reset from any page or from the admin view on the machine's
  screen, enable and disable, and a bonus (as whole credits) are sent
  to the satellite. Pushes, cash-outs, the floor lock and tournaments
  already were.
- **A G2S command on something SAS owns there is refused, and says what
  does the job**: the cash-out switch, the transfer device, the ticket
  device, the clock.
- A ticket header or expiry saved in Settings is not pushed to it over
  G2S; the answer says SAS has it.
- Left on G2S, when someone asks: a reboot, an Options-tab change, the
  Games tab, the bill acceptor switch.
- An enable also lifts an old G2S disable if the machine still reports
  one. That is the one G2S command this rule sends.

## v0.3.26
Seventh and last batch from the G2S audit: the card reader, the bill
acceptor, bonus awards, handpays and transfers. Sixteen items and seven
comment corrections. Nearly all of it is for cases the AVP and the
Bluebird 2 have never produced; nothing here makes the hub send a
machine a command it did not send before.
- **A bonus is taken from the House once per machine, transaction and
  award.** It was keyed on the transaction number alone, which each
  machine counts from 1: a second machine's award under a number the
  first had used was paid on the machine, logged as debited, and never
  taken from the House.
- **A failed bonus says why**, in the document's words (machine not
  playable, pay method not available, ...), and the command API accepts
  all five pay methods (credit-meter-or-fail and pay-to-account were
  missing).
- **With several handpays waiting, Reset takes the cashable one first**,
  then promotional, then non-cashable, the order a machine may insist
  on. Lockups read back from a machine's log after a hub restart are
  held oldest first, as live ones are; they were held newest first and
  the two reset actions then worked on each other's lockup.
- **A refused key-off type is explained for both kinds of machine.** The
  AVP goes by its options at the time, so arming the option and trying
  again works there; a machine that goes by what the lockup offered
  when it was raised needs the lockup cleared at the machine.
- **What a machine says about its card reader is kept and shown**
  (`idReader` in /api/status), with its events named. It was only
  logged. Nothing is decided on it: a card session is the hub's own.
- **The card push uses the reader the machine says the hub owns**, the
  cabinet's own locale, and the lengths the fields have (playerId 16,
  localeId 10).
- **A machine's own card-validation request is answered "command not
  supported"**, no longer "class not supported".
- **A bill acceptor that disables itself when the hub is quiet is
  named**, with the time, when its profile is read.
- For a machine that runs its own transfers: an overdrawn House is given
  as 0, an account name is cut to 64 characters, a withdrawal is capped
  at the balance, and a key asked for under a real algorithm is refused
  as not supported.
- Settled without a change: the bonus award keeps the attribute list
  the AVP accepted (the document does not list one of them); no card
  state is re-sent by the hub at a sign-on; a transfer request written
  off when a machine reports a reset stays written off.
- Comment corrections (wrong section numbers, and what the code no
  longer does).

## v0.3.25
Two things seen on the floor.
- **TO WALLET reads ON only when it is on.** On a G2S machine the
  player's screen took the machine's own setting as the answer. The
  machine keeps that setting; the hub keeps whose wallet it is for only
  until it restarts. After a hub restart the screen read TO WALLET ON
  with no wallet behind it, and CASH OUT asked the hub, was refused and
  printed a ticket a few seconds later. The screen now reads ON only
  while this player's wallet is set to receive the cash-out and the
  machine has taken the setting. A machine found sending its cash-out
  to the hub with no wallet set is put back to printing a ticket, once
  per sign-on. A machine with a SAS link is not touched.
- **Amounts stay on the player's screen.** On the digital glass the
  keypad figure, the wallet balance, the amount button and the two
  credit lines are measured and scaled to their space, so a long amount
  gets smaller as it grows where before it ran off the edge (the keypad
  from about $1,000 up on the AVP). On the Bluebird's screen the keypad
  figure steps down from $100,000.

## v0.3.24
Sixth batch from the G2S audit: the cabinet and events. Eleven items,
one of them (a lock and a machine restart) settled with no change: a
machine locked from the hub stays locked until the hub unlocks it, and
the hub sends no lock or unlock by itself.
- **The hub is not left with no event subscription.** At each sign-on
  it clears its subscriptions and sets the list it wants. If the
  machine refused that list, or never acknowledged it, the hub held
  none and went on showing "subscribed". It now asks for everything
  instead, and shows "not subscribed" in between.
- **Game started and game ended are no longer kept by the machine until
  acknowledged.** A machine holds only so many unsent events for the
  hub (the document's minimum is 35; the AVP and the Bluebird 2 both
  say 100). With the hub off and a machine being played, those two
  filled it in about fifty games, and the machine then dropped events
  or switched its event handler off. The hub also now
  says when a machine reports that its event log is full, and reads the
  log back when it clears.
- **An event first seen in the machine's log is still handled when the
  machine sends it.** It was taken for a repeat: a bill stacked while
  the hub was away could miss the count.
- **A service-button press delivered late is not acted on.** A press
  made while the hub was unreachable arrived when it came back and
  opened or closed the player menu then.
- **The hub refuses to restart a machine while it knows something is in
  progress on it**: a handpay waiting, a transfer, an option or
  denomination change. The command API can insist.
- Commands go to the cabinet and handpay devices the machine marks as
  active, and to the event and meter devices numbered as the hub is,
  not always to number 1.
- A lock text is cut to the 256 characters a machine accepts.
- A door found open when the power came back is filed as a door
  opening, not as a power loss.
- A partial lock the machine did not apply is no longer shown as
  confirmed, and one that was lifted no longer goes on showing.

## v0.3.23
Ticket expiry, kept by the hub.
- **A ticket is good through the date on it and no longer.** The date is
  the day it was printed plus the hub's expiry days at that moment. It
  is refused from the next midnight. There is no grace period. Before,
  the hub never refused a ticket for its age: a ticket from its own
  ledger stayed good for 30 days past its date and then until the
  ledger happened to be tidied, and a ticket a G2S machine printed had
  no expiry at all.
- **Tickets a G2S machine prints are held to the hub's days too.** With
  the hub now setting each machine's expiry at every join (v0.3.22),
  that is the date on the paper.
- **A nightly job at 12:01 am** logs how many tickets went past their
  date at midnight and removes tickets that expired more than 30 days
  ago. It decides nothing: a ticket is judged by its own date at the
  moment it is presented, so a hub that was off overnight loses
  nothing. A hub that was off at 12:01 runs the job when it comes up.
- A G2S machine is told "expired" for such a ticket; a SAS machine gets
  the plain rejection it gets for any refused ticket.
- The Tickets page shows the date a ticket is good through.
- Tickets printed before this version, and tickets printed while the
  hub is set to "never", carry no date and never expire. Reissuing a
  ticket, or making it reusable, lifts its expiry as before.

## v0.3.22
Fifth batch from the G2S audit: the rest of the ticket items. Seven
items.
- **Ticket printing is not left switched off.** When a machine holds
  ticket numbers the hub did not issue, the hub switches its ticket
  printing off and on again so that it asks for new ones. If the "on"
  was lost, printing stayed off, and on some machines the whole machine
  with it, until someone sent the command by hand. The hub now keeps a
  note on disk from the "off" until it has seen the machine say "on",
  and until then asks the machine and sends the "on" again. A hub
  restart between the two no longer loses it.
- **Ticket header text has the room a ticket has.** The name and each
  address line take 40 characters and the title 16, on both protocols.
  The hub accepted 64 for each; a longer line made a strict G2S machine
  refuse the whole header. Settings now holds each field to its limit,
  and a header saved under the old limit is sent as far as it fits.
- **"Never expires" is not sent to a G2S machine as 0 days.** The
  setting is a number of days and 0 means, read as written, "expires
  the day it is printed". For "never" the hub sets the most days the
  machine says it allows; where the machine has not said, its own
  setting is left as it is. A number of days is sent as before.
- **The ticket expiry is set on a machine every time it joins**, the
  way the header already was. It used to go out only when the setting
  was saved, so a machine that had been re-imaged or memory-cleared
  went back to its own number of days and stayed there. This applies
  when the hub is set to a number of days; "never" is still sent only
  on Save. An expiry saved together with a header change is now held
  until the header change is done, then sent; it used to be refused and
  dropped.
- **A ticket's own expiry date is used only when the machine says it
  applies.** A machine can print a date on a ticket record that means
  nothing unless a second flag is set; the hub enforced it regardless.
- **A ticket refused because the hub's ledger could not be read is
  reported to the machine as "denied"**, not as "ticket not found".
- **A failed print is not shown as printed**, and a good print reported
  in the short form is not shown as failed.
- A ticket log entry the machine has not finished with is no longer
  copied into the hub's history beside the ticket's own record.

## v0.3.21
Fourth batch from the G2S audit: meters and game play. Eight items.
- **The credit on a machine is all three kinds of credit.** A machine
  keeps cashable, promotional and restricted credit on separate meters,
  and what the player sees is the total. The hub looked at the cashable
  meter alone. A machine holding only restricted or promotional credit
  read as empty: the tournament's clear step passed it and put the stake
  on top, and the figure on the machine's own screen under-read. A pull
  now asks for each kind and credits each as what it is. This is for
  machines whose money moves over G2S; a machine with a SAS link is
  unchanged.
- **A denomination range is read as the denominations in it.** A machine
  may list its denominations as a range (1¢ to 5¢, say) instead of one by
  one. The hub kept ranges aside and showed nothing for them, and a
  denomination change sent only the single entries, which switched off
  every active denomination in the range. A range the hub cannot list is
  said, and a change for that game is refused.
- **An empty machine is confirmed by the answer to the hub's own
  question.** Before funding a tournament seat the hub asks the machine
  for its credit meters. Any meter report arriving afterwards was taken
  as the answer, including a routine report sent before the question and
  an answer about other meters.
- **Breakdown meters no longer replace a device's totals.** A meter
  report can also carry figures per wager category, per denomination and
  per currency under the same names as the totals. They were stored over
  the totals. They are now left out.
- **A game whose paytable or settings change is read again.** The hub
  read a game's profile once and kept it for good. After a change at the
  machine the Games page showed the new paytable with the old payback
  and maximum bet.
- **When a machine's game is replaced, all of its entries are read
  again.** On a single-game machine the entries on the Games page are
  that game's paytables. Only the one the machine is set to reports
  anything, so the others kept the old game's name: a Bluebird running
  Dr. Jackpot still listed eleven Wizard of Oz II paytables four months
  after the change. The hub does this by itself at most three times a
  day per machine. A player moving between the games of a multi-game
  machine does not trigger it.
- **Yes/no values written 1 and 0 are understood**, as well as true and
  false, everywhere the hub reads one. A machine that writes them that
  way could be left in a configuration mode, have every denomination
  shown as off, and be sent the same handpay settings at every join.
- A machine can mark one entry on its Games page (a game, or one
  paytable of its game) as one it will not run without. Disabling that
  entry takes the whole machine out of play (still powered, shown as
  disabled). The Games page now says so on that entry's Disable button.
  For every other entry Disable only makes it unavailable, as before.
- A machine whose currency does not have two decimal places (yen, for
  one) gets a note on its tile saying its amounts are off and by how
  much; the hub does not rescale them, and does not put that currency's
  sign in front of them.
- The join's log line for the handpay settings names what was sent.

## v0.3.20
Third batch from the G2S audit: configuration changes. Eight items.
- **A machine is not left in a configuration mode.** While a machine is
  in one it cannot be played. The hub could leave it there when a
  denomination change got no answer to its write, when the command to
  leave the mode was refused or lost, when the machine entered the mode
  later than it first answered, or when the hub restarted part-way. The
  hub now gives the write a deadline, sends the exit again until the
  machine confirms it, notices a machine that has entered a mode nothing
  is driving and takes it out, and checks for this at every join. A mode
  entered by hand through the command API is left to whoever entered it.
- **The hub only changes the options a machine allows a host to
  change.** It reads which ones those are before changing anything. A
  WMS Bluebird 2 keeps two of the handpay settings to itself, so the
  hub's change was refused whole at every join, the settings it does
  allow included. They are now left as the machine has them.
- **The two things the hub re-applies at every join each wait for the
  machine's own answer.** The ticket header and the remote handpay
  reset are checked against the machine at join and sent again if a
  memory clear has put them back. Each could be judged on the answer
  to the other's question, using what the hub remembered from an
  earlier join: a handpay setting that a memory clear had switched off
  could be taken as still on, and the Bluebird was still sent the two
  settings it keeps to itself. If a machine refuses the fuller read the
  hub now makes, it is read the way earlier versions read it.
- **An option change the hub gives up on is cancelled on the machine**
  instead of being left there waiting. One the machine has accepted and
  is waiting to apply is no longer shown as timed out or replaced.
- **Claiming ownership of a machine's devices builds on what the machine
  already holds for this hub.** The claim sent the hub's own idea of its
  address, reset the host's settings to defaults, marked every device
  active and could drop devices. It now sends back what the machine
  reported, refuses to run before that has been read, and does not claim
  a class the hub does not serve (progressive, player, central).
- A host list the machine sends unasked updates the entries it carries
  and no longer replaces the whole table.

## v0.3.19
Second batch from the G2S audit: how the hub reads, answers and sends
G2S messages. Nine items.
- **A message with `&`, `<` or `"` in it is no longer refused.** A game
  name, ticket text or option description containing one of those made
  the hub answer "malformed" and drop the whole message.
- **A machine's refusal is charged to the request it refuses.** The hub
  used to pin a refusal on whatever it had in flight for that kind of
  thing. A machine refusing a status read could mark a wallet transfer
  as failed, stop a ticket-header change, or abandon a denomination
  change after the machine had been asked to lock, leaving it locked. A
  refused bonus award now shows as refused instead of staying "sent".
- **A machine the hub cannot reach is told to start over.** If the hub
  could not send to a machine, for example after the machine's address
  changed, it kept accepting what the machine sent and answered into
  nowhere. It now tells the machine communications are not established,
  which makes the machine sign on again with its current address.
- **A message that did not go out is sent again.** When a machine's
  listener refused the connection, the hub dropped that message for
  good. It now tries twice more. A message that went out and drew no
  answer is not repeated, since the machine may have acted on it.
- **A machine that says its queue is full is sent the message again**
  after a pause, up to five times, instead of losing it.
- **A long device list sent in several parts is kept whole.** The hub
  kept only the last part.
- A repeated answer to a repeated request is marked as a repeat.
- A machine set up with the hub under a different host number is still
  let in, and the hub's log and status now say which number it uses.

## v0.3.18
First batch from an audit of the G2S host against the G2S Message
Protocol 1.0.3 document: the four items where money or a locked machine
is at stake. None shows in ordinary play on the machines this has run
on; each needs an uncommon event.
- **A ticket printed after a machine's memory was cleared is no longer
  lost.** A cleared machine numbers its transactions from 1 again. The
  hub recognised a repeat by that number alone, so a new ticket that
  landed on an old number was acknowledged and not recorded, and was
  refused when it came back. The same went for the end of a redemption
  and for a handpay: a new lockup could be acknowledged and never appear
  on the hub. A repeat is now the same number and the same ticket (or
  the same moment, for a handpay).
- **A machine holding ticket numbers this hub did not issue is given new
  ones at once.** After the hub's ticket store has been replaced, or with
  a machine that last ran on another hub, the machine's stored numbers
  are no good here. When such a machine asked for no new numbers, the hub
  left the old ones in place, and the tickets printed from them were
  refused later.
- **Resetting a handpay asks for the right amount when the machine paid
  part of the win itself.** The hub took the machine-paid part off the
  amount a second time. A win of 2,000 with 1,200 paid to the meter
  showed a lockup of 0, and the reset was refused. With nothing
  machine-paid, which is every handpay seen so far, nothing changes.
- **A wallet cash-out cannot be credited twice after a long absence.** A
  machine keeps sending the end of a transfer until the hub's answer
  reaches it. If it then stayed off the network while the rest of the
  floor was busy, the hub could forget the transfer and credit the
  wallet again when the machine returned. The hub now notes on the
  transfer itself that its money has moved, and keeps each machine's
  transfers apart.

## v0.3.17
The last four items of the SAS audit. With these it is complete.
- **The machine is polled while the hub decides on a ticket.** When a
  ticket goes in, or a cash-out asks for a ticket number, the satellite
  asks the hub and waits. It sent the machine nothing while it waited. A
  machine that is not being polled takes the link for down and hands the
  ticket back, or refuses the cash-out. The satellite now keeps polling
  through the wait. With a hub that answers at once, as it does on the
  same network, nothing changes on the wire.
- **Transfers no longer depend on registering the machine.** The
  satellite used to register every machine and send the registration key
  with each transfer, and a machine where the registration would not
  complete got no transfer at all. The SAS document says registration is
  for bank debit transfers and that transfers like ours should carry a
  key of all zeros. They now do, and nothing is registered first. A
  registered WMS Bluebird 2 was seen to take a push that way.
- **A machine that refuses the zeros is sent its own key.** Some older
  machines are said to fail on the zeros. A refused transfer moves
  nothing, so the satellite sends the same transfer once more with the
  machine's key (registering the machine first when its answer was "not
  registered" or "key does not match"). If the key is what made it go
  through, that machine is sent its key from then on, as before this
  version. The hub still registers a machine when it joins, as it always
  has, so the key is there if it is needed; the tile shows AFT as ready
  as soon as the asset number is known, without waiting for that. A
  machine with no asset number set is still refused, with the same
  message.
- **Every machine event has a name.** 51 event codes showed in the log and
  on the hub as a bare number, such as "exception 0x7E" for every game
  start. All of them are named now, with the bill value on each bill
  event ($2, $50, $100, $200, $500 were missing).
- Event names no longer carry a "(VERIFY_ON_BENCH)" tag. The tag was
  showing in the hub's event list, and it cut two names short.
- A machine set to an address that is also an event code (81 is "handpay
  pending") has that event read as an event. This was fixed in v0.3.13
  and is now covered by a test.
- A transfer's result says which key it carried (`keySent`).

## v0.3.16
Seventh and last batch from the SAS audit: meters, denominations and the
remainder. Twelve smaller items.
- **A ticket's issue time is the satellite's own clock.** It was the date
  inside the machine's ticket record, which is the machine's clock and is
  wrong after a RAM clear or with a dead clock battery. The hub works
  ticket expiry from the issue time, so a ticket stamped years back could
  be removed as expired the same day it printed. The machine's date is
  kept with the ticket for the record.
- **The machine's clock is kept in step.** The satellite reads the
  machine's date and time when it comes online and once a day, and sets
  it when it is more than five seconds off. That is what the machine
  prints on tickets and writes in its own logs, and it is wrong after a
  RAM clear. The satellite only sets it when its own clock comes from a
  time source (NTP); a Pi that started without one leaves the machine
  alone and says so in its log. A machine kept right some other way,
  such as over G2S, just reads as in step.
- **The hub offers the right denomination for every machine.** A SAS
  machine reports its accounting denomination as a code. The hub only
  knew one code (1 cent), so any other machine showed a bare code for the
  operator to work out. It now knows the document's whole table and
  offers the value ("25 cents") to confirm. It is still only offered: the
  operator's setting is what money is worked from.
- **Each machine gets its own validation ID.** Every machine was seeded
  with the same one, so two machines' ticket numbers were kept apart only
  by when they were seeded. The ID is now worked out from the satellite
  and the machine's address, and the starting sequence is sent in the
  byte order the document gives. This takes effect the next time a
  machine is seeded (after a RAM clear, or from Settings).
- **A call to the hub for a ticket decision has one time limit.** The
  2.5 seconds applied to each step of the call, so a slow hub could hold
  the machine unpolled for five seconds or more, at which point it hands
  the ticket back. It now applies to the whole call.
- **The multi-denomination answer is checked before it is used**: its
  length, its checksum, and that it is for the denomination asked for.
- **A lock request that got no answer is cancelled.** It was only
  cancelled when an answer had been read, so a machine that had put the
  request on hold could lock itself later with no transfer coming.
- Answers that carry more bytes than expected are read, not refused (AFT
  registration, validation status, enabled games), as the document asks.
- Turning a game off reports which games are left, and says so when none
  is. The per-denomination form labels a game number of 0000 as the
  experiment it is.
- The extended meter read keeps "not supported" apart from zero and
  takes twelve codes; the jackpot meter has a parser; the enabled
  features poll can no longer be built without its game number; an
  unregister poll carries the code alone.

## v0.3.15
Sixth batch from the SAS audit: lock, unlock and handpay reset report
what the machine answered.
- **A lock or unlock is only recorded when the machine acknowledges it.**
  Anything that came back counted as done, and so did nothing coming back:
  a lock the machine refused, put off because it was busy, or never heard
  was shown as locked on the hub. The satellite now reads the answer. A
  busy machine is asked again (three tries); a refusal or silence is
  reported as such and the hub's lock state does not change. The chip on
  the machine's tile goes amber with the reason.
- **Locking reports what happened to the credits.** A machine is allowed
  to cash out its credits when it is locked. The credit meter is read
  before and after, both figures are in the result, and a drop is called
  out.
- **A handpay reset the machine confirms is a reset.** The hub's reset
  was reported as a timeout, and the HANDPAY banner stayed, unless the
  machine's follow-up notice arrived within about a second. The machine's
  own answer "handpay was reset" is now the result. If a handpay notice
  arrives while waiting, its record is read, which is what lets the
  machine send that follow-up.
- **The HANDPAY banner clears when it should.** It only cleared on the
  machine's "handpay was reset" notice. It now also clears when the
  player cancels the handpay request, and when the machine answers a
  reset with "not in a handpay", which until now left the banner up until
  the satellite restarted.
- **A stray byte is not taken for consent to reset.** A lone address
  byte, which is what a machine that cannot hear the hub sends unasked,
  was accepted as the answer to both steps of a reset.
- "Not in a handpay" from the second step of a reset is reported as that,
  not as "method refused".
- **A busy or refused answer is acknowledged before the command is sent
  again.** Sent again with nothing between, a strict machine takes the
  retry as "say that again", repeats its busy, and then stops answering.
  A busy machine is also no longer asked twice more on the spot when a
  record is being read.
- Comments that called the handpay and bonus polls guesses now cite the
  document's tables.

## v0.3.14
Fifth batch from the SAS audit: the machine's own CASH OUT button to a
wallet, on SAS machines.
There are two ways credits leave a SAS machine for a wallet. The hub can
pull them (CASH OUT TO WALLET on the player screen); that is unchanged. Or
the machine's CASH OUT button can send them, which depends on the
machine's "cashout to host" option. This release is about the second.
- **Set the machine's cashout to host option to HOST CONTROLLED.** The
  docs used to say "soft cash-out to host". Host controlled leaves the
  on/off to the hub: the button prints a ticket until a carded player
  switches it to their wallet, and prints a ticket again when they switch
  it back or card out. `SAS/BENCH_AFT_CASHOUT.md` is rewritten around it.
- **The SMIB player screen can switch the button.** The CASH OUT page has
  a new switch, MACHINE CASH OUT BUTTON: TO WALLET ON / OFF, under the
  existing CASH OUT TO WALLET button (which still sends the credits at
  once). The digital glass already had the switch.
- **Arming looks at the right setting.** The hub decided whether a
  machine's button could go to the wallet from a bit that only says the
  hub could pull credits. A machine with cashout to host off was armed,
  the page said cash-out goes to the wallet, and the button printed a
  ticket. The hub now reads the machine's host cashout status (shown in
  the Machines status as `sasHostCashout`), the satellite switches the
  option on and off on a host controlled machine, and where it is off and
  set at the machine the switch is refused with the reason.
- **Every cash-out request from a machine is answered.** On a machine set
  to on (soft or hard) each press asks the hub first and waits up to
  eight seconds. When nobody was armed the satellite said nothing, so the
  player stood through the eight seconds, and a machine in hard mode
  tilted. The satellite now declines at once and the ticket prints.
- **A win the machine wants to send to the host is taken as a win.** It
  was answered as if it were the cash-out button, which a machine has to
  refuse.
- **A refusal from the hub is shown in its own words** on the SMIB player
  screen and the digital glass. Both pages read the hub's answer only when
  it was a yes, so every refusal came out as "try again".
- The refusal for a pull no longer tells the owner to turn on "soft
  cash-out to host". A pull does not need it.
- **A refused AFT registration says why.** It was always reported as
  "possibly awaiting operator acknowledgment at the machine". A machine
  cannot ask for that by itself. The usual cause is the asset number not
  matching the one set at the machine, and the message says so.
- None of the button path has been on a real machine yet; the tests run
  against a pretend machine that follows the document.

## v0.3.13
Fourth batch from the SAS audit: the link rules. This changes the rhythm
on the wire for every SAS machine. A WMS Bluebird 2 took the old rhythm
without complaint; a machine that keeps the document's rules would not.
- **An idle machine is no longer sent the same poll over and over.** While
  nothing was happening the satellite sent one general poll every 200 ms
  and nothing else, about twenty in a row. The document counts the second
  as "send that again" and lets a machine stop answering at the third.
  Every answered poll is now acknowledged (one extra byte, a poll to
  address 0) before the next. A poll that got no answer is still repeated
  as it was, because that repeat is how the machine is asked to send
  again.
- **Polls keep the polling rate.** The meter reads, the exception drain,
  the AFT status read and hub commands went out about 25 ms after the
  previous poll. The document allows 200 ms between polls to one machine,
  or 40 ms where the machine says it supports that. The satellite starts
  at 200 ms and drops to 40 ms once the machine's own answer allows it
  (it says so, or it validates tickets, which requires it). The two cases
  the document exempts are kept: asking again after a damaged answer, and
  answering a priority exception. The Machines status shows the rate in
  use (`pollRateMs`).
- **After a silence the machine gets its sync poll first.** When SAS was
  switched back on in Settings, the status polls in the same pass went
  out before the sync poll, to a machine still ignoring its own address,
  and the hub showed it as not registered for the next thirty seconds.
  The same now holds after any four seconds with nothing sent.
- **A command is not sent to a machine that is not answering.** A machine
  whose receive line is dead sends one stray byte every 200 ms, and that
  byte is also what "done" looks like for a bonus award, a lock or a game
  enable. About one such command in four was reported done on a machine
  that never heard it. The command is refused with the reason.
- **Exception 81 to 8F at the matching address is reported.** A byte
  equal to the machine's address plus 0x80 was dropped as a refusal. It
  is an ordinary exception: at address 2, every entry to the attendant
  menu went unreported. These codes have their names now. If the same
  byte comes on the very next poll again it is counted, not reported
  twice (a Bluebird 2 once sent 81 about twenty times in a row).
- The waits for a handpay reset, a ticket redemption and an AFT transfer
  no longer report "no activity" and stray address bytes as events, and
  the twentieth exception taken in one pass is acknowledged like the
  rest.
- Not tried on a machine that enforces these rules; the tests run against
  a pretend one that does.

## v0.3.12
Restricted promotional credit, end to end. Every player wallet has two
balances: cash and restricted. Restricted credit has to be played and is
never paid out as cash. Operator pages call it "restricted promotional",
the machine's own wording; the player's glass page calls it "Free Slot
Play".
- **Sending it to a machine.** The Wallet page has a new step, "As": cash
  credit or restricted promotional. It comes off the sending account's
  restricted balance, and the bank can give it freely. It works on SAS
  machines (the AFT restricted amount) and on G2S machines, where the hub
  could already move it but the page never offered it.
- **Giving it to a player.** The Players page fund box has a "restricted
  promotional" tick box, for giving and for pulling back.
- **On the player's screen.** On the SMIB player screen and the digital
  glass, a carded player with Free Slot Play sees it on the home page
  under their wallet balance. In MY WALLET the two balances are two
  rows, each with a radio button: tap the one to load from, and the ADD
  button loads that one. A player with none sees the pages as before.
- **The SMIB player screen picks up updates by itself.** That screen
  reloads its page, while nobody is carded in, when the hub says the
  page changed. The hub was only saying so on a poll that screen never
  reads, so after an update it kept the old page until its browser was
  restarted. The signal now goes out on the screen's own poll. A
  balance of 13 characters (such as $1,058,441.00) also no longer loses
  the edge of its last digit on the home page.
- **Cashing it out to the wallet.** A SAS cash-out to the wallet now
  takes the machine's restricted credit along with its cash and puts
  each in its own balance. Before, restricted credit was left on the
  machine.
- **Tickets.** A SAS machine says what each ticket it prints is. A
  restricted promotional ticket (titled PLAYABLE ONLY) is tagged in the
  Cage list and named in its detail, and when it goes back into a
  machine it is credited as restricted credit, on SAS and on G2S.
- The default expiry for restricted tickets is set on the machine along
  with the ordinary one.
- Seen on a Bluebird 2: a $150 restricted push, and at cash-out a normal
  ticket for the cash followed by a PLAYABLE ONLY ticket for the $150.

## v0.3.11
Third batch from the SAS audit, second part.
- **The ticket expiry is also set the way the document says.** It was
  sent only inside the older ticket-data poll. The document has a
  dedicated poll for it whose value wins, so both are sent now.
- **The machine's own word is used to tell whether it still holds our
  ticket settings.** SAS has no poll that reads the ticket header text
  back, but the expiry travels with it and can be read at any time. The
  SAS box notes the expiry the machine reports when the header goes on,
  reads it again every 30 seconds, and sends the header again if it has
  changed. It compares with the machine's own earlier answer, not with
  the hub's setting, so a machine that rounds or caps the value is not
  re-sent the header for ever.

## v0.3.10
Third batch from the SAS audit, first part: a ticket redemption is
followed to its end.
- **A slow ticket no longer stays "pending" for good.** After a machine
  accepts a ticket it stacks it or hands it back, and says so. The SAS box
  waited about two seconds for that and then gave up, so a slower stacker
  left the ticket "redeem pending" on the hub with the player already
  credited, and a ticket handed back could then only be used at that one
  machine. Now the SAS box keeps asking: at once when the machine says
  the ticket is done, otherwise after 2, 5 and then every 15 seconds, and
  before it reads the next ticket. The machine's answer names the ticket,
  so this also closes one left open by a restart.
- **An answer that says nothing about the ticket is no longer read as a
  refusal.** "Not compatible with the current redemption" and "no
  validation information" used to put the ticket back to "issued" while
  the machine might still be stacking it. Only a real refusal does that
  now.
- **Damaged answers during a redemption are asked for again**, and a
  machine that says it is still waiting for our approval is sent it once
  more.
- **Tickets from a machine in standard validation that answers the newer
  read with zeros are captured.** The document allows that answer; such a
  machine never got the older read that holds its ticket number.
- The SAS log no longer says "ticket issued" for a read that found
  nothing.
- **System validation: a lost answer no longer ends the cash-out.** The
  machine asks for a validation number, gives the host ten seconds, and
  stops asking once the host has started. A garbled answer to either of
  the host's two polls used to end it there with no ticket. Both are now
  asked for again.
- **System validation: the machine's reply to the number is read.** It
  can say "not in a cash-out" or "improper validation rejected"; both
  used to be reported as an approval. A refusal now gets one more try and
  is then reported as refused.

## v0.3.9
Second batch from the SAS audit: an AFT transfer always ends in a known
result.
- **A transfer that finishes late is no longer lost.** The SAS box waited
  about three seconds for a transfer's final status, tried one cancel,
  and reported "unconfirmed, verify meters". If the machine finished the
  transfer after that, or while the SAS box was restarting, the credits
  had moved and the hub never heard: a push could be sent a second time,
  and a cash-out left the machine without reaching the wallet. Now the
  transfer is written to disk before it is sent and kept until the
  machine has given a final answer. The SAS box asks again after 2, 5
  and then every 15 seconds, at once when the machine says "transfer
  complete", and first thing after a restart. The hub then gets a second
  result, marked "settled late", under the same transaction number, and
  books it once as it would have the first time. No hub change was
  needed for that.
- **One transfer at a time.** The document forbids starting a transfer
  while an earlier one has no final status, and a machine that keeps it
  refuses the new one and will not lock. A new transfer now waits: it
  comes back as "an earlier transfer has no final status yet" and nothing
  is sent.
- **A plain "no" from the machine reads as "no".** Three short answers
  the document defines (unsupported, not compatible with the transfer in
  progress, no transfer on record) were read as silence, which turned a
  definite refusal into "unconfirmed".
- If the transfer cannot be written to disk first, it is not sent.
- Tested against a pretend machine that holds a transfer open until its
  status is read, including a full run of the SAS box's own program. Not
  yet seen on a real machine: a Bluebird 2 answers within the wait.

## v0.3.8
First batch from an audit of the SAS code against the SAS 6.02 document.
- **A printed ticket can no longer go missing because one answer arrived
  damaged.** A machine hands over the record of a printed ticket once, and
  deletes it as soon as the host's next poll counts as "got it". When that
  answer arrived garbled, the document has the host send the same poll
  again. The SAS box sent a different one, which is the "got it", so the
  machine threw away the only copy and the hub then refused the paper at
  every machine as an unknown number. Now a damaged or missing answer is
  asked for again, up to twice, with nothing sent in between. The SAS box
  also looks at the record first without taking it, and stores that copy
  if the real read still fails. The handpay record is read the same way.
- **A handpay is no longer stored as a ticket.** A machine keeps handpays,
  handpay receipts and records of redeemed tickets in the same list as
  the tickets it prints, and says which is which. That was ignored: every
  record became a redeemable ticket on the hub, so a handpay receipt could
  have been paid a second time. Only tickets are stored now. Anything else
  is logged and shown on the machine's event list as "validation record,
  not a ticket". Seen on a Bluebird 2: a handpay receipt printed with the
  attendant key was read as "jackpot handpay, receipt printed" and left
  off the hub.
- A restricted ("playable only") ticket is still stored and redeems like
  any other. The SAS box now keeps its type with the record; the hub does
  not flag it at the cash desk yet.
- **The same command is never sent a fourth time in a row.** The document
  defines three; a strict machine stops answering after that.
- Tests for these run against a new pretend machine that keeps the
  document's rules, since a Bluebird 2 forgives all three. Not yet tried on
  a strict machine.

## v0.3.7
- **The ticket header reaches a machine that was not ready for it.** The
  SAS box sent the header once each time a machine came onto the link. A
  WMS Bluebird 2 fresh from a RAM clear answered neither ticket-data poll
  two seconds after it came up, then stayed on the link, so that one
  attempt was the only one: the hub showed "ticket header pending" and a
  ticket was printed in that state. Only restarting the SAS box sent it.
  Now a header the machine did not take is tried again after 5, 10, 20
  and 40 seconds and then once a minute for as long as it is missing. It
  is also sent two seconds after the machine says its setup changed: it
  reset its meters, it has no validation ID, its validation ID was just
  set, or an operator changed its options. Those re-send a header that
  was already on the machine too, because SAS lets a machine go back to
  its own ticket text when its validation setup is cancelled. A machine
  that never takes a header gets one log line, not one a minute. Found in
  the Bluebird's own log and reproduced against a test machine that
  ignores the header at first; not yet repeated on a real RAM clear.

## v0.3.6
- **AFT registration on a machine that follows the SAS acknowledgement
  rule to the letter.** Registering a machine takes three polls with the
  same command: read, initialize, register. SAS has no "got it" message; a
  machine knows its answer arrived when the next poll is a different one,
  and it may ignore a command sent three times in a row. CabiNet sent the
  three back to back. A WMS Bluebird 2 answers anyway. A tester's
  Ainsworth A560 answered the first two and never the third, so
  registration failed every time with "silence on register (73/01)" while
  everything else on the machine worked. The SAS box now acknowledges an
  answer before it repeats a command, everywhere it repeats one:
  registration, the wait for an AFT lock, transfers. Found from the
  tester's support bundle and reproduced against a test machine that keeps
  the rule; not yet confirmed on the A560 itself.
- **A registration that fails says what the machine sent back.** "Silence"
  used to cover no answer, a refusal, "busy" and a damaged answer alike.
  The SAS log and the support bundle now name which one it was.
- **A support bundle no longer carries anything that opens the hub.** The
  bundle script copied the whole data folder, which since the hub login
  (v0.3.4) included the hub's API token, the hashes of the login passwords,
  the signed-in browser sessions, and the key and hashes behind player
  PINs. All of those are now left out; usernames, roles, players, balances
  and everything else a diagnosis needs are still there, and the note
  inside the bundle says what was left out. If the database cannot be
  copied and cleaned it is left out too, where before it was sent raw. A
  bundle made on v0.3.4 or v0.3.5 does contain them; if you sent one and
  use the hub login, a new hub password makes that copy of the old one
  useless.
- **Starting the DHCP server by hand says what is actually wrong.** On an
  installed host the `cabinet-dhcp` service already runs it. Run by hand
  next to it, the script now says so and stops, where before it could
  start a second DHCP server beside the first. With a network card name
  the box does not have (the default, `eno1`, is a PC's; a Pi has `eth0`)
  it lists the cards it does have and the command to use. It no longer
  ends every failure with "make sure to run with sudo", and it exits with
  an error when it fails, so the service is retried instead of quietly
  staying down. The bench guide names the interface now.
- **A sign-in page that is already open leaves when the lock is turned
  off.** It checked once, when it loaded. A screen left on the sign-in
  form, such as the hub's own touchscreen, stayed there after the lock was
  turned off from another device, and a kiosk has no reload button. The
  page now checks every few seconds and carries on to where it was going.

## v0.3.5
- **Ticketing status moved from the Tickets page to Settings, and it is
  now right for a machine that tickets over SAS.** The Tickets page is for
  looking a ticket up or voiding one, so the row of machine chips and the
  "Turn ticketing on/off" button are gone from it. Each machine's
  ticketing state now sits with the other per-machine switches, under
  **Settings → Machines**:
  - a machine that tickets over G2S shows **ticketing on** or **off** and
    keeps its "Turn ticketing off/on" button;
  - a machine that tickets over SAS shows **ticketing on**, or **off** with
    the reason (the SAS link is down, its Pi is not reporting, the machine
    is not hearing the Pi, or SAS is off). It has no button, because SAS
    ticketing follows the link. Before, such a machine (a Bluebird 2, for
    one) could say "ticketing off" while it printed and redeemed tickets
    normally, because only its G2S ticket device was looked at.
- **The Debug tab is hidden unless you turn it on.** It is the live host
  log and the protocol tools, which most people never need. It now ships
  hidden, and **Settings → Host options → Debug tab** shows it again. The
  switch is kept on the hub, so it applies in every browser and on the
  hub's own screen, and only admin logins see the tab. After this update
  the tab is hidden on every hub until someone switches it on. The tab
  bar also fills its width now when a tab is hidden.
- **No scroll bars on the hub's touchscreen.** The kiosk drew a pale
  desktop scroll bar down the side of every page. It is gone on the hub's
  own kiosk; scrolling is still a swipe. Other browsers, small windows and
  phones are not affected.
- **No mouse arrow on the hub's touchscreen — one command on an existing
  kiosk.** The kiosk parked an arrow in the middle of the screen. It was
  never a mouse: the Pi's HDMI ports count as pointing devices. Removing it
  takes a system rule that only root can install, which an update cannot
  do, so:
  - a kiosk set up from now on (`sudo ./deploy/kiosk_setup.sh`) has it;
  - on a kiosk you already have, run **`sudo ./deploy/kiosk_pointer.sh`**
    on the hub once. The screen restarts and, if the hub login is on, asks
    you to sign in again. The updater reminds you of the command until it
    is done. `--undo` puts the arrow back. A real mouse still works.
- **Icons show on a screen that has no emoji font.** The pages use emoji
  as icons, and a Raspberry Pi has no emoji font, so on the hub's own
  touchscreen (and on a wall board run from a Pi) every icon was an empty
  box. The hub now carries a small emoji font of its own, and a page uses
  it when it finds the device has none. It comes with this update:
  nothing to install, and it works with no internet. Phones and computers
  keep using their own emoji.
- **An open kiosk page picks up a change to the on-screen keyboard.** It
  already reloaded itself when the hub page changed; it now does the same
  when only the keyboard file changes.

## v0.3.4
- **Use a ticket again.** Open any ticket on the Tickets page and press
  **Set back to Issued**: the same paper can be redeemed once more. It
  works on redeemed, voided and expired tickets, and on a ticket stuck at
  "minted" because the hub never heard it print. It is refused while a
  machine is in the middle of redeeming that ticket.
- **Reusable tickets.** Tick **Reusable ticket** on a ticket and it goes
  back to Issued by itself every time it is redeemed, so you can keep a
  stack of round-number tickets to hand out. A reusable ticket never
  expires, the list shows how many times it has been used, and unticking
  the box makes the next redemption use it up. Both controls work for
  tickets printed over SAS and over G2S, and with the machine that
  printed them switched off.
- **SAS comes back by itself after a machine restart.** After a slot
  machine was switched off and on, its SAS link could stay dead until the
  satellite's SAS service was restarted by hand, so the machine showed
  offline with no validation ID or AFT registration. A machine that has
  just started ignores the satellite until it sees one poll addressed to
  a different machine. The satellite sent that poll once, at the moment
  the machine went off, and never again. It now sends it with every poll
  for as long as the machine is silent, so the machine is picked up within
  a second of being ready.
- **The Floor says when a machine cannot hear its Pi.** A machine that is
  switched on but receiving nothing from the satellite sends a "nobody is
  talking to me" signal. The hub used to read that as the machine being
  online. It now shows **machine is on but not hearing the Pi**, which
  points at one half of the link: the wire from the Pi's transmit pin,
  through the level shifter, to the machine.
- **Removed the serial "watchdog".** It reopened the satellite's serial
  port whenever a machine was silent, on the belief that the Pi had stopped
  receiving. That was never the cause (see the item above), it never
  brought a link back, and it reopened the port every 15 to 60 seconds for
  as long as a machine was switched off. Turning SAS back on in Settings
  now also re-syncs the machine straight away.
- **Restarting a machine during an update no longer undoes the update.**
  The updater used to roll everything back, database included, if a
  machine that had been on was not on again three and a half minutes
  later, even though the hub and the Pi were both fine. It now checks the
  hub and the Pis themselves, looks at the floor again just before it
  restarts anything, and names a machine that is off or restarting in a
  warning instead. It still rolls back when something is really broken: a
  Pi whose service stops reporting, a SAS machine that never answers the
  new code, or a G2S machine that talks to the hub but cannot rejoin.
- **On-screen keyboard for a hub with no keyboard.** On the hub's
  touchscreen, tapping any text box now brings up a keyboard along the
  bottom of the screen (a number pad for number boxes). It works on the
  hub page, the sign-in page, the Test Panel and the console, so names,
  players, settings and logins can all be typed on the hub itself. Phones
  and tablets keep using their own keyboard. Add `?osk=1` or `?osk=0` to
  the hub's address once to force it on or off in that browser.
- **Optional sign-in for the hub pages.** Settings has a new **Hub
  login** card. Add a login, turn the lock on, and the hub pages ask for
  a username and password. It is off until you turn it on, and nothing
  changes if you never do.
  - **Two kinds of login.** An **admin** can do everything. A **normal**
    login can see everything and run the floor day to day: clear
    handpays, void tickets, lock and unlock machines, load credits.
    Settings, logins, players and cards, naming, forgetting machines,
    updates, the Test Panel, and the two ticket controls above need an
    admin.
  - **Machines keep working without a login.** Slot machines, satellite
    and card-reader Pis, the screens on the machines and the wall board
    never sign in. A signed-out browser can read only what the wall board
    shows.
  - **Signs out when unused.** A login is signed out after 30 minutes
    with no taps or typing (you can change this, or turn it off). The
    hub's own touchscreen asks for a sign-in too.
  - **Locked out?** On the hub, run `python3 G2S/tools/hub_login.py`. It
    can reset a password, add an admin or turn the lock off, and needs no
    sign-in.
  - **What it does not do.** The hub still speaks plain HTTP, so a
    password crosses your network unencrypted, and a device on the slot
    network can still talk to the hub the way a machine does. The lock
    keeps people out of the hub pages; keep strangers off the slot
    network as before.

## v0.3.3
- **Updating no longer needs every machine switched on.** The updater
  used to stop with "could not read WorkingDirectory" if any satellite Pi
  was powered off, so one dark machine blocked the update for the whole
  floor. It now skips a Pi it cannot reach, names it, and updates
  everything else.
- **A machine that comes back later is brought up to date by itself.**
  When a satellite or card-reader Pi starts reporting, the hub checks its
  code against its own and, if it is older, copies the current version
  over and restarts that Pi's service. Nothing is restarted if it is
  already current. It waits if an update or a tournament is running.

## v0.3.2
- **A machine whose clock runs ahead of the hub can now join.** Such a
  machine treated every request from the hub as already expired (the
  "Time-to-live Expired" refusal), including the one that completes the
  handshake, so it sat at "waiting to join" forever. Found on a Bally
  Alpha 2 Pro Curve running 70 seconds fast. The hub now measures the
  lead from the machine's own timestamps and gives its requests that much
  extra time to live; once the machine has joined, the existing clock
  sync sets its clock right and the extra time is no longer needed.
  A machine set weeks or more ahead (a wrong month or year) gets requests
  that never expire instead, since that much extra time does not fit in
  the field. Machines running behind the hub were never affected.
- **The bench pre-flight check works on a Raspberry Pi hub.**
  `bench_preflight.py` looked for a network port called `eno1`, which a
  Pi does not have, and reported the hub's own running services as
  intruders on ports 8081 and 67, so a correctly built hub got NO-GO. It
  now finds the slot network port itself (or takes `--interface`), and
  recognises CabiNet's own servers.

## v0.3.1
- **SAS on a Raspberry Pi 4 now reads long replies.** On a Pi 4 the SAS
  box cut every machine reply longer than 16 bytes in two and threw both
  halves away. Short replies still got through, so the machine looked
  healthy: credits showed, bonuses landed, tickets were issued. But AFT
  never registered ("silence on registration initialize"), and a printed
  ticket was never recorded as printed, so it was refused when you fed it
  back in. Found from a tester's support bundle and reproduced on a WMS
  BB2. If you saw "Invalid CRC in packet" filling the SAS log, this was it.
- **Satellite Pis keep the right time.** A Pi on the slot network has no
  route to the internet and no battery clock, so it sat at whatever date
  its image was made. It now takes its time from the host. Existing
  satellites get this on their next Update.
- **Setup on a brand-new card no longer stops at "mkdir ... failed".**
  `smib_setup.sh` and `companion_setup.sh` now create the folder they copy
  into.

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
