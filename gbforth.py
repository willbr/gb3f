"""Host-side driver for the 3-instruction Forth running on a Game Boy.

Talks to BGB over its TCP link-cable protocol. We act as the serial master,
clocking each byte in/out by exchanging sync1/sync2 packets.
"""
import argparse
import socket
import struct
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

    def call(self, addr):
        self.xchg(0x03)
        self.xchg((addr >> 8) & 0xFF)
        self.xchg(addr & 0xFF)

    def store_many(self, addr, data):
        for i, b in enumerate(data):
            self.store(addr + i, b)

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


def print_h(link):
    """Put a single 'H' in the top-left corner of the DMG screen.

    Rather than driving ~1060 individual XC! calls (slow, and flaky under BGB
    with LCD still partially live), we upload a compiled subroutine into WRAM
    and XCALL it. The three primitives (XC@/XC!/XCALL) are plenty to build
    higher-level operations — this is exactly the distributed-Forth idea.
    """
    # Stage 1: minimal — set BG palette, copy 'H' glyph, set map[0]=1, re-enable LCD.
    # Skip the blank-tile-0 and BG-map-clear stages; we rely on the random boot VRAM
    # pattern not to render nonsense everywhere. (A cleaner version would upload a
    # clear routine too.)
    routine = bytes([
        0xAF, 0xE0, 0x40,                 # xor a ; ldh [$40],a   -- LCD off
        0x3E, 0xE4, 0xE0, 0x47,           # ld a,$E4 ; ldh [$47],a -- BGP
        0xAF, 0xE0, 0x42, 0xE0, 0x43,     # xor a ; ldh [$42],a ; ldh [$43],a
        0x21, 0x00, 0xC1,                 # ld hl,$C100            -- H glyph source
        0x11, 0x10, 0x80,                 # ld de,$8010            -- tile 1 dest
        0x06, 0x10,                       # ld b,16
        0x2A, 0x12, 0x13, 0x05, 0x20, 0xFA,  # copy: ld a,[hl+] ; ld [de],a ; inc de ; dec b ; jr nz,copy
        0x3E, 0x01, 0xEA, 0x00, 0x98,     # ld a,1 ; ld [$9800],a    -- top-left tile
        0x3E, 0x91, 0xE0, 0x40,           # ld a,$91 ; ldh [$40],a   -- LCD on
        0xC9,                             # ret
    ])
    link.store_many(0xC000, routine)
    link.store_many(0xC100, GLYPH_H)
    link.call(0xC000)


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


if __name__ == "__main__":
    main()
