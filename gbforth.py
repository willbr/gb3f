"""Host-side driver for the 3-instruction Forth running on a Game Boy.

Talks to BGB over its TCP link-cable protocol. We act as the serial master,
clocking each byte in/out by exchanging sync1/sync2 packets.
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import time


PACKET = struct.Struct("<BBBBI")

CMD_VERSION = 1
CMD_JOYPAD = 101
CMD_SYNC1 = 104
CMD_SYNC2 = 105
CMD_SYNC3 = 106
CMD_STATUS = 108
CMD_WANTDISCONNECT = 109


def pack(b1, b2=0, b3=0, b4=0, i1=0):
    return PACKET.pack(b1, b2, b3, b4, i1 & 0x7FFFFFFF)


TICK_HZ = 2 * 1024 * 1024  # 2 MiHz, per BGB protocol


class Link:
    def __init__(self, host="127.0.0.1", port=8765, verbose=False):
        self.verbose = verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.connect((host, port))
        self._t0 = time.monotonic()
        # Version handshake: both sides must send and verify the same version.
        self.sock.sendall(pack(CMD_VERSION, 1, 4, 0, 0))
        # flags: running=1, paused=0, supportreconnect=1 -> wantdisconnect is honored
        self.sock.sendall(pack(CMD_STATUS, 0b101, 0, 0, 0))
        remote_version = self._await(CMD_VERSION)
        if remote_version[1:4] != (1, 4, 0):
            raise RuntimeError(f"unexpected BGB version: {remote_version}")
        # BGB starts emulation paused and unpauses once it has synced
        # timestamps with us. Send a sync3 ping so it has a fresh remote ts
        # to latch onto, then give it a moment to catch up.
        self.sock.sendall(pack(CMD_SYNC3, 0, 0, 0, self.ts))
        time.sleep(0.2)

    @property
    def ts(self):
        # BGB gates emulation on the remote's timestamp; if ours lags behind the
        # emulated clock, BGB stalls waiting for us. Use wall-clock ticks so we
        # stay ahead of the 4.19 MHz CPU emulation.
        return int((time.monotonic() - self._t0) * TICK_HZ) & 0x7FFFFFFF

    def _recv(self):
        buf = b""
        while len(buf) < 8:
            chunk = self.sock.recv(8 - len(buf))
            if not chunk:
                raise EOFError("BGB closed the link")
            buf += chunk
        pkt = PACKET.unpack(buf)
        if self.verbose:
            print(f"  <- {pkt}", file=sys.stderr)
        return pkt

    def _send(self, *a, **kw):
        if self.verbose:
            print(f"  -> cmd={a[0]} args={a[1:]} ts={kw.get('i1', 0)}", file=sys.stderr)
        self.sock.sendall(pack(*a, **kw))

    def _await(self, want_cmd):
        while True:
            pkt = self._recv()
            cmd = pkt[0]
            if cmd == want_cmd:
                return pkt
            # Absorb background packets (status, timestamp syncs, joypad, etc.)
            if cmd == CMD_STATUS:
                continue
            if cmd == CMD_SYNC3:
                continue
            if cmd == CMD_JOYPAD:
                continue
            # Unknown — ignore rather than hang.

    def xchg(self, byte):
        """Send one byte as master, return the byte clocked back from the GB.

        If the GB side hasn't armed SC=$80 yet (right after boot, or during
        non-serial code paths), BGB replies with sync3 b2=1 (ack-no-transfer).
        Retry until the transfer actually happens — the bytes are the protocol,
        we never want to drop one.
        """
        while True:
            # b3 control: bit0=1 master-side, bit7=1 always.
            self._send(CMD_SYNC1, byte & 0xFF, 0x81, 0, i1=self.ts)
            while True:
                pkt = self._recv()
                cmd = pkt[0]
                if cmd == CMD_SYNC2:
                    # Small settle-delay so the GB has time to re-arm SC=$80
                    # before our next sync1 arrives; otherwise we race the
                    # ~10µs gap between SB-read and the next GetByte, and
                    # at high throughput some bytes end up dropped by BGB.
                    time.sleep(0.005)
                    return pkt[1]
                if cmd == CMD_SYNC3 and pkt[1] == 1:
                    # GB wasn't armed yet; break out of inner loop to retry.
                    time.sleep(0.001)
                    break
                # CMD_SYNC3 with b2=0 (timestamp sync), CMD_STATUS, CMD_JOYPAD,
                # or a duplicate CMD_VERSION: keep draining.

    # --- Three-instruction Forth primitives ---

    def fetch(self, addr):
        self.xchg(0x01)
        self.xchg((addr >> 8) & 0xFF)
        self.xchg(addr & 0xFF)
        self.xchg(0x00)         # first dummy — GB runs SendByte during this window
        return self.xchg(0x00)  # second dummy actually clocks the result out

    def store(self, addr, val):
        self.xchg(0x02)
        self.xchg((addr >> 8) & 0xFF)
        self.xchg(addr & 0xFF)
        self.xchg(val & 0xFF)

    def call(self, addr, settle=0.05):
        """XCALL the routine at `addr`. Sleeps `settle` seconds afterwards so
        the routine has time to finish and return to the main GetByte loop
        before the next command arrives — without this, back-to-back XCALLs
        can race the routine's execution and the next sync1 misaligns."""
        self.xchg(0x03)
        self.xchg((addr >> 8) & 0xFF)
        self.xchg(addr & 0xFF)
        if settle:
            time.sleep(settle)

    def store_many(self, addr, data):
        for i, b in enumerate(data):
            self.store(addr + i, b)

    def store16(self, addr, value):
        """Little-endian 16-bit store, for filling a word-arg slot."""
        self.store(addr, value & 0xFF)
        self.store(addr + 1, (value >> 8) & 0xFF)

    def close(self):
        # Tell BGB this is a user-initiated disconnect so it goes back to
        # listening instead of trying to reconnect to us.
        try:
            self._send(CMD_WANTDISCONNECT, 0, 0, 0)
        except OSError:
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# An 'H' glyph, 8x8, using color 1 (only the low bitplane is set).
GLYPH_H = bytes([
    0b01100110, 0x00,
    0b01100110, 0x00,
    0b01100110, 0x00,
    0b01111110, 0x00,
    0b01111110, 0x00,
    0b01100110, 0x00,
    0b01100110, 0x00,
    0b00000000, 0x00,
])

