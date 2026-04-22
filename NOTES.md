# gb3f — a 3-instruction Forth for the Game Boy over BGB's link cable

A tiny monitor ROM that exposes three primitives (`fetch`, `store`, `call`)
to a host PC over the emulated link cable, following Frank Sergeant's 1991
"3-instruction Forth for embedded systems" paper. The PC drives BGB's link
protocol, and all higher-level behavior (VRAM setup, glyph upload, LCD on)
is composed on the host out of XC@/XC!/XCALL.

End-to-end demo: `hello` prints an `H` glyph in the top-left corner of the
Game Boy screen.

## Files

| Path | What |
| --- | --- |
| `3forth.asm` | SM83 source for the monitor. Assembles to 66 bytes at $0150 plus an `ld b,b` halt at $0008. |
| `3forth.gb` | Assembled 32 KiB ROM (rgbasm + rgblink + rgbfix). |
| `words.asm` | Hot-reloadable library of leaf SM83 words, uploaded into WRAM at runtime. |
| `gbforth.py` | BGB link-cable client and CLI. |
| `bmp2png.py` | Pillow one-liner to convert BGB's `.bmp` screenshots to `.png`. |
| `run_tests.py` | Scenario suite: builds the ROM, drives BGB through each gbforth subcommand, reports pass/fail. |
| `build/` | rgbasm/rgblink outputs (`.o`, `.sym`, `words.bin`) — gitignored. |
| `screenshots/` | BGB BMPs (gitignored) and their PNG exports (tracked). |
| `reference/forth.md` | Sergeant's original 68HC11 paper. |
| `reference/Specifications.html` | Pan Docs (GB hardware reference). |
| `reference/bgb readme.html` | BGB 1.6.6 user manual (options, command-line flags). |
| `reference/bgb 1.4 link protocol.html` | The TCP wire protocol BGB speaks. |

## Building the ROM

Intermediate outputs (`.o`, `.sym`) land in `build/`; the final
`3forth.gb` stays at the repo root since it's the tracked distributable.

```sh
mkdir -p build
rgbasm  -o build/3forth.o  3forth.asm
rgblink -o 3forth.gb       -n build/3forth.sym build/3forth.o
rgbfix  -p 0 -v 3forth.gb
```

`-p 0` pads to the next valid ROM size with zeros, `-v` fills in the Nintendo
logo, header checksum, and related header fields. The resulting cartridge
boots on any DMG emulator; on real hardware it would need a flash cart.

## Running

```sh
# Terminal 1: start BGB in windowed mode, listening for link clients.
#   -br 8       break when PC hits $0008 (our `ld b,b` halt)
#   -autoexit   exit when breaking to the debugger
#   -screenonexit <path>  save a final framebuffer as BMP on exit
bgb64 -rom ./3forth.gb -listen 127.0.0.1:8765 \
      -nowarn -nowriteini -br 8 -autoexit \
      -screenonexit "$(pwd)/screenshots/hello.bmp"

# Terminal 2: drive the ROM.
python gbforth.py selftest        # round-trip 512 bytes through WRAM
python gbforth.py peek 0x0104     # read a ROM byte
python gbforth.py poke 0xC000 42  # write a RAM byte
python gbforth.py hello --halt    # draw H in the top-left, then XCALL $0008
python bmp2png.py screenshots/hello.bmp screenshots/hello.png

# Or run the whole suite non-interactively — `run_tests.py` builds the
# ROM, spins up a fresh BGB per scenario, and drives each gbforth
# subcommand in sequence.
python run_tests.py
```

`--halt` makes `hello` finish by calling into the `ld b,b` trap so that BGB
autoexits and writes the screenshot. Without `--halt` the ROM keeps waiting
for more commands.

## The ROM

The Game Boy loops as slave, reading command bytes off SB and dispatching:

```
loop:            GetByte:                  SendByte:
  call GetByte     a = $80                   [SB] = a
  cp 1  -> fetch   [SC] = a  (arm slave)     a = $80
  cp 2  -> store   wait until SC.bit7==0     [SC] = a
  cp 3  -> call    return [SB]               wait until SC.bit7==0
  jr loop                                    return

fetch: GetAddr; a=[hl]; SendByte; jr loop
store: GetAddr; GetByte; [hl]=a; jr loop
call:  GetAddr; CallHL; jr loop         (CallHL is just `jp hl`)
```

Total: 66 bytes. `ld b,b` at $0008 is a BGB source-code breakpoint we use as
a clean halt target when driving scripted tests.

## The host protocol

