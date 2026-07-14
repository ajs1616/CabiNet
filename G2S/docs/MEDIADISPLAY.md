# IGT mediaDisplay — on-glass content over G2S (wire-proven 2026-07-10)

CabiNet can push HTML to an IGT AVP's **own screen** over G2S: the host sends a URL,
the cabinet's built-in browser HTTP-GETs it from the hub, and it renders on one of the
cabinet's media windows. No pixels ride G2S — it's just HTML served by the hub. This
was proven live on a real AVP: `hello.html` rendered on the left
Service Window.

This is the collector payoff for task #18: a G2S cabinet needs only an RFID Companion
(no external touchscreen) because the slot's own glass is the player/operator UI.

## The class

- Namespace: `igtMediaDisplay = http://g2s.igt.com/mediaDisplay/v1.0.0` (an IGT extension,
  the pre-cursor to the GSA-standardized G2S mediaDisplay class — **not** in the G2S
  v1.0.3 manual). Authoritative schema: `igtMediaDisplay.xsd` **v1.8** (IGT © 2007).
- Wire framing (`build_inner_request_ext` in `g2s_host.py`): the class element is
  `<igtMediaDisplay:mediaDisplay …>` — extension prefix + local name `mediaDisplay`
  (NOT `<g2s:mediaDisplay>`, NOT `IGT_mediaDisplay` as an element name). Structural attrs
  (deviceId/dateTime/commandId/sessionType/sessionId/timeToLive) keep the `g2s:` prefix;
  command-child attrs use `igtMediaDisplay:`; **inherited transaction attrs
  (`transactionId`) keep `g2s:`** because the schema is `attributeFormDefault=qualified`
  and transactionId is inherited from the g2s namespace.

## The AVP's 6 windows (live geometry, getMediaDisplayProfile)

| dev | description | type / position | size (WxH) | notes |
|----:|-------------|-----------------|-----------|-------|
| 1 | Service Window | left | 256×1024 | portrait strip, left edge |
| 2 | Service Window | right (IGT_right) | 256×1024 | portrait strip, right edge |
| 3 | Digital Glass | overlay / fullScreen (secondary) | 1280×1024 (content 640×512) | touchscreenCapable=false |
| 4 | Play Area | overlay / fullScreen | 1280×1024 (content 640×512) | priority 3 |
| 5 | Player Banner | bottom | 1280×32 | the thin "Hello %n" strip |
| 6 | Notification Window | bottom | 1280×200 | priority 4 |

All report `localConnectionPort=30001` (EMDI — the content↔EGM return channel; **not**
required for fetch or display, only for a page that wants to talk back to the cabinet).

## Two enables, and a RAM ceiling

- **Play-enable ≠ media-enable.** A cabinet can be fully playable (credits, ready-to-play,
  `hostEnabled=true`) while its media windows are disabled.
- Each window has its own `egmEnabled`, a **cabinet operator-menu** switch the host
  **cannot** flip (`setMediaDisplayState enable=true` acks but does not clear it). Enable
  it on the machine (IGT operator/attendant menu → Media Display). Read-back convention:
  `getMediaDisplayStatus` returns `egmEnabled="false"` when disabled and **omits** the
  attribute when enabled (default-true omission — an empty attrs dict = enabled).
- A disabled device rejects `loadContent` with `G2S_APX016 "Command Not Processed,
  Device Disabled"`.
- **RAM limit:** AJ's AVP refused to enable all 6 at once ("not enough memory"). The two
  1280×1024 fullscreen overlays (dev 3 Digital Glass, dev 4 Play Area) are the heavy ones;
  the four lighter windows (1, 2, 5, 6) enabled fine. Budget accordingly.

## The content lifecycle (wire-proven)