# A solid blank tile (color 0 everywhere).
GLYPH_BLANK = bytes(16)


# 8x8 bitmap font (1 byte per row, 8 rows per glyph) for a printable-ASCII
# subset — enough for short demo strings. Each row bit set = inked pixel.
# We convert to GB 2bpp at upload time (same byte on both planes → color 3
# = black under the default $E4 palette).
FONT_1BPP = {
    ' ': b'\x00\x00\x00\x00\x00\x00\x00\x00',
    '!': b'\x18\x18\x18\x18\x00\x00\x18\x00',
    '.': b'\x00\x00\x00\x00\x00\x18\x18\x00',
    ',': b'\x00\x00\x00\x00\x00\x18\x18\x30',
    ':': b'\x00\x18\x18\x00\x00\x18\x18\x00',
    '-': b'\x00\x00\x00\x7E\x00\x00\x00\x00',
    '?': b'\x3C\x66\x06\x0C\x18\x00\x18\x00',
    "'": b'\x18\x18\x18\x00\x00\x00\x00\x00',
    '0': b'\x3C\x66\x6E\x76\x66\x66\x3C\x00',
    '1': b'\x18\x38\x18\x18\x18\x18\x7E\x00',
    '2': b'\x3C\x66\x06\x0C\x30\x60\x7E\x00',
    '3': b'\x3C\x66\x06\x1C\x06\x66\x3C\x00',
    '4': b'\x0C\x1C\x3C\x6C\x7E\x0C\x0C\x00',
    '5': b'\x7E\x60\x7C\x06\x06\x66\x3C\x00',
    '6': b'\x3C\x66\x60\x7C\x66\x66\x3C\x00',
    '7': b'\x7E\x06\x0C\x18\x30\x30\x30\x00',
    '8': b'\x3C\x66\x66\x3C\x66\x66\x3C\x00',
    '9': b'\x3C\x66\x66\x3E\x06\x66\x3C\x00',
    'A': b'\x18\x3C\x66\x66\x7E\x66\x66\x00',
    'B': b'\x7C\x66\x66\x7C\x66\x66\x7C\x00',
    'C': b'\x3C\x66\x60\x60\x60\x66\x3C\x00',
    'D': b'\x78\x6C\x66\x66\x66\x6C\x78\x00',
    'E': b'\x7E\x60\x60\x7C\x60\x60\x7E\x00',
    'F': b'\x7E\x60\x60\x7C\x60\x60\x60\x00',
    'G': b'\x3C\x66\x60\x6E\x66\x66\x3E\x00',
    'H': b'\x66\x66\x66\x7E\x66\x66\x66\x00',
    'I': b'\x3C\x18\x18\x18\x18\x18\x3C\x00',
    'J': b'\x1E\x0C\x0C\x0C\x0C\x6C\x38\x00',
    'K': b'\x66\x6C\x78\x70\x78\x6C\x66\x00',
    'L': b'\x60\x60\x60\x60\x60\x60\x7E\x00',
    'M': b'\x63\x77\x7F\x6B\x63\x63\x63\x00',
    'N': b'\x63\x73\x7B\x6F\x67\x63\x63\x00',
    'O': b'\x3C\x66\x66\x66\x66\x66\x3C\x00',
    'P': b'\x7C\x66\x66\x7C\x60\x60\x60\x00',
    'Q': b'\x3C\x66\x66\x66\x6A\x6C\x36\x00',
    'R': b'\x7C\x66\x66\x7C\x78\x6C\x66\x00',
    'S': b'\x3C\x66\x60\x3C\x06\x66\x3C\x00',
    'T': b'\x7E\x18\x18\x18\x18\x18\x18\x00',
    'U': b'\x66\x66\x66\x66\x66\x66\x3C\x00',
    'V': b'\x66\x66\x66\x66\x66\x3C\x18\x00',
    'W': b'\x63\x63\x63\x6B\x7F\x77\x63\x00',
    'X': b'\x66\x66\x3C\x18\x3C\x66\x66\x00',
    'Y': b'\x66\x66\x66\x3C\x18\x18\x18\x00',
    'Z': b'\x7E\x06\x0C\x18\x30\x60\x7E\x00',
}


