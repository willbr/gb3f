# Session log — building gb3f

A reconstructed narrative of the conversation that produced this project,
written from Claude's context at the end of the session. User messages are
quoted verbatim; assistant side summarises tool use and findings.

---

## 1. "read everything in this repo"

At session start the repo held:

- `readme.md` — four-line project sketch: use BGB, build a 3-instruction Forth
  bootrom with RGBDS, drive it over the link cable from Python, print a
  character.
- `reference/forth.md` — Frank Sergeant's 1991 paper, the source for the
  3-instruction idea and its wire protocol (01 fetch, 02 store, 03 call).
- `reference/Specifications.html` — Pan Docs: memory map, serial registers
  (SB=$FF01, SC=$FF02), video (LCDC, tile data at $8000, BG map at $9800),
  cartridge header, SM83 instruction set.
- `reference/bgb readme.html` — BGB 1.6.6 manual; commandline flags
  (`-rom`, `-listen`, `-br`, `-autoexit`, `-screenonexit`, `-headless`),
  debugger hotkeys, `ld b,b` source breakpoints.
- `reference/bgb 1.4 link protocol.html` — BGB's TCP link protocol: 8-byte
  packets, version handshake, sync1/sync2/sync3, 2 MiHz timestamps,
  wantdisconnect for clean shutdown.

Summarised findings and how they fit together: RGBDS assembles a cartridge
whose main loop polls SB/SC and dispatches on 01/02/03 — essentially
Sergeant's HC11 monitor ported to SM83. Python opens a TCP socket to BGB,
does the version handshake, and shuttles bytes via sync1/sync2. "Print a
character" = XC! tile bytes into $8000+, XC! tile index into $9800+.

## 2. "go for it"

### ROM

Wrote `3forth.asm`: header at $100 (nop/jp Start, then 76 zero bytes that
rgbfix fills in), a `Stop` section at $0008 with `ld b,b` / `jr -2` for
scripted halting, and a main loop at $0150 calling GetByte / dispatching to
.fetch / .store / .call. GetByte arms SC=$80 (slave, start) and waits for
bit 7 to clear. SendByte writes SB then arms SC. GetAddr is two GetBytes.
CallHL is `jp hl`, wrapped in a `call` so the callee's RET pops the right
return address.

Built with the old RGBDS on the system:
```
rgbasm  -o 3forth.o  3forth.asm
rgblink -o 3forth.gb -n 3forth.sym 3forth.o
rgbfix  -p 0 -v 3forth.gb
```
Two syntax surprises for that old rgbasm: `@` isn't a constant expression
there, and `ds N, fill` isn't supported — `ds 0x4C` with plain zero padding
was fine since rgbfix overwrites those bytes anyway.

### Host

Wrote `gbforth.py`: socket client with TCP_NODELAY, version handshake
(cmd 1, b2=1 b3=4 b4=0), `xchg(byte)` that sends sync1 (b3=$81) and waits
for sync2/sync3, and high-level `fetch / store / call` primitives. First
version incremented the timestamp by a tiny constant per packet.

### Debugging round 1 — BGB emulation paused

First test: `peek 0x0100` returned $FF. Adding verbose packet logging
revealed BGB's status packet reported `paused` (bit 1 set) and every sync1
came back as sync3 b2=1 (GB not armed).

Discovered BGB gates emulation on the remote side's timestamp: if our
timestamps lag behind BGB's 4.19 MHz simulated clock, BGB pauses emulation
waiting for us. Switched to wall-clock timestamps in 2 MiHz ticks. `peek
0x0100` returned 0 — correct (nop byte).

### Debugging round 2 — first byte missed

`peek 0x0101` showed the first sync1 getting sync3 b2=1 (GB still ramping
up), then subsequent transfers succeeding but shifted by one position.
Added a retry loop in `xchg` on sync3 b2=1: keep re-sending the same byte
until BGB reports an actual transfer.

### Debugging round 3 — sync2 one-transfer lag

`peek 0x0104` returned 0x01 instead of 0xCE. Traced the sync2.b2 sequence
across a fetch: the bytes came back as if GB's SB were one transfer behind
what the protocol should produce. Adding a second dummy byte at the end of
`fetch` (so five bytes total: `01 hi lo 00 00`) fixed it — the real response
now clocks out on the second dummy.

