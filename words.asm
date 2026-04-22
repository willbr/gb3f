; Hot-reloadable Game Boy words, uploaded into WRAM at $C000 by gbforth.py.
;
; Each word is position-independent: only `jr` for internal branches, only
; absolute addresses for fixed hardware regions (I/O, VRAM, hardcoded WRAM
; slots). **Do not `call` another word in this file** — labels here resolve
; to $00XX while the code runs at $C0XX, so the call would jump into ROM.
; Composition happens on the host, one XCALL per word.
;
; WRAM layout the host and these words agree on:
;   $C000..$C0FF  code (this file)
;   $C100..$C10F  glyph data buffer for CopyTile1
;   $C1F0..$C1FF  word argument slots (set by host via XC! before XCALL):
;                   $C1F0/F1  arg0 (16-bit, e.g. source pointer)
;                   $C1F2/F3  arg1 (16-bit, e.g. destination pointer)
;                   $C1F4/F5  arg2 (16-bit, e.g. byte count)
;                   $C1F6     arg3 (8-bit, e.g. fill value)
;   $C200..       host-staged data (strings, uploaded glyphs, etc.)
;
; Build:
;   rgbasm  -o words.o words.asm
;   rgblink -o words.bin -n words.sym words.o

ARG0 EQU $C1F0    ; 16-bit
ARG1 EQU $C1F2    ; 16-bit
ARG2 EQU $C1F4    ; 16-bit
ARG3 EQU $C1F6    ; 8-bit

SECTION "Words", ROM0[$0000]

; ---------------------------------------------------------------------
; LcdOff :: ( -- )   Turn the LCD off.
LcdOff::
    xor a
    ldh [$40], a
    ret

; ---------------------------------------------------------------------
; LcdBgOn :: ( -- )  Turn the LCD on with BG enabled, tile data at $8000,
;                    BG map at $9800 (LCDC = $91).
LcdBgOn::
    ld a, $91
    ldh [$40], a
    ret

; ---------------------------------------------------------------------
; ResetScrollPal :: ( -- )  SCY=SCX=0, BGP=$E4 (standard 4-shade palette).
ResetScrollPal::
    xor a
    ldh [$42], a
    ldh [$43], a
    ld a, $E4
    ldh [$47], a
    ret

; ---------------------------------------------------------------------
; ClearBG :: ( -- )  Zero-fill the BG map at $9800..$9BFF (1024 bytes).
ClearBG::
    ld hl, $9800
    ld b, 4                  ; 4 * 256 = 1024 iterations
    xor a
.outer:
    ld c, 0                  ; 0 means 256 inner iters
.inner:
    ld [hl+], a
    dec c
    jr nz, .inner
    dec b
    jr nz, .outer
    ret

; ---------------------------------------------------------------------
; ClearVRAM :: ( -- )  Zero-fill all of VRAM $8000..$9FFF (8192 bytes).
ClearVRAM::
    ld hl, $8000
    ld b, 32
    xor a
.outer:
    ld c, 0
.inner:
    ld [hl+], a
    dec c
    jr nz, .inner
    dec b
    jr nz, .outer
    ret

; ---------------------------------------------------------------------
; CopyTile1 :: ( -- )  Copy 16 bytes from $C100 to $8010 (tile 1).
;              Callers upload glyph data to $C100 before invoking.
CopyTile1::
    ld hl, $C100
    ld de, $8010
    ld b, 16
.loop:
    ld a, [hl+]
    ld [de], a
    inc de
    dec b
    jr nz, .loop
    ret

; ---------------------------------------------------------------------
; SetTopLeft1 :: ( -- )  Write 1 to $9800 so the top-left BG cell uses tile 1.
SetTopLeft1::
    ld a, 1
    ld [$9800], a
    ret

; ---------------------------------------------------------------------
; CopyBytes :: ( -- )  Copy arg2 bytes from arg0 to arg1.
;   arg0 = source ptr, arg1 = dest ptr, arg2 = byte count.
CopyBytes::
    ld a, [ARG0]
    ld l, a
    ld a, [ARG0+1]
    ld h, a
    ld a, [ARG1]
    ld e, a
    ld a, [ARG1+1]
    ld d, a
    ld a, [ARG2]
    ld c, a
    ld a, [ARG2+1]
    ld b, a
.loop:
    ld a, [hl+]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

; ---------------------------------------------------------------------
; FillMem :: ( -- )  Write arg3 to arg1 bytes starting at arg0.
;   arg0 = dest ptr, arg1 = byte count, arg3 = fill value.
FillMem::
    ld a, [ARG0]
    ld l, a
    ld a, [ARG0+1]
    ld h, a
    ld a, [ARG1]
    ld c, a
    ld a, [ARG1+1]
    ld b, a
    ld a, [ARG3]
    ld d, a           ; save fill value
.loop:
    ld a, d
    ld [hl+], a
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

; ---------------------------------------------------------------------
; Checksum :: ( -- )  XOR-fold arg1 bytes starting at arg0; write the
; 8-bit result into arg3. Host compares against its own XOR of the
; bytes it uploaded — one fetch confirms the whole block arrived
; uncorrupted. If the check fails the host can retry.
;   arg0 = source ptr, arg1 = byte count, arg3 = result out.
Checksum::
    ld a, [ARG0]
    ld l, a
    ld a, [ARG0+1]
    ld h, a
    ld a, [ARG1]
    ld c, a
    ld a, [ARG1+1]
    ld b, a
    xor a
    ld d, a            ; accumulator
.loop:
    ld a, [hl+]
    xor d
    ld d, a
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ld a, d
    ld [ARG3], a
    ret

; ---------------------------------------------------------------------
; PrintString :: ( -- )  Write a NUL-terminated byte string to the BG map
; (or any mem region). Each source byte becomes one destination byte, so
; if the dest is the BG map and the source chars are ASCII, the ASCII
; code doubles as the tile index — the host just needs to make sure the
; glyph for that code is at $8000 + code*16.
;   arg0 = source ptr (string), arg1 = dest ptr (BG map cell).
PrintString::
    ld a, [ARG0]
    ld l, a
    ld a, [ARG0+1]
    ld h, a
    ld a, [ARG1]
    ld e, a
    ld a, [ARG1+1]
    ld d, a
.loop:
    ld a, [hl+]
    or a
    jr z, .done
    ld [de], a
    inc de
    jr .loop
.done:
    ret
