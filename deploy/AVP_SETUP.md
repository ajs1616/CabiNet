# IGT AVP machine setup — step by step

Everything the machine side needs, with photos from a real AVP (Family 14).
The network join is zero-config (the host hands the machine its URL over
DHCP) — these steps turn on the *features*: G2S, money permissions, and the
on-glass UI.

**You need your eKey inserted to change the protocol settings, and the
credit meter must be at zero** — the machine refuses protocol changes while
credits are on it (cash out first).

## 1. Enable G2S

Operator menu: **Setup > Communication > Protocol**.

![Protocol Setup list](img/avp-protocol-list.jpg)

Tap **Advanced** next to **G2STransportG2S 6** (the name may be numbered
differently on your cabinet) and set **Protocol enabled = YES**:

![G2S Transport Advanced Options](img/avp-g2s-transport-advanced-redacted.jpg)

The other fields on this screen lock while the protocol is enabled (the red
banner) — to change them, disable the protocol first, change, re-enable.
The defaults are fine.

## 2. Protocol permissions

Back on the protocol list screen, hit **NEXT** at the bottom — the
**PROTOCOL THAT CONTROLS** pages appear. Set **G2STransportG2S 6** on every
field below (three pages; Previous/Next walks them):

| Field | Notes |
|---|---|
| Voucher In/Out | * |
| Handpay Resets | |
| Progressive | ^ |
| Remote Configuration | |
| Download | |
| Bill Validator Setup | |
| Wagering Account Transfers | * — **see warning below** |

\* — once G2STransport is set, the **Advanced** button next to the field
opens machine-level options you may set to your liking (ticketing
permissions, for example).
^ — not used by CabiNet yet; set it now for future features.

![Protocol that controls, page 1](img/avp-protocol-control-1.jpg)
![Protocol that controls, page 2](img/avp-protocol-control-2.jpg)
![Protocol that controls, page 3](img/avp-protocol-control-3.jpg)

> ⚠️ **Wagering Account Transfers — Advanced is NOT optional.** Both
> settings must be **YES** or wallet↔machine transfers won't work:
>
> ![Advanced G2S WAT Options](img/avp-wat-advanced.jpg)

Voucher In/Out's Advanced page is where your ticketing preferences live —
set to taste:

![Voucher In/Out options](img/avp-voucher-options-1.jpg)

## 3. Enable the glass (on-machine UI)

Operator menu: **Setup > Machine > Media Display**. Two settings:

**Media Display Global Options** → **Application handles service button =
YES** (this is what lets the cabinet's SERVICE button open and close the
CabiNet menu):

![Media Display Global Options](img/avp-md-global-options.jpg)

**Media Display Setup** → select **Service Window (Left)** → **Enable media
display = TRUE**:

![Service Window (Left) enable](img/avp-md-service-window-left.jpg)

That's the one window CabiNet uses — leave the other five alone.

## Done

Exit the operator menu, **remove the eKey, and close the logic door**. The
machine finds the host and joins on its own either way — watch its tile go
**Connecting…** then **LIVE** on the floor view — but it stays in **tilt**
until the key is out and the door is shut, so it'll look like nothing's
working even though it already joined. If the endpoint stays dark after you
changed comm settings, re-enable G2S in the debug menu (see `DEPLOY.md`).