`selftest` now passed: all ROM peeks correct, 512-byte WRAM round-trips with
0 mismatches.

## 3. "bgb opens with the debug window, is that causing problems?"

Answered that the debug window itself is cosmetic, but debug mode does
halve emulation speed and BGB pauses link emulation until handshakes settle
— the `paused` status flag I was seeing. That was the real culprit, not the
window.

## 4. "would it be better if you used headless mode to avoid the debugger?"

Reasoned headless would be cleaner for automation and tried it. `peek`
worked. But when I ran the `hello` demo — driving ~1060 XC! calls to set up
tile data, clear the BG map, and enable the LCD — the screenshot came out
blank. Poked at VRAM and discovered `[8000] = FF` after storing $AA there.
A stress test of 128 VRAM writes in headless returned 127 mismatches.

Concluded that `-headless` ("run without graphics") disables LCD
emulation, so VRAM reads/writes become no-ops. Windowed BGB is required
even for screenshot-driven automation.

## 5. VRAM-write flakiness and the upload-and-XCALL trick

Even in windowed mode, under the 1000+ XC! load, some VRAM writes didn't
stick — the 'H' glyph bytes came back with stray FFs in positions that
should have been 00. Traced to the disabling-LCD-outside-VBlank hazard
mentioned in Pan Docs: BGB apparently emulates lingering-undefined-state
behaviour.

Switched strategy to the actual intent of a 3-instruction Forth: let the
target do the heavy lifting. `print_h` now assembles a ~30-byte SM83
subroutine (LCD off, set BGP/SCX/SCY, copy glyph from $C100 into tile 1 at
$8010, write 1 into $9800 for the top-left, LCD on, ret), uploads it into
WRAM at $C000, uploads the glyph at $C100, and does a single
`XCALL $C000`. Runtime dropped from 20 s to ~1.2 s and output stabilised.

The screenshot (`hello.png`) showed a small `H` in the top-left corner; the
surrounding "Nintendo®" is BGB's simulated post-boot VRAM that this minimal
routine doesn't clear.

## 6. BMP → PNG conversion

User rejected a PowerShell one-liner and asked:

> write a python script to convert the image

Wrote `bmp2png.py`: three-line Pillow wrapper. Converted `hello.bmp` to
`hello.png`, read it back with the `Read` tool to confirm the 'H'.

## 7. "please document your work in a markdown file"

Wrote `NOTES.md` — file layout, build/run commands, a diagram of the ROM's
main loop, BGB protocol framing, the pile of quirks (timestamp gating,
sync2 one-transfer lag, headless kills VRAM, wantdisconnect required,
between-bytes arming gap), the upload-and-XCALL trick, wire-level tuning
knobs, and a "what's missing" section.

## 8. "git commit your work"

Added a `.gitignore` excluding `__pycache__/`, `*.o`, `*.sym`, `*.bmp`,
scratch logs, and `.claude/`. Staged the source (`3forth.asm`, `gbforth.py`,
`bmp2png.py`), the built ROM (`3forth.gb`, for reproducibility with the
specific rgbasm vintage), the demo screenshot (`hello.png`), `NOTES.md`,
`.gitignore`, and the readme-with-numbering change the user had made.
Committed as `ce69dd5` on `master`, one commit ahead of origin.

## 9. "update the readme"

Rewrote `readme.md` as a proper project overview: what gb3f is and its
Sergeant-paper lineage, the screenshot inline, a quick-start recipe
(build / launch BGB / drive from Python), a file layout table, and a
pointer to `NOTES.md` for the deep dive. Left it uncommitted for review.

## 10. "please export this chat log to the reference folder"

— this file.

---

## Takeaways worth remembering

- A 3-instruction Forth really is enough. 66 bytes on the target; everything
  else — even "clear 1024 bytes of VRAM" — lives on the host, uploaded as a
  small routine and invoked via the third instruction.
- BGB's link protocol is easy to speak but its failure modes are quiet.
  Timestamps aren't cosmetic; headless isn't invisible; sync2 data is one
  transfer behind where you'd expect. Verbose packet logging paid for
  itself the first time it fired.
- When a write-heavy host-driven loop is too slow or too flaky, the answer
  with a programmable target is almost always "move the loop to the
  target". 1000 XC! calls → 1 XCALL.