```
loadContent(contentId, mediaURI)                 -> EGM ack
        EGM: contentStatus{contentId, contentState=IGT_contentPending, transactionId=N}
        EGM browser: HTTP GET mediaURI  (~4s later; UA logged as "GLASS FETCH")
setActiveContent(g2s:transactionId=N, contentId) -> EGM ack
        EGM: contentStatus{contentState=IGT_contentExecuting}
showMediaDisplay        (empty element)          -> EGM: mediaDisplayAck{transactionId, contentId}
        -> content visible on glass
--- teardown ---
hideMediaDisplay        (empty element)          -> mediaDisplayAck
releaseContent(g2s:transactionId=N, contentId)   -> contentStatus IGT_contentReleased
```

- The **EGM assigns the transactionId** at load and reports it in `contentStatus`; the
  host echoes it into every transaction verb. CabiNet captures it automatically (the
  inbound fold stashes it in `assoc.media_txn[dev]`), so bench probes don't re-type it.
- `contentState` enum: IGT_contentPending → IGT_contentLoaded → IGT_contentExecuting →
  IGT_contentReleased (+ IGT_contentError / contentException on failure).
- **Diagnostics:** `getContentLogStatus` (empty) + `getContentLog` (g2s:lastSequence/
  totalEntries) return contentLog records carrying the transactionId, contentId, and the
  **mediaURI the EGM actually recorded** — invaluable for catching a wrong/defaulted URL.

## Command inventory (igtMediaDisplay.xsd v1.8)

Host requests: getMediaDisplayProfile, getMediaDisplayStatus, setMediaDisplayState,
setMediaDisplayLockOut, **loadContent**, **setActiveContent**, **showMediaDisplay**,
**hideMediaDisplay**, **releaseContent**, getContentStatus, getContentLogStatus,
getContentLog. EGM responses: mediaDisplayProfile, mediaDisplayStatus, mediaDisplayAck,
contentStatus, contentLogStatus, contentLogList.

**There is no `showContent`/`hideContent`.** Those were reconstruction guesses; sending
one draws `G2S_MSX004` (the gSOAP deserializer has no binding for a nonexistent element).

## The two bugs that cost a round-trip (so you don't repeat them)

1. **`mediaURI`, not `mediaUri`.** The attribute is capital-URI and is *optional with a
   default of `http://www.example.com/example.swf`*. Send the wrong casing and the
   deserializer silently drops your attribute, defaults the URL to example.swf, acks the
   command cleanly, and never fetches your page. XML attributes are case-sensitive.
2. **Display is `setActiveContent` + `showMediaDisplay`, not a single "show".** loadContent
   alone fetches but does not display.

## Window sizing & type — how to shrink the panel (AVP, live-read 2026-07-10)

A media window's footprint is not fixed HTML — it's the window's **geometry + type**,
and both are exposed as **settable optionConfig parameters** in each device's
`IGT_mediaDisplayOptions` group (securityLevel `G2S_administrator`; host-vs-operator
settability = the `canModRemote` flag, confirm with a `getOptionList` `optionDetail=true`
before relying on a remote set). Live paramIds + values read off Service Window dev 1:

| paramId | dev-1 value | meaning |
|---------|-------------|---------|
| `IGT_mediaDisplayType` | `IGT_scale` | **scale = squeezes the game aside; overlay = floats on top** |
| `IGT_mediaDisplayPosition` | `IGT_left` | left/right/bottom/top/fullScreen |
| `IGT_xPosition` / `IGT_yPosition` | `0` / `0` | top-left origin on the screen |
| `IGT_mediaDisplayWidth` / `IGT_mediaDisplayHeight` | `256` / `1024` | the WINDOW box |
| `IGT_contentWidth` / `IGT_contentHeight` | `256` / `1024` | the CONTENT region inside it |
| `IGT_mediaDisplayPriority` | `1` | stacking order |

`mediaDisplayType` enum (igtMediaDisplayGlobal.xsd): `IGT_scale`, `IGT_overlay`,
`IGT_primaryScale`, `IGT_primaryOverlay`, `IGT_linkedScale`, `IGT_linkedOverlay`.

