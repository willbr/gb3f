; Hot-reloadable Game Boy words, uploaded into WRAM at $C000 by gbforth.py.
;
; Each word is position-independent: only `jr` for internal branches, only
; absolute addresses for fixed hardware regions (I/O, VRAM, hardcoded WRAM
; slots). **Do not `call` another word in this file** — labels here resolve
; to $00XX while the code runs at $C0XX, so the call would jump into ROM.
; Composition happens on the host, one XCALL per word.
;
; Convention:
;   $C000..$C0FF  code (this file)
;   $C100..$C10F  glyph data buffer for CopyTile
;
; Build:
;   rgbasm  -o words.o words.asm
;   rgblink -o words.bin -n words.sym words.o

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