def glyph_2bpp(ch):
    """Expand a 1bpp 8x8 glyph to GB tile format (16 bytes, color 3)."""
    rows = FONT_1BPP.get(ch.upper(), FONT_1BPP[' '])
    out = bytearray()
    for r in rows:
        out.append(r)  # low bitplane
        out.append(r)  # high bitplane (same bits → color 3 under BGP=$E4)
    return bytes(out)


WORDS_BASE = 0xC000
ARG0 = 0xC1F0
ARG1 = 0xC1F2
ARG2 = 0xC1F4
ARG3 = 0xC1F6
STAGING = 0xC200       # where host-staged data lives (strings, tile bytes)


class WordSet:
    """A set of SM83 words compiled from `words.asm`, uploaded into WRAM,
    and callable by label name.

    Each invocation of `compile_and_upload()` re-runs rgbasm/rgblink, reads
    the fresh `words.sym`, and stores the first N bytes of `words.bin` into
    WRAM at `base` (default $C000). After that, `run(name)` issues a single
    XCALL at `base + labels[name]`.

    Words in `words.asm` must be position-independent: internal branches
    use `jr`; absolute addresses are only allowed for fixed hardware and
    WRAM slots, never for other labels in the same file.
    """
    def __init__(self, link, base=WORDS_BASE):
        self.link = link
        self.base = base
        self.labels = {}

    @staticmethod
    def compile(asm="words.asm"):
        """Run rgbasm + rgblink. Returns (bin_path, sym_path)."""
        stem, _ = os.path.splitext(asm)
        o, b, s = stem + ".o", stem + ".bin", stem + ".sym"
        subprocess.check_call(["rgbasm", "-o", o, asm])
        subprocess.check_call(["rgblink", "-o", b, "-n", s, o])
        return b, s

    @staticmethod
    def parse_sym(path):
        labels = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(";"):
                    continue
                # "00:0123 Label"
                bank_addr, _, name = line.partition(" ")
                if ":" not in bank_addr or not name:
                    continue
                _, addr = bank_addr.split(":")
                labels[name] = int(addr, 16)
        return labels

    def compile_and_upload(self, asm="words.asm"):
        bin_path, sym_path = self.compile(asm)
        self.labels = self.parse_sym(sym_path)
        # Upload only as much of the binary as we actually use (rgblink pads
        # the output to a full ROM bank).
        end = max(self.labels.values()) + 64
        with open(bin_path, "rb") as f:
            data = f.read(end)
        self.link.store_many(self.base, data)

    def addr(self, name):
        return self.base + self.labels[name]

    def run(self, name):
        self.link.call(self.addr(name))