**Why the panels feel huge:** every *enabled* window we can use is a **scale** type —
it carves its box out of the game, so a 256-wide Service Window or a 200-tall
Notification permanently shrinks the game by that much. The two **overlay** windows
(dev 3 Digital Glass, dev 4 Play Area) *float over the game without shrinking it* — a
small, mostly-transparent overlay page = a small on-glass chip with the game full-size
behind it — but they're the RAM-heavy 1280×1024 windows the AVP won't always enable.

**Two ways to a small panel — BOTH CLOSED on AJ's AVP (live-adjudicated 2026-07-10):**
1. **Resize via optionConfig: ❌ canModRemote=false.** Every geometry param sampled
   (`IGT_xPosition`, `IGT_mediaDisplayWidth`, `IGT_mediaDisplayType`) rejects with
   `G2S_OCX015 "…parameter is not remotely configurable, therefore the incoming value
   should match the existing value"` — the whole `IGT_behaviorParams` geometry set is
   firmware-locked to operator-side config. (Method note: `applyCondition=G2S_cancel`
   is a free validate-only dry run; and the multi-param `values` form of `setOption`
   sends all edits in ONE §9.15 transaction — exposed in /api/command 2026-07-10.)
2. **Overlay + transparent page: ❌ the base plane is OPAQUE.** overlay_probe v2 cycled
   `transparent` / `rgba(0,0,0,0)` / black / magenta / green on the live dev 4 Play
   Area — every mode painted a solid slab over the game. No chroma-key, no CSS
   transparency; the window host owns the base plane and doesn't composite. (dev 4
   renders + touches fine otherwise — it ran the probe pages perfectly.)

What SHIPPED instead: **GLASS_FOLLOW_CARD** — the menu window hides when nobody is
carded in (game 100% full screen) and shows on the fob tap; the page stays alive and
polling under the hide so re-show is ~0.4s. Remaining unexplored: whether the cabinet
OPERATOR menu (where per-window memory allocation lives) exposes window geometry.

## Which windows actually render HTML (live-tested)

| dev | window | renders our HTML? | notes |
|----:|--------|-------------------|-------|
| 1,2 | Service Window (256×1024, scale) | ✅ yes | proven (hello.html, glass_ping.html) |
| 5 | Player Banner (1280×32, scale) | ❌ no | loadContent stuck IGT_contentPending, never fetched — system-reserved, not a browser |
| 6 | Notification (1280×200, scale) | ⚠️ executes but no GET seen | reached IGT_contentExecuting; fetch not confirmed — verify |
| 3,4 | Digital Glass / Play Area (1280×1024, overlay) | untested (RAM-gated enable) | the float-over-game windows |

**Content lifecycle gotcha:** a device holds ONE active content (`maxContentLoaded=1`).
Loading new content while a device already has active content NO-OPs (stays pending, no
fetch). **releaseContent the old (needs its contentId + transactionId) before loadContent
the new** — live-hit 2026-07-10.

## Sequencer timing laws (live-hit 2026-07-10, glass nav v1)

Automating the lifecycle (`glassShow`) taught two more wire facts the manual rungs never
exposed (human latency between rungs masked both):

1. **`setActiveContent` before the fetch draws `IGT_MDX005` "Content not loaded."** —
   `contentStatus IGT_contentPending` is NOT permission to activate; the cabinet browser
   must actually GET the page first (0.7–4 s later; `IGT_MDE101/MDE102` events narrate
   the load, same txn). CabiNet's sequencer therefore only *records* the txn on
   pending and fires the activate off the **resident SPA's own self-identifying poll**
   (`/api/glass/state?...&src=spa&dev=N` — a poll can only come from a fetched page),
   spaced 2.5 s, ≤5 tries. `IGT_contentLoaded` in a contentStatus (when narrated)
   advances immediately.
