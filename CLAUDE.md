# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A 3-instruction Forth for the Game Boy, per Frank Sergeant's 1991 paper
(`reference/forth.md`). A 66-byte SM83 monitor ROM implements only three
primitives — XC@ (fetch), XC! (store), XCALL — and a Python host client
drives them over BGB's TCP link-cable protocol. Everything bulkier than a
single byte fetch/store is composed on the host.

Full prose explanation is in `readme.md` (overview) and `NOTES.md` (deep
dive on protocol quirks). Always consult `NOTES.md` before touching the
host protocol or the ROM's dispatch loop — it documents hard-won quirks
that aren't obvious from the code.

## Two-process architecture

- **BGB (`bgb64.exe`)** runs the emulator and listens on TCP :8765, speaking
  the 8-byte-packet link protocol documented in
  `reference/bgb 1.4 link protocol.html`. Must run **windowed** —
  `-headless` disables LCD emulation, which silently no-ops all VRAM
  reads and writes.
- **`gbforth.py`** is the TCP client. Every CLI invocation opens a new
  session and closes it cleanly via `wantdisconnect` so BGB re-listens.

The "3-instruction" constraint means adding new capability does not mean
adding a new opcode to the ROM. Instead, assemble a small SM83 subroutine
as a Python `bytes(...)` literal, upload it with `store_many(0xC000, ...)`,
and fire it with `call(0xC000)`. `print_h` in `gbforth.py` is the worked
example — it replaces ~1060 individual XC! calls (20 s, flaky) with one
XCALL of an uploaded routine (1.2 s, deterministic). Any new high-level
operation should follow that shape.

## Common commands

```sh
# Build (requires rgbds on PATH; this repo's toolchain is an OLD rgbasm
# without `@` as a constant or `ds N, fill` — see `3forth.asm` for the
# compatible idioms).
rgbasm  -o 3forth.o  3forth.asm
rgblink -o 3forth.gb -n 3forth.sym 3forth.o
rgbfix  -p 0 -v 3forth.gb

# Run BGB listening for the link client. The -br/-autoexit/-screenonexit
# trio is the scripted-halt path: `python gbforth.py ... --halt` will
# XCALL $0008 (our `ld b,b`), BGB breaks, autoexits, saves a BMP.
bgb64 -rom ./3forth.gb -listen 127.0.0.1:8765 \
      -nowarn -nowriteini -br 8 -autoexit \
      -screenonexit "$(pwd)/hello.bmp"

# Drive the ROM (each call is a fresh TCP session; BGB must still be up).
python gbforth.py selftest          # 512-byte WRAM round-trip + ROM peeks
python gbforth.py peek 0x0104       # expect CE (Nintendo logo byte 0)
python gbforth.py poke 0xC000 42
python gbforth.py call 0xC000
python gbforth.py hello --halt      # full end-to-end demo
python bmp2png.py hello.bmp hello.png
```

There are no tests beyond `gbforth.py selftest`. To sanity-check a change,
start BGB + run selftest — both the ROM peeks and the WRAM round-trip
must report 0 failures. `diag` runs `print_h` and peeks back the key
addresses; useful when a render change doesn't look right on screen.

## Protocol invariants the code depends on

These are the non-obvious things `gbforth.py` must keep doing, documented
with the reasoning in `NOTES.md`:

- **Timestamps must track wall-clock in 2 MiHz ticks.** BGB stalls GB
  emulation if the remote's timestamp lags its emulated clock. `Link.ts`
  is a property for this reason.
- **`fetch()` sends 5 bytes, not 4.** `sync2.b2` reflects the GB's SB
  with a one-transfer delay — the second dummy byte at the end is what
  actually clocks the real response out.
- **`xchg()` retries on `sync3 b2=1`.** Between calls to `GetByte` the
  GB briefly has SC unarmed; a sync1 landing in that gap must be resent.
- **Send `wantdisconnect` (109) on `__exit__`.** Otherwise BGB marks the
  drop as unintentional and stops re-listening until restart.
- **~5 ms settle sleep after each successful `xchg`.** Below ~1 ms the
  protocol misaligns bytes under load.

## ROM conventions

`3forth.asm` places three sections deliberately:

- `$0008`: `ld b,b` / `jr -2` halt target. BGB's `-br 8` breakpoint lands
  here. Used as the scripted-exit path for screenshot capture.
- `$0100`: cartridge header (`nop; jp Start` + padding). `rgbfix -v`
  fills the Nintendo logo and checksums in the padded bytes.
- `$0150`: the dispatch loop and GetByte/SendByte/GetAddr/CallHL helpers.

`CallHL` is `jp hl` *wrapped in a `call`* from the main loop so the
callee's `RET` pops the right return address. Uploaded routines must end
in `RET` and leave the GB in a state where `GetByte` can safely arm SC
again (i.e., don't trap the CPU in an infinite loop unless you intend the
session to end there).

## Reference material

`reference/` is the source-of-truth archive, not an exported copy — keep
it intact. `session-log.md` inside it is the reconstructed narrative of
the original build session and is the fastest way to understand *why*
pieces of `gbforth.py` look the way they do.