def print_h(link, words=None):
    """Put a single 'H' in the top-left corner of the DMG screen.

    Composes leaf words from `words.asm` on the host side — exactly the
    distributed-Forth idea Sergeant's paper describes.
    """
    if words is None:
        words = WordSet(link)
        words.compile_and_upload()
    link.store_many(0xC100, GLYPH_H)   # CopyTile1 reads from $C100
    words.run("LcdOff")
    words.run("ResetScrollPal")
    words.run("ClearBG")
    words.run("CopyTile1")
    words.run("SetTopLeft1")
    words.run("LcdBgOn")


def print_string(link, x, y, message, words=None):
    """Paint an ASCII string onto the BG map at tile coordinates (x, y).

    We use tile index = ASCII code, so each character's glyph must be at
    $8000 + ord(c)*16. Only the glyphs for distinct characters in the
    message are uploaded — enough font for the word, no more.
    """
    if words is None:
        words = WordSet(link)
        words.compile_and_upload()

    words.run("LcdOff")
    words.run("ResetScrollPal")
    words.run("ClearBG")

    # Upload glyphs for each unique character.
    for ch in set(message.upper()):
        tile = ord(ch)
        link.store_many(0x8000 + tile * 16, glyph_2bpp(ch))

    # Stage the null-terminated message in WRAM and point PrintString at it.
    msg = message.upper().encode("ascii") + b"\0"
    link.store_many(STAGING, msg)
    link.store16(ARG0, STAGING)
    link.store16(ARG1, 0x9800 + y * 32 + x)
    words.run("PrintString")

    words.run("LcdBgOn")


def stress(link, n=512):
    """Store a deterministic byte pattern across $C000..$C000+n, then read back."""
    bad = 0
    for i in range(n):
        link.store(0xC000 + i, (i * 31 + 7) & 0xFF)
    for i in range(n):
        got = link.fetch(0xC000 + i)
        want = (i * 31 + 7) & 0xFF
        if got != want:
            bad += 1
            if bad <= 5:
                print(f"  MISMATCH [{0xC000+i:04X}] want {want:02X} got {got:02X}")
    print(f"  {bad}/{n} mismatches")


def selftest(link):
    stress(link)
    cases = [
        (0x0100, 0x00, "entrypoint nop"),
        (0x0101, 0xC3, "jp opcode"),
        (0x0104, 0xCE, "Nintendo logo byte 0"),
        (0x0105, 0xED, "Nintendo logo byte 1"),
        (0x0150, 0xF3, "Start: di"),
        (0x0008, 0x40, "StopHere: ld b, b"),
    ]
    fails = 0
    for addr, expect, name in cases:
        got = link.fetch(addr)
        ok = "OK " if got == expect else "FAIL"
        if got != expect:
            fails += 1
        print(f"  {ok}  [{addr:04X}] = {got:02X}  (expected {expect:02X}, {name})")

    print("  round-trip writes to WRAM:")
    for addr, val in [(0xC000, 0xAB), (0xC001, 0xCD), (0xDFFF, 0x5A)]:
        link.store(addr, val)
        got = link.fetch(addr)
        ok = "OK " if got == val else "FAIL"
        if got != val:
            fails += 1
        print(f"  {ok}  [{addr:04X}] wrote {val:02X}, read {got:02X}")

    print(f"  {fails} failure(s)")