2. **`loadContent` at an occupied window draws `IGT_MDX003` "Must release loaded
   content."** — and after a HOST RESTART the hub's in-memory contentId/txn are gone,
   so release-before-load has nothing to release while the page sits happily on the
   glass. The `src=spa` poll doubles as the restart-surviving residency signal
   (`GLASS_SPA_LIVE_SEC`): a fresh one makes the card-IN recovery skip entirely and an
   explicit `glassShow` short-circuit to a bare `showMediaDisplay`
   (`residentShowOnly: true`). A mediaDisplay-class rejection while a push is still at
   the 'loading' stage fail-fasts (clears the push at once with an actionable log).

**Deploy trick that fell out for free:** the SPA self-reloads when `uiBuild` (the
glass.html mtime in the state response) changes — scp a new page to the hub and the
resident glass picks it up on its next poll, no content re-push, no operator touch.

## The cabinet browser (design target)

`hello.html`'s GET revealed the UA:
`Mozilla/5.0 (QtEmbedded; QNX) AppleWebKit/534.34 (KHTML, like Gecko) Qt/4.8.2 Safari/534.34`
— a QNX / Qt 4.8.2 / WebKit 534.34 engine (~Safari 5, 2011). Author content **ES5-only**,
no flexbox/grid (absolute or table layout), sized to the target window's geometry
(dev 1 = 256×1024 portrait). There is no worse-of-fleet browser to wait for: **the BB2
has no browser at all** (next section).

## The BB2E (WMS) fleet tell — live-adjudicated 2026-07-10

- **The control dialect is SHARED.** The BB2 answers getMediaDisplayStatus /
  getMediaDisplayProfile / show / contentStatus in clean igtMediaDisplay v1.8 shapes:
  2 windows (dev 1+2), both 160×600 scale, right edge of the Game Screen,
  touchscreenCapable=true, maxContentLoaded=5, egmEnabled out of the box.
- **The content engine is NOT a browser — it's a Flash 7 player.** The profile's
  capabilitiesList advertises exactly one capabilityItem: `IGT_flash / 7 / swf`.
  loadContent with an `.html` mediaURI draws `IGT_MDX006 "Invalid event or command"`
  (extension gate); an `.swf` URI is ACCEPTED (txn assigned, contentStatus follows).
- **And the loader never fetches**: with a real 30-byte Flash-7 SWF served
  (`webui/funcino.swf`), contentStatus goes `IGT_contentError contentException=1`
  ~300ms after pending with **ZERO packets from the BB2** (tcpdump: no HTTP, no DNS —
  nothing). The downloading/rendering engine is the WMS **BigEvent/CGC media
  controller** side (the operator "protocol version 1/2" screen: 169x=v1, 16Ax=v2 OS
  releases; the CGC server address lives in the cabinet's IP settings). With CGC
  unconfigured the G2S class acks the choreography but no content can ever arrive.
- **Decision (AJ, 2026-07-10): BB2 player UX stays on the external SMIB screen path**
  (the SAS satellite / Pro-tier touchscreen). The carded greeting via setIdValidation
  still works on the BB2 (first G2S carded session was proven there). Impersonating a
  CGC server is a parked someday-quest — capture what the cabinet probes if CGC is
  ever pointed at the hub.

## Driving it from CabiNet

`POST /api/command {"action":"mediaDisplayProbe","egmId":…,"rung":R,"deviceId":D,…}`
where R ∈ status | profile | enable | disable | load | logstatus | log | setactive |
show | hide | release | contentstatus. `load` accepts `uri` (defaults to
`http://192.168.50.2:8081/webui/hello.html`) and allocates a numeric contentId;
transaction rungs auto-use the captured transactionId or accept an explicit
`transactionId`. Every probe is INERT until fired — nothing mediaDisplay hits the wire at
join, and the class stays out of SPOKEN_CLASSES.

Status/captured state surfaces at `GET /api/status` under each machine's `mediaDisplay`
block.

See `reference_casinonet_mediadisplay` (session memory) for the blow-by-blow; the
authoritative schema copies are in the session scratchpad.
