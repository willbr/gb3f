; 3-instruction Forth bootroom for Game Boy (DMG).
;
; Wire protocol on the link cable (GB is slave, PC is master):
;   01 hi lo       -> GB replies with byte at address hi:lo     (XC@ fetch)
;   02 hi lo val   -> GB stores val at hi:lo                    (XC! store)
;   03 hi lo       -> GB calls subroutine at hi:lo              (XCALL)
;
; Every byte from PC clocks in one byte from GB's SB register, so XC@'s
; response is clocked out by the PC sending one dummy byte after the address.
; The PC is responsible for setting up VRAM, LCDC, palettes, etc. via XC!.

SB  EQU $01   ; serial data register (FF01)
SC  EQU $02   ; serial control register (FF02)

; XCALL $0008 halts the CPU in a tight loop; BGB's -br 0008 breakpoint lands
; here so we can auto-exit with a screenshot for scripted smoke tests.
SECTION "Stop", ROM0[$0008]
StopHere:
    ld b, b
.loop:
    jr .loop

SECTION "Header", ROM0[$100]
    nop
    jp Start
    ds $4C           ; $104..$14F: logo/title/checksum; rgbfix fills this

SECTION "Main", ROM0[$150]
Start:
    di
    ld sp, $FFFE

.loop:
    call GetByte
    cp 1
    jr z, .fetch
    cp 2
    jr z, .store
    cp 3
    jr z, .call
    jr .loop

.fetch:
    call GetAddr
    ld a, [hl]
    call SendByte
    jr .loop

.store:
    call GetAddr
    call GetByte
    ld [hl], a
    jr .loop

.call:
    call GetAddr
    call CallHL
    jr .loop

GetByte:
    ld a, $80
    ldh [SC], a
.wait:
    ldh a, [SC]
    bit 7, a
    jr nz, .wait
    ldh a, [SB]
    ret

SendByte:
    ldh [SB], a
    ld a, $80
    ldh [SC], a
.wait:
    ldh a, [SC]
    bit 7, a
    jr nz, .wait
    ret

GetAddr:
    call GetByte
    ld h, a
    call GetByte
    ld l, a
    ret

CallHL:
    jp hl