def vram_stress(link, n=256):
    """Same as stress() but against VRAM at $8000. LCD is assumed off already."""
    bad = 0
    for i in range(n):
        link.store(0x8000 + i, (i * 31 + 7) & 0xFF)
    for i in range(n):
        got = link.fetch(0x8000 + i)
        want = (i * 31 + 7) & 0xFF
        if got != want:
            bad += 1
            if bad <= 5:
                print(f"  VRAM MISMATCH [{0x8000+i:04X}] want {want:02X} got {got:02X}")
    print(f"  VRAM {bad}/{n} mismatches")


def diagnose(link):
    # Sample LY twice to confirm LCD is actually running
    ly1 = link.fetch(0xFF44)
    ly2 = link.fetch(0xFF44)
    lcdc = link.fetch(0xFF40)
    print(f"  boot:  LCDC={lcdc:02X} LY={ly1:02X}->{ly2:02X}")
    # Turn LCD off and recheck
    link.store(0xFF40, 0x00)
    lcdc2 = link.fetch(0xFF40)
    ly3 = link.fetch(0xFF44)
    print(f"  after LCDC<-0:  LCDC={lcdc2:02X} LY={ly3:02X}")
    vram_stress(link)

    print_h(link)
    print("after hello:")
    print(f"  LCDC  [FF40] = {link.fetch(0xFF40):02X}  (want 91)")
    print(f"  BGP   [FF47] = {link.fetch(0xFF47):02X}  (want E4)")
    print(f"  SCX   [FF43] = {link.fetch(0xFF43):02X}  (want 00)")
    print(f"  SCY   [FF42] = {link.fetch(0xFF42):02X}  (want 00)")
    print("  tile 1 data [8010..801F]:")
    for a in range(0x8010, 0x8020):
        print(f"    [{a:04X}] = {link.fetch(a):02X}")
    print("  BG map [9800..9807]:")
    for a in range(0x9800, 0x9808):
        print(f"    [{a:04X}] = {link.fetch(a):02X}")


REPL_HELP = """\
 !                          show this help
 @ <addr>                   XC@ — fetch a byte                (addr hex ok)
 : <addr> <val>             XC! — store a byte
 x <addr>                   XCALL — call subroutine
 reload                     rebuild words.asm and re-upload
 words                      list loaded words and their runtime addresses
 run <word>                 XCALL a named word
 print <x> <y> <message>    render a string at tile coords (x, y)
 hello                      draw the original "H" demo
 q / quit / Ctrl-D          exit
"""


def _parse_int(tok):
    return int(tok, 0)