BGB's link cable runs over TCP with 8-byte fixed-size packets:

```
offset size description
0      1    b1  — command
1      3    b2/b3/b4 — per-command
4      4    i1  — little-endian 31-bit timestamp (2 MiHz ticks)
```

Initial handshake: both sides send cmd `1` (version 1.4.0) and a `108`
status packet. BGB pauses emulation until the link is synchronized.

To exchange one byte while the GB is armed as slave, the host sends
cmd `104` (sync1) with `b2=data`, `b3=$81`. BGB replies with either
`105` (sync2) containing the GB's SB, or `106` (sync3) with `b2=1`
meaning "GB wasn't waiting on a transfer". The host retries on the
latter.

### Quirks learned the hard way

- **BGB gates emulation on the remote's timestamp.** If the host sends
  constant or near-zero timestamps, BGB stalls the GB CPU waiting for
  the host's clock to "catch up". Sending real wall-clock time expressed
  in 2 MiHz ticks fixes this.
- **`sync2.b2` reflects SB with a one-transfer delay.** A `fetch` of
  `$0104` needs to send five bytes (`01 hi lo 00 00`), not four — the
  last dummy byte is what actually clocks the real response out.
- **`-headless` disables LCD emulation** ("run without graphics"), and
  consequently VRAM reads/writes all become no-ops. Windowed BGB is
  required even for automated screenshot runs.
- **BGB boots with `LCDC=$91`** (post-boot-ROM state), so turning off
  the LCD before touching VRAM is still necessary. Disabling the LCD
  outside VBlank is undefined on real hardware, and under BGB it leaves
  VRAM in a state where some subsequent writes leak.
- **On graceful disconnect, send `109` (wantdisconnect)** after the
  `108` status announcing `supportreconnect`. Otherwise BGB assumes the
  drop was unintentional and refuses to re-listen, leaving the port
  unreachable until BGB is restarted.
- **Between bytes the GB is briefly not armed.** After `GetByte`
  returns, there's a small window where the main loop runs `cp` / `jr`
  before the next `GetByte` re-arms SC. A sync1 that lands in that gap
  comes back as sync3 b2=1 — `xchg` retries the same byte until the
  GB catches up.

## Hot-reloadable words (`words.asm` + `WordSet`)

Driving ~1060 individual `XC!` calls to populate tiles + BG map takes
~20 s and is also flaky: under BGB some of those VRAM writes don't
stick (LCD-mode races, disabling-outside-VBlank lingering state, etc).

The current workflow instead keeps a library of short SM83 routines in
`words.asm`, assembled by rgbasm to a flat binary that `gbforth.py`
uploads into WRAM at $C000 and calls by label name. Each command-line
invocation re-builds, re-uploads, and can run a fresh word:

```sh
python gbforth.py reload     # rebuild words.asm and upload; print the label table
python gbforth.py run ClearBG
python gbforth.py hello      # WordSet-composed sequence that draws H
```

Composition happens on the host — `print_h` is now just:

```python
words.run("LcdOff")
words.run("ResetScrollPal")
words.run("ClearBG")
words.run("CopyTile1")
words.run("SetTopLeft1")
words.run("LcdBgOn")
```

End-to-end `hello` drops from 20 s (1060 XC!) to ~4 s (6 XCALLs on top
of a one-shot upload of ~72 bytes of word code), and the output is
stable.

### Position-independence constraint

Our rgbasm vintage doesn't support `LOAD` blocks, so there's no way to
assemble a ROM output whose bytes resolve labels against a WRAM base
address. Words in `words.asm` therefore must be position-independent:

- internal branches use `jr` (relative)
- absolute addresses are only allowed for fixed hardware regions
  (`$FF40`, `$9800`, etc.) and hardcoded WRAM slots
- **do not `call` another word in the same file** — label addresses
  resolve against `ROM0[$0000]`, and the call would land in cartridge
  ROM instead of WRAM

Compose words on the host (multiple XCALLs) rather than inside each
other. For parameters, use fixed WRAM slots (the `CopyTile1` convention
is "source at $C100, dest at $8010", hardcoded in the word itself).

### Post-XCALL settle

`Link.call()` sleeps ~50 ms after sending the three protocol bytes so
the routine has time to finish and return to the main GetByte loop
before the next command's sync1 arrives. Back-to-back XCALLs without
this settle race the GB's return path and cause byte misalignment —
the 'H' glyph bytes end up partly corrupted with leftover VRAM state.

### Arg slots and generic words

A fixed WRAM arg area at `$C1F0..$C1FF` lets words take parameters:

```
$C1F0/F1  arg0   (16-bit, e.g. source pointer)
$C1F2/F3  arg1   (16-bit, e.g. destination pointer)
$C1F4/F5  arg2   (16-bit, e.g. byte count)
$C1F6     arg3   (8-bit,  e.g. fill value)
```

The host does `XC!` into the arg slots, then `XCALL` the word; each word
loads its arguments off the slots at entry. `Link.store16(addr, val)` is
the little-endian pair-store helper used for pointer-style args.

Generic words in `words.asm`:

- `CopyBytes` — copy arg2 bytes from arg0 to arg1.
- `FillMem`   — fill arg1 bytes starting at arg0 with arg3.
- `PrintString` — walk a NUL-terminated byte string at arg0, copy each
  byte into memory starting at arg1. When arg1 is a BG-map cell and the
  source bytes are ASCII, the ASCII codes double as tile indices — the
  host just makes sure the glyph for each code has been uploaded to
  `$8000 + code*16` beforehand.

### Font and `print` command

`gbforth.py` ships a built-in 1bpp 8x8 font for uppercase ASCII + digits
+ a little punctuation. `glyph_2bpp(c)` expands a 1bpp row to the
2bpp GB tile format (duplicating the row on both bitplanes, which
resolves to colour 3 under the default `$E4` palette).

`python gbforth.py print "HELLO GAMEBOY!" -x 3 -y 8` uploads glyphs only
for the distinct characters in the message (11 here), stages the NUL-
terminated string at `$C200`, sets `arg0 = $C200`, `arg1 = $9800 + y*32 + x`,
and XCALLs `PrintString`. End-to-end ~30 s for a short string — mostly
glyph bytes at ~20 ms/store (4 wire bytes × 5 ms settle).

## Wire-level tuning knobs

`gbforth.py` sleeps 5 ms after each successful exchange. That's much
longer than the ~10 µs hardware gap between transfers, but it keeps
the protocol reliable across Python's GIL, Windows TCP scheduling, and
BGB's rate-limiting. Dropping it below ~1 ms causes byte misalignment
under load.

## REPL

`python gbforth.py repl` keeps the TCP link open, compiles and uploads
the word set once, and reads single-line commands from stdin:

```
gb> @ 0x0104          # XC@ fetch
gb> : 0xC300 0x42     # XC! store
gb> x 0x0008          # XCALL
gb> run ClearBG       # call a named word
gb> print 3 8 HELLO   # render a string at tile coords (3,8)
gb> words             # list loaded words
gb> reload            # rebuild words.asm and re-upload
```

Closer to the Pygmy-Forth target-dev loop the Sergeant paper describes
than one-shot CLI invocations are — amortizes the ~1 s word upload
across a session.

## RLE (partial)

`RleDecode` is a PackBits-style expander: control byte 0..127 means
"literal run of N+1 bytes", 128..255 means "(N & $7F)+2 copies of
next byte". `rle_encode(data)` in `gbforth.py` emits the matching
stream; `WordSet.rle_store(dst, data)` stages the encoded blob at
`$C200`, sets arg0/arg1/arg2, and XCALLs `RleDecode`. Falls back to
`store_many` when the encoding isn't actually smaller.

Single-call round-trip verified for all-zeros, all-FF, alternating,
and the space glyph. Two consecutive `rle_store`s to different
destinations corrupt state: the arg slots read back as zero after
the second call, and BGB eventually trips an invalid-opcode exception
around `$C9xx`, which means something in the handoff is pushing PC
into uninitialized WRAM. The assembled `RleDecode` bytes check out
against the source, and a single invocation always works, so the
trouble is across-call, not intra-word — unshipped until the
reproducer is understood.

## What's missing / ideas

- Track down the two-consecutive-`rle_store` corruption above.
- The font is single-case ASCII; adding lowercase + more punctuation is
  just more entries in `FONT_1BPP`.
- A `bulk_store_many` primitive that stays in a GB-side receive loop
  would theoretically collapse `print "HELLO GAMEBOY!"` from ~30 s to
  ~1 s. First attempt ran into a BGB timing quirk: BGB's emulator runs
  faster than wall-clock during link activity, and its internal
  timestamp outruns ours by tens of seconds after only a few batches;
  that throttling made subsequent transfers pile up latency linearly
  (first batch ~1.4 s, fifth batch ~7 s). Needs more digging into the
  BGB side to make it reliable.
- Real hardware path (flash cart + USB serial adapter wired to the
  link port) is unexplored; the protocol half is already portable,
  only the TCP-to-serial shim would change.
