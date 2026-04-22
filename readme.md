# gb3f — 3-instruction Forth on the Game Boy

A tiny DMG cartridge ROM that exposes three primitives (`XC@` fetch, `XC!`
store, `XCALL` subroutine call) to a host PC over the emulated link cable.
All higher-level behaviour lives on the host, composed out of the three
primitives — the Game Boy side stays at 66 bytes. Following Frank Sergeant's
1991 *"A 3-instruction Forth for embedded systems work"* paper
(`reference/forth.md`), ported to SM83 and BGB's TCP link protocol.

End-to-end demo: a Python script tells the ROM to render a string using
a bitmap font it uploads on demand.

![hello.png](screenshots/hello.png)

Produced by `python gbforth.py print "HELLO GAMEBOY!"`. The host assembles
leaf words (`LcdOff`, `ClearBG`, `PrintString`, …) from `words.asm`,
uploads them into WRAM, stages the string + glyphs, fills the word-arg
slots via `XC!`, and fires each word with a single `XCALL`.

## Quick start

```sh
# Build the ROM. Intermediate artifacts land in build/; 3forth.gb is the
# distributable and stays at the repo root.
mkdir -p build
rgbasm  -o build/3forth.o  3forth.asm
rgblink -o 3forth.gb       -n build/3forth.sym build/3forth.o
rgbfix  -p 0 -v 3forth.gb

# Launch BGB (windowed mode — headless disables LCD emulation).
bgb64 -rom ./3forth.gb -listen 127.0.0.1:8765 \
      -nowarn -nowriteini -br 8 -autoexit \
      -screenonexit "$(pwd)/screenshots/hello.bmp"

# Drive it from the host.
python gbforth.py selftest         # round-trips 512 bytes to verify the link
python gbforth.py peek 0x0104      # read one byte (expect CE, Nintendo logo)
python gbforth.py poke 0xC000 42   # write one byte to WRAM
python gbforth.py reload           # rebuild words.asm and upload; list labels
python gbforth.py run ClearBG      # fire any word from the hot-reloadable library
python gbforth.py hello --halt     # paint an H, then XCALL $0008 to exit BGB
python gbforth.py print "HELLO GAMEBOY!" --halt
python gbforth.py repl             # interactive prompt; word set stays loaded
python gbforth.py scroll "HELLO GAMEBOY!"  # host-driven SCX animation
python bmp2png.py screenshots/hello.bmp screenshots/hello.png

# Or run the whole scenario suite at once (builds, drives BGB, emits PNGs):
python run_tests.py
```

## Layout

| Path | What |
| --- | --- |
| `3forth.asm` | SM83 source for the monitor (66 bytes + an `ld b,b` halt at $0008). |
| `3forth.gb` | Assembled cartridge. |
| `words.asm` | Hot-reloadable library of leaf SM83 words, uploaded into WRAM at runtime. |
| `gbforth.py` | BGB TCP link-cable client and CLI (`peek`, `poke`, `call`, `run`, `reload`, `selftest`, `diag`, `hello`, `print`, `repl`, `scroll`). |
| `bmp2png.py` | Converts BGB's BMP screenshots to PNG. |
| `run_tests.py` | Sanity-check suite: builds, drives BGB through every scenario, reports pass/fail. |
| `build/` | Assembler outputs (`.o`, `.sym`, `words.bin`) — gitignored. |
| `screenshots/` | BMPs from BGB (gitignored) and their PNG exports (tracked). |
| `NOTES.md` | Deep dive: protocol details, BGB quirks, the hot-reloadable-words workflow. |
| `reference/` | The source material — Sergeant's paper, Pan Docs, BGB manual, BGB link protocol. |

See `NOTES.md` for how the ROM's dispatch loop works, the host protocol
framing, the timing/lag gotchas (BGB gates emulation on remote timestamps;
`sync2.b2` lags one transfer; `-headless` silently kills VRAM writes), and
why `hello` uploads a routine and `XCALL`s it instead of driving 1000+
individual stores.