def repl(link):
    """Interactive prompt that keeps the TCP session open and re-uses one
    word-set upload across many commands — closer to the ergonomics of
    Sergeant's Pygmy-driven target dev loop."""
    ws = WordSet(link)
    print("gb3f repl — type `!` for help, Ctrl-D / q to quit.")
    print("loading words...", end="", flush=True)
    ws.compile_and_upload()
    print(f" {len(ws.labels)} words at ${ws.base:04X}")
    while True:
        try:
            line = input("gb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        tokens = line.split(None, 3)
        cmd = tokens[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                return
            elif cmd == "!" or cmd == "help":
                print(REPL_HELP)
            elif cmd == "@":
                addr = _parse_int(tokens[1])
                print(f"[{addr:04X}] = {link.fetch(addr):02X}")
            elif cmd == ":":
                addr = _parse_int(tokens[1])
                val = _parse_int(tokens[2])
                link.store(addr, val)
            elif cmd == "x":
                link.call(_parse_int(tokens[1]))
            elif cmd == "reload":
                t0 = time.time()
                ws.compile_and_upload()
                print(f"{len(ws.labels)} words in {time.time()-t0:.2f}s")
            elif cmd == "words":
                for name in sorted(ws.labels, key=ws.labels.get):
                    print(f"  ${ws.addr(name):04X}  {name}")
            elif cmd == "run":
                name = tokens[1]
                if name not in ws.labels:
                    print(f"unknown word '{name}'")
                else:
                    ws.run(name)
            elif cmd == "print":
                x = int(tokens[1])
                y = int(tokens[2])
                message = tokens[3] if len(tokens) > 3 else ""
                t0 = time.time()
                print_string(link, x, y, message, ws)
                print(f"done in {time.time()-t0:.2f}s")
            elif cmd == "hello":
                print_h(link, ws)
            else:
                print(f"unknown: {cmd!r} (type ! for help)")
        except (IndexError, ValueError) as e:
            print(f"error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--verbose", "-v", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_hello = sub.add_parser("hello", help="print 'H' in the top-left of the screen")
    p_hello.add_argument("--halt", action="store_true",
                         help="XCALL $0008 after drawing, so a headless BGB can auto-exit")
    sub.add_parser("selftest", help="peek ROM bytes and round-trip RAM to verify the link")
    sub.add_parser("diag", help="run hello then peek back LCDC, tile data, and BG map entry")
    sub.add_parser("reload", help="rebuild words.asm and upload to WRAM")

    p_run = sub.add_parser("run", help="run a named word (implies reload)")
    p_run.add_argument("word")

    p_print = sub.add_parser("print", help="render an ASCII string on the GB screen")
    p_print.add_argument("message")
    p_print.add_argument("-x", type=int, default=5, help="BG-map column (0..19)")
    p_print.add_argument("-y", type=int, default=8, help="BG-map row (0..17)")
    p_print.add_argument("--halt", action="store_true",
                         help="XCALL $0008 after drawing, so a headless BGB can auto-exit")

    sub.add_parser("repl", help="interactive prompt that keeps the link open and the word set loaded")

    p_peek = sub.add_parser("peek", help="fetch one byte")
    p_peek.add_argument("addr", type=lambda s: int(s, 0))

    p_poke = sub.add_parser("poke", help="store one byte")
    p_poke.add_argument("addr", type=lambda s: int(s, 0))
    p_poke.add_argument("value", type=lambda s: int(s, 0))

    p_call = sub.add_parser("call", help="call subroutine at address")
    p_call.add_argument("addr", type=lambda s: int(s, 0))

    args = ap.parse_args()
    with Link(args.host, args.port, verbose=args.verbose) as link:
        if args.cmd == "hello":
            t0 = time.time()
            print_h(link)
            print(f"done in {time.time()-t0:.2f}s")
            if args.halt:
                link.call(0x0008)
        elif args.cmd == "selftest":
            selftest(link)
        elif args.cmd == "diag":
            diagnose(link)
        elif args.cmd == "peek":
            val = link.fetch(args.addr)
            print(f"[{args.addr:04X}] = {val:02X}")
        elif args.cmd == "poke":
            link.store(args.addr, args.value)
        elif args.cmd == "call":
            link.call(args.addr)
        elif args.cmd == "reload":
            ws = WordSet(link)
            ws.compile_and_upload()
            for name in sorted(ws.labels, key=ws.labels.get):
                print(f"  {ws.addr(name):04X}  {name}")
        elif args.cmd == "run":
            ws = WordSet(link)
            ws.compile_and_upload()
            if args.word not in ws.labels:
                available = ", ".join(sorted(ws.labels))
                raise SystemExit(f"unknown word '{args.word}'. available: {available}")
            ws.run(args.word)
        elif args.cmd == "print":
            t0 = time.time()
            print_string(link, args.x, args.y, args.message)
            print(f"done in {time.time()-t0:.2f}s")
            if args.halt:
                link.call(0x0008)
        elif args.cmd == "repl":
            repl(link)


if __name__ == "__main__":
    main()
