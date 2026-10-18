; TETRIS for the ZX Spectrum (48K/128K)
;
; Assemble with sjasmplus (see build.py). Runs at 50 frames per second,
; locked to the frame interrupt.
;
; Rendering: every cell of the well holds the same 8x8 block bitmap, written
; once when the screen is set up. From then on the game only writes colour
; attributes: one byte per cell. An empty cell is black-on-black, a block is
; black ink on coloured paper, and the ghost piece is coloured ink on black
; (which shows just the block's outline). All drawing happens right after the
; frame interrupt, while the beam is still in the top border, so nothing
; flickers or tears.

        org 32768

ATTRS           equ 0x5800
FONT            equ 0x3C00              ; ROM character set - 32*8
FRAMES          equ 23672
BORDCR          equ 23624

WELL_X          equ 11                  ; screen column of the well's left cell
WELL_Y          equ 2                   ; screen row of the well's top cell
WELL_ATTR       equ ATTRS+WELL_Y*32+WELL_X
NEXT_ROW        equ 5
NEXT_COL        equ 25

EMPTY           equ 0x00                ; field value / attribute of an empty cell
WALL            equ 0xFF                ; field value outside the well
FLASH_ATTR      equ 0x7F                ; solid bright white
DEAD_ATTR       equ 0x38                ; grey block for the game-over fill
FRAME_ATTR      equ 0x07
LABEL_ATTR      equ 0x4F                ; bright white on blue
VALUE_ATTR      equ 0x46                ; bright yellow on black

DAS_DELAY       equ 9                   ; frames before sideways auto-repeat
DAS_REPEAT      equ 2                   ; frames between repeats
SOFT_INT        equ 2                   ; frames per row while soft dropping
LOCK_DELAY      equ 25                  ; frames a grounded piece can still be moved
MAX_RESETS      equ 12                  ; lock-delay resets allowed per piece
MAX_LEVEL       equ 20

K_LEFT          equ 0
K_RIGHT         equ 1
K_ROT           equ 2
K_SOFT          equ 3
K_HARD          equ 4
K_PAUSE         equ 5
K_ROTCCW        equ 6

; ----------------------------------------------------------------------------

start:
        ld (save_sp),sp
        xor a
        out (254),a
        ld a,7
        ld (BORDCR),a                   ; black border, white ink in the lower screen
    IFDEF TEST
        jp new_game
    ENDIF

title:
        call cls
        call draw_big_logo
        ld hl,title_strs
        call print_table
        ld bc,0x1612
        call cell_addr
        ld hl,hiscore
        ld b,3
        call print_bcd_n
        call wait_no_key
.wait:  call wait_frame
        ld hl,title_count
        inc (hl)
        call any_key
        jr z,.wait
        ; seed the random generator from how long the player took
        ld hl,(FRAMES)
        ld a,(title_count)
        xor h
        ld h,a
        ld a,r
        xor l
        ld l,a
        or h
        jr nz,.seeded
        ld hl,0xACE1
.seeded:
        ld (seed),hl

new_game:
        ld hl,vars_start
        ld de,vars_start+1
        ld bc,vars_end-vars_start-1
        ld (hl),0
        ldir
        ld a,1
        ld (level_bin),a
        ld (level_bcd),a
        ld a,10
        ld (to_level),a
        ld a,(grav_table)
        ld (grav_interval),a
        ld a,7
        ld (bag_idx),a
        ld a,DAS_DELAY
        ld (das),a
        ld a,0xFF
        ld (keys_prev),a                ; keys held from the title don't count
        call init_field
        call draw_game_screen
        call print_stats
        call take_piece
        ld (next_piece),a
        call spawn
        ld hl,(FRAMES)
        ld (t_start),hl
        ld hl,0
        ld (halts),hl

; ---------------------------------------------------------------- main loop --

main_loop:
        call wait_frame
        call render
        call read_keys
        ld c,a
        ld a,(keys_prev)
        cpl
        and c
        ld b,a                          ; B = keys newly pressed, C = keys held
        ld a,c
        ld (keys_prev),a
        ld (kheld),a
        ld a,b
        ld (knew),a
        bit K_SOFT,c
        jr nz,.soft_held
        xor a
        ld (soft_block),a               ; soft drop re-armed once the key is released
.soft_held:
        bit K_PAUSE,b
        call nz,pause
        ld a,(knew)
        bit K_ROT,a
        call nz,rot_cw
        ld a,(knew)
        bit K_ROTCCW,a
        call nz,rot_ccw
        call do_horiz
        ld a,(knew)
        bit K_HARD,a
        jp nz,hard_drop

        ; gravity: can the piece move down one row?
        ld a,(cur_x)
        ld b,a
        ld a,(cur_y)
        inc a
        ld c,a
        ld a,(cur_rot)
        call check_fit
        jr nz,.ground
        xor a
        ld (lock_timer),a
        call soft_active
        ld a,(grav_interval)
        ld d,a
        ld e,0
        jr z,.go
        ld e,1                          ; soft dropping: scores a point per row
        cp SOFT_INT+1
        jr c,.go
        ld d,SOFT_INT
.go:    ld hl,grav_count
        inc (hl)
        ld a,(hl)
        cp d
        jp c,main_loop
        ld (hl),0
        ld hl,cur_y
        inc (hl)
        ld a,1
        ld (dirty),a
        dec e
        jp nz,main_loop
        ld a,1
        call add_score_small
        jp main_loop

.ground:
        ld hl,lock_timer
        inc (hl)
        call soft_active
        jr z,.no_soft
        ld a,(lock_timer)
        add a,4                         ; holding soft drop locks sooner
        ld (lock_timer),a
.no_soft:
        ld a,(lock_timer)
        cp LOCK_DELAY
        jp c,main_loop
        jp lock_piece

soft_active:                            ; NZ if soft drop is in effect
        ld a,(soft_block)
        or a
        jr nz,.no
        ld a,(kheld)
        and 1<<K_SOFT
        ret
.no:    xor a
        ret

; ------------------------------------------------------------ player moves --

do_horiz:
        ld a,(kheld)
        and 3
        ret z
        cp 3
        ret z                           ; left and right together cancel out
        ld d,a
        ld a,(knew)
        and 3
        jr nz,.first
        ld hl,das
        dec (hl)
        ret nz
        ld (hl),DAS_REPEAT
        jr .move
.first: ld a,DAS_DELAY
        ld (das),a
.move:  ld e,1
        dec d
        jr nz,try_shift
        ld e,-1

try_shift:                              ; E = -1 or +1
        ld a,(cur_x)
        add a,e
        ld b,a
        ld a,(cur_y)
        ld c,a
        ld a,(cur_rot)
        call check_fit
        ret nz
        ld a,b
        ld (cur_x),a
        ld de,0x1808
        call tone

moved:                                  ; the piece changed column or rotation
        call calc_ghost
        ld a,1
        ld (dirty),a
        ld a,(lock_timer)
        or a
        ret z
        ld hl,lock_resets
        ld a,(hl)
        cp MAX_RESETS
        ret nc
        inc (hl)
        xor a
        ld (lock_timer),a
        ret

rot_cw: ld e,1
        jr rotate
rot_ccw:
        ld e,3
rotate: ld a,(cur_rot)
        add a,e
        and 3
        ld d,a                          ; D = wanted rotation
        ld e,3                          ; kicks to try: 0, -1, +1 ...
        ld a,(cur_piece)
        or a
        jr nz,.kicks
        ld e,5                          ; ... and -2, +2 for the I piece
.kicks: ld hl,kick_table
.try:   ld a,(cur_x)
        add a,(hl)
        ld b,a
        ld a,(cur_y)
        ld c,a
        push hl
        push de
        ld a,d
        call check_fit
        pop de
        pop hl
        jr z,.ok
        inc hl
        dec e
        jr nz,.try
        ret
.ok:    ld a,b
        ld (cur_x),a
        ld a,d
        ld (cur_rot),a
        ld de,0x280C
        call tone
        jr moved

hard_drop:
        ld a,(ghost_y)
        ld hl,cur_y
        sub (hl)
        ld b,a                          ; rows fallen
        ld a,(ghost_y)
        ld (hl),a
        ld a,1
        ld (dirty),a
        ld a,b
        or a
        jr z,lock_piece
        add a,a                         ; two points per row
        call add_score_small

; ------------------------------------------------------------------ locking --

lock_piece:
        call wait_frame
        call render                     ; show the piece where it lands
        ld a,(cur_piece)
        ld e,a
        ld d,0
        ld hl,piece_attrs
        add hl,de
        ld a,(hl)
        ld (plot_attr),a
        ld a,(cur_rot)
        call piece_ptr
        push hl
        ld a,(cur_x)
        ld b,a
        ld a,(cur_y)
        ld c,a
        call pos_ptr
        pop de
        DUP 4
        ld a,(de)
        inc de
        inc de
        add a,l
        ld c,a
        adc a,h
        sub c
        ld b,a
        ld a,(plot_attr)
        ld (bc),a
        EDUP
        ld de,0xC808
        call tone
        call clear_lines
        call spawn
        jp z,main_loop

; ---------------------------------------------------------------- game over --

game_over:
        call wait_frame
        call render
        ld b,20
        ld hl,WELL_ATTR+19*32
.row:   call wait_frame
        push bc
        push hl
        ld b,10
.cell:  ld (hl),DEAD_ATTR
        inc hl
        djnz .cell
        pop hl
        pop bc
        push bc
        push hl
        ld a,b
        add a,a
        add a,a
        neg
        add a,140                       ; pitch falls as the well fills
        ld d,a
        ld e,10
        call tone
        pop hl
        pop bc
        ld de,-32
        add hl,de
        djnz .row
        ; new high score?
        ld hl,score
        ld de,hiscore
        ld b,3
.cmp:   ld a,(de)
        cp (hl)
        jr c,.newhi
        jr nz,.nohi
        inc hl
        inc de
        djnz .cmp
        jr .nohi
.newhi: ld hl,score
        ld de,hiscore
        ld bc,3
        ldir
        call print_hiscore
.nohi:  ld hl,over_strs
        call print_table
    IFDEF TEST
        jp exit_basic
    ELSE
        ld b,40
.pause: call wait_frame
        djnz .pause
        call wait_no_key
.key:   call wait_frame
        call any_key
        jr z,.key
        jp title
    ENDIF

exit_basic:
        ld hl,(FRAMES)
        ld (t_end),hl
        ld sp,(save_sp)
        ld iy,0x5C3A
        ret

; -------------------------------------------------------------------- pause --

pause:
        ld hl,pause_str
        call print_str
.down:  call wait_frame
        call read_keys
        bit K_PAUSE,a
        jr nz,.down
.up:    call wait_frame
        call read_keys
        bit K_PAUSE,a
        jr z,.up
        ld (keys_prev),a
        ld a,(kheld)
        or 1<<K_SOFT
        ld (soft_block),a
        xor a
        ld (knew),a
        ld hl,unpause_str
        jp print_str

; ------------------------------------------------------------------- pieces --

spawn:                                  ; Z if the new piece fits, NZ = game over
        ld a,(next_piece)
        ld (cur_piece),a
        call take_piece
        ld (next_piece),a
        call draw_next
        xor a
        ld (cur_rot),a
        ld (cur_y),a
        ld (grav_count),a
        ld (lock_timer),a
        ld (lock_resets),a
        ld (drawn_valid),a
        ld a,3
        ld (cur_x),a
        ld a,(kheld)
        and 1<<K_SOFT
        ld (soft_block),a               ; a held soft drop doesn't carry over
        call calc_ghost
        ld a,1
        ld (dirty),a
        ld a,(cur_x)
        ld b,a
        ld c,0
        xor a
        jp check_fit

take_piece:                             ; A = next piece from a shuffled bag of 7
    IFDEF TEST
        ld hl,(test_seq_ptr)
        ld a,(hl)
        inc hl
        ld (test_seq_ptr),hl
        ret
    ELSE
        ld a,(bag_idx)
        cp 7
        jr c,.have
        ld b,6
.shuffle:
        call rnd
        and 7
        cp b
        jr z,.pick
        jr nc,.shuffle
.pick:  ld e,a
        ld d,0
        ld hl,bag
        add hl,de
        push hl
        ld e,b
        ld hl,bag
        add hl,de
        pop de
        ld a,(de)
        ld c,(hl)
        ld (hl),a
        ld a,c
        ld (de),a
        djnz .shuffle
        xor a
.have:  ld e,a
        inc a
        ld (bag_idx),a
        ld d,0
        ld hl,bag
        add hl,de
        ld a,(hl)
        ret
    ENDIF

rnd:                                    ; 16-bit xorshift; returns A, keeps BC and DE
        ld hl,(seed)
        ld a,h
        rra
        ld a,l
        rra
        xor h
        ld h,a
        ld a,l
        rra
        ld a,h
        rra
        xor l
        ld l,a
        xor h
        ld h,a
        ld (seed),hl
        ret

piece_ptr:                              ; A = rotation -> HL = cell table of the current piece
        ld l,a
        ld a,(cur_piece)
        add a,a
        add a,a
        add a,l
cells_ptr:                              ; A = piece*4 + rotation -> HL
        add a,a
        add a,a
        add a,a
        ld l,a
        ld h,0
        ld de,pieces
        add hl,de
        ret

pos_ptr:                                ; B = x (signed), C = y -> HL = field address
        ld l,c
        ld h,0
        add hl,hl
        add hl,hl
        add hl,hl
        add hl,hl
        ld de,field
        jr add_x
attr_ptr:                               ; B = x (signed), C = y -> HL = attribute address
        ld l,c
        ld h,0
        add hl,hl
        add hl,hl
        add hl,hl
        add hl,hl
        add hl,hl
        ld de,WELL_ATTR
add_x:  add hl,de
        ld a,b
        or a
        jp p,.pos
        dec h
.pos:   add a,l
        ld l,a
        ret nc
        inc h
        ret

check_fit:                              ; B = x, C = y, A = rotation -> Z if the piece fits
        push bc                         ; keeps BC
        call piece_ptr
        push hl
        call pos_ptr
        pop de
        DUP 4
        ld a,(de)
        inc de
        inc de
        add a,l
        ld c,a
        adc a,h
        sub c
        ld b,a
        ld a,(bc)
        or a
        jr nz,.fail
        EDUP
        pop bc
        xor a
        ret
.fail:  pop bc
        or 1
        ret

calc_ghost:                             ; where would the piece land?
        ld a,(cur_x)
        ld b,a
        ld a,(cur_y)
        ld c,a
.down:  inc c
        ld a,(cur_rot)
        call check_fit
        jr z,.down
        dec c
        ld a,c
        ld (ghost_y),a
        ret

; ---------------------------------------------------------------- rendering --

render:                                 ; redraw the falling piece if it changed
        ld a,(score_dirty)
        or a
        call nz,print_score
        ld a,(dirty)
        or a
        ret z
        xor a
        ld (dirty),a
        ld a,(drawn_valid)
        or a
        jr z,.draw
        xor a                           ; EMPTY
        ld (plot_attr),a
        ld a,(drawn_x)
        ld b,a
        ld a,(drawn_gy)
        ld c,a
        ld a,(drawn_rot)
        call plot_piece
        ld a,(drawn_x)
        ld b,a
        ld a,(drawn_y)
        ld c,a
        ld a,(drawn_rot)
        call plot_piece
.draw:  ld hl,cur_rot
        ld de,drawn_rot
        ld bc,4
        ldir                            ; rot, x, y, ghost_y
        ld a,1
        ld (drawn_valid),a
        ld a,(cur_piece)
        ld e,a
        ld d,0
        ld hl,piece_attrs
        add hl,de
        ld a,(hl)
        push af
        rrca
        rrca
        rrca
        and 7
        or 0x40                         ; ghost: the piece colour as ink on black
        ld (plot_attr),a
        ld a,(cur_x)
        ld b,a
        ld a,(ghost_y)
        ld c,a
        ld a,(cur_rot)
        call plot_piece
        pop af
        ld (plot_attr),a
        ld a,(cur_x)
        ld b,a
        ld a,(cur_y)
        ld c,a
        ld a,(cur_rot)

plot_piece:                             ; B = x, C = y, A = rotation, colour in plot_attr
        call piece_ptr
        push hl
        call attr_ptr
        pop de
        inc de                          ; second byte of each pair = attribute offset
plot_cells:                             ; HL = attribute base, DE = cell table + 1
        DUP 4
        ld a,(de)
        inc de
        inc de
        add a,l
        ld c,a
        adc a,h
        sub c
        ld b,a
        ld a,(plot_attr)
        ld (bc),a
        EDUP
        ret

draw_next:
        xor a
        ld bc,NEXT_ROW*256+NEXT_COL
        ld de,0x0204
        call fill_attr
        ld a,(next_piece)
        ld e,a
        ld d,0
        ld hl,piece_attrs
        add hl,de
        ld a,(hl)
        ld (plot_attr),a
        ld a,e
        add a,a
        add a,a
        call cells_ptr
        ex de,hl
        inc de
        ld hl,ATTRS+NEXT_ROW*32+NEXT_COL
        jr plot_cells

draw_field:                             ; copy the whole field to the attributes
        ld hl,field
        ld de,WELL_ATTR
        ld a,20
.row:   DUP 10
        ldi
        EDUP
        ld bc,6
        add hl,bc
        ex de,hl
        ld bc,22
        add hl,bc
        ex de,hl
        dec a
        jr nz,.row
        ret

; -------------------------------------------------------------- line clears --

clear_lines:
        ld hl,field
        ld de,fullrows
        ld bc,20*256
.scan:  push bc
        push hl
        ld b,10
.cell:  ld a,(hl)
        or a
        jr z,.notfull
        inc hl
        djnz .cell
        ld a,1
.notfull:
        ld (de),a                       ; A is 0 here unless all ten were set
        inc de
        pop hl
        ld bc,16
        add hl,bc
        pop bc
        add a,c
        ld c,a
        djnz .scan
        ld a,c
        ld (nfull),a
        or a
        ret z

        ld b,6                          ; flash the full rows
.anim:  push bc
        call wait_frame
        pop bc
        push bc
        ld a,b
        rrca
        sbc a,a
        cpl
        and FLASH_ATTR                  ; white on even steps, black on odd
        call paint_full
        pop bc
        push bc
        ld a,b
        add a,a
        add a,a
        add a,a
        add a,20
        ld d,a                          ; rising pitch
        ld a,(nfull)
        add a,a
        add a,a
        add a,a
        add a,24
        ld e,a                          ; longer for more lines
        call tone
        call wait_frame
        call wait_frame
        pop bc
        djnz .anim

        ; drop the rows above into the gaps
        ld hl,field+19*16+15
        ld d,h
        ld e,l
        ld ix,fullrows+19
        ld b,20
.pack:  ld a,(ix+0)
        dec ix
        or a
        jr z,.copy
        push bc
        ld bc,-16
        add hl,bc
        pop bc
        jr .next
.copy:  push bc
        ld bc,16
        lddr
        pop bc
.next:  djnz .pack
        ld a,(nfull)
        ld b,a
.fill:  push bc
        ld b,6
        ld a,WALL
.wall:  ld (de),a
        dec de
        djnz .wall
        ld b,10
        xor a
.empty: ld (de),a
        dec de
        djnz .empty
        pop bc
        djnz .fill
        call wait_frame
        call draw_field

        ; lines, score, level
        ld a,(nfull)
        ld hl,lines+1
        add a,(hl)
        daa
        ld (hl),a
        dec hl
        ld a,(hl)
        adc a,0
        daa
        ld (hl),a
        ld a,(nfull)
        dec a
        ld e,a
        add a,a
        add a,e
        ld e,a
        ld d,0
        ld hl,line_scores
        add hl,de
        ld a,(level_bin)
        ld b,a
.score: push bc
        push hl
        call add_bcd3
        pop hl
        pop bc
        djnz .score
        ld hl,nfull
        ld a,(to_level)
        sub (hl)
        jr z,.level
        jr nc,.same
.level: add a,10
        ld (to_level),a
        call level_up
        jp print_stats
.same:  ld (to_level),a
        jp print_stats

paint_full:                             ; A = attribute for every full row
        ld c,a
        ld hl,WELL_ATTR
        ld de,fullrows
        ld b,20
.row:   ld a,(de)
        inc de
        or a
        jr z,.skip
        push hl
        push bc
        ld b,10
.cell:  ld (hl),c
        inc hl
        djnz .cell
        pop bc
        pop hl
.skip:  push de
        ld de,32
        add hl,de
        pop de
        djnz .row
        ret

level_up:
        ld a,(level_bin)
        cp 99
        ret nc
        inc a
        ld (level_bin),a
        ld a,(level_bcd)
        add a,1
        daa
        ld (level_bcd),a
        ld a,(level_bin)
        cp MAX_LEVEL
        jr c,.ok
        ld a,MAX_LEVEL
.ok:    dec a
        ld e,a
        ld d,0
        ld hl,grav_table
        add hl,de
        ld a,(hl)
        ld (grav_interval),a
        ld de,0x5020
        call tone
        ld de,0x3C20
        call tone
        ld de,0x2830
        jp tone

; ------------------------------------------------------------------ scoring --

add_score_small:                        ; A = points to add (binary, 1-99)
        ld b,a
        xor a
.conv:  add a,1
        daa
        djnz .conv
        ld (small_bcd+2),a
        ld hl,small_bcd

add_bcd3:                               ; HL -> 3 BCD bytes (high first) to add to the score
        inc hl
        inc hl
        ld de,score+2
        ld b,3
        or a
.add:   ld a,(de)
        adc a,(hl)
        daa
        ld (de),a
        dec hl
        dec de
        djnz .add
        ld a,1
        ld (score_dirty),a
        ret

print_stats:
        ld a,VALUE_ATTR
        ld (pattr),a
        ld bc,0x0F03
        call cell_addr
        ld hl,lines
        ld b,2
        call print_bcd_n
        ld bc,0x0B1A
        call cell_addr
        ld hl,level_bcd
        ld b,1
        call print_bcd_n
        call print_hiscore

print_score:
        xor a
        ld (score_dirty),a
        ld a,VALUE_ATTR
        ld (pattr),a
        ld bc,0x0502
        call cell_addr
        ld hl,score
        ld b,3
        jr print_bcd_n

print_hiscore:
        ld a,VALUE_ATTR
        ld (pattr),a
        ld bc,0x0A02
        call cell_addr
        ld hl,hiscore
        ld b,3

print_bcd_n:                            ; HL -> B BCD bytes, DE = screen address
.byte:  push bc
        ld a,(hl)
        push hl
        push af
        rrca
        rrca
        rrca
        rrca
        call .digit
        pop af
        call .digit
        pop hl
        inc hl
        pop bc
        djnz .byte
        ret
.digit: and 15
        add a,'0'
        jp print_char

; ------------------------------------------------------------------ screens --

cls:
        ld hl,0x4000
        ld de,0x4001
        ld bc,0x1AFF
        ld (hl),l
        ldir
        ret

init_field:
        ld hl,field_guard
        ld de,field_guard+1
        ld bc,22*16-1
        ld (hl),WALL
        ldir
        ld hl,field
        ld c,20
.row:   ld b,10
.cell:  ld (hl),EMPTY
        inc hl
        djnz .cell
        ld de,6
        add hl,de
        dec c
        jr nz,.row
        ret

draw_game_screen:
        call cls
        ; block bitmap in every cell of the well and the preview box
        ld hl,block_tile
        ld bc,WELL_Y*256+WELL_X
        ld de,0x140A
        call fill_rect
        ld hl,block_tile
        ld bc,NEXT_ROW*256+NEXT_COL
        ld de,0x0204
        call fill_rect
        ; brick frame: two sides and the floor
        ld hl,brick_tile
        ld bc,WELL_Y*256+WELL_X-1
        ld de,0x1501
        call fill_rect
        ld hl,brick_tile
        ld bc,WELL_Y*256+WELL_X+10
        ld de,0x1501
        call fill_rect
        ld hl,brick_tile
        ld bc,(WELL_Y+20)*256+WELL_X
        ld de,0x010A
        call fill_rect
        ld a,FRAME_ATTR
        ld bc,WELL_Y*256+WELL_X-1
        ld de,0x1501
        call fill_attr
        ld a,FRAME_ATTR
        ld bc,WELL_Y*256+WELL_X+10
        ld de,0x1501
        call fill_attr
        ld a,FRAME_ATTR
        ld bc,(WELL_Y+20)*256+WELL_X
        ld de,0x010A
        call fill_attr
        ; double-height logo
        ld hl,logo_small
.logo:  ld a,(hl)
        or a
        jr z,.text
        inc hl
        ld c,(hl)
        inc hl
        ld e,a
        ld a,(hl)
        inc hl
        ld (pattr),a
        ld a,e
        ld b,0
        push hl
        call print_big
        pop hl
        jr .logo
.text:  ld hl,game_strs
        jp print_table

draw_big_logo:                          ; TETRIS in coloured blocks
        ld hl,block_tile
        ld bc,0x0304
        ld de,0x0517
        call fill_rect
        ld ix,big_logo
        ld c,4
        ld a,6
.letter:
        push af
        ld a,(ix+0)
        inc ix
        ld (.attr+1),a
        ld b,3
.row:   ld d,(ix+0)
        inc ix
        push bc
        ld e,3
.bit:   ld a,d
        add a,a
        ld d,a
        and 8
        jr z,.skip
        call attr_addr
.attr:  ld (hl),0
.skip:  inc c
        dec e
        jr nz,.bit
        pop bc
        inc b
        ld a,b
        cp 8
        jr nz,.row
        ld a,c
        add a,4
        ld c,a
        pop af
        dec a
        jr nz,.letter
        ret

; -------------------------------------------------------- drawing primitives --

cell_addr:                              ; B = row, C = column -> DE = screen address
        ld a,b
        and 0x18
        or 0x40
        ld d,a
        ld a,b
        and 7
        rrca
        rrca
        rrca
        or c
        ld e,a
        ret

attr_addr:                              ; B = row, C = column -> HL = attribute address
        ld a,b
        rrca
        rrca
        rrca
        ld l,a
        and 3
        or 0x58
        ld h,a
        ld a,l
        and 0xE0
        or c
        ld l,a
        ret

put_tile:                               ; HL -> 8 bytes, B = row, C = column
        push bc
        push de
        call cell_addr
        ld b,8
.line:  ld a,(hl)
        ld (de),a
        inc hl
        inc d
        djnz .line
        pop de
        pop bc
        ret

fill_rect:                              ; HL = tile, B = row, C = column, D = height, E = width
.row:   push bc
        push de
.col:   push hl
        call put_tile
        pop hl
        inc c
        dec e
        jr nz,.col
        pop de
        pop bc
        inc b
        dec d
        jr nz,.row
        ret

fill_attr:                              ; A = attribute, B = row, C = column, D = height, E = width
        ld (.value+1),a
.row:   call attr_addr
        push de
.value: ld a,0
.col:   ld (hl),a
        inc hl
        dec e
        jr nz,.col
        pop de
        inc b
        dec d
        jr nz,.row
        ret

print_table:                            ; HL -> strings for print_str, ended by 0xFF
        ld a,(hl)
        inc a
        ret z
        call print_str
        jr print_table

print_str:                              ; HL -> row, column, attribute, text, 0
        ld b,(hl)
        inc hl
        ld c,(hl)
        inc hl
        ld a,(hl)
        inc hl
        ld (pattr),a
        call cell_addr
.char:  ld a,(hl)
        inc hl
        or a
        ret z
        push hl
        call print_char
        pop hl
        jr .char

print_char:                             ; A = character, DE = screen address (moves right)
        push de
        ld l,a
        ld h,0
        add hl,hl
        add hl,hl
        add hl,hl
        ld bc,FONT
        add hl,bc
        ld b,8
.line:  ld a,(hl)
        ld c,a
        srl a
        or c                            ; embolden
        ld (de),a
        inc hl
        inc d
        djnz .line
        pop de
        ld a,d
        rrca
        rrca
        rrca
        and 3
        or 0x58
        ld h,a
        ld l,e
        ld a,(pattr)
        ld (hl),a
        inc e
        ret

print_big:                              ; A = character, B = row, C = column: double height
        push af
        call cell_addr
        pop af
        ld l,a
        ld h,0
        add hl,hl
        add hl,hl
        add hl,hl
        ld bc,FONT
        add hl,bc
        call .half
        ld a,d
        sub 8
        ld d,a
        ld a,e
        add a,32
        ld e,a
.half:  ld b,4
.line:  ld a,(hl)
        ld c,a
        srl a
        or c
        ld (de),a
        inc d
        ld (de),a
        inc d
        inc hl
        djnz .line
        push hl
        ld a,d
        dec a
        rrca
        rrca
        rrca
        and 3
        or 0x58
        ld h,a
        ld l,e
        ld a,(pattr)
        ld (hl),a
        pop hl
        ret

; -------------------------------------------------------------- input, time --

wait_frame:
        halt
        push hl
        ld hl,(halts)
        inc hl
        ld (halts),hl
        pop hl
        ret

any_key:                                ; NZ if any key is down
        xor a
        in a,(254)
        cpl
        and 0x1F
        ret

wait_no_key:
        call wait_frame
        call any_key
        jr nz,wait_no_key
        ret

read_keys:                              ; A = bit mask of the K_ controls that are down
    IFDEF TEST
        ld hl,(script_ptr)
        ld a,(hl)
        or a
        jp z,exit_basic                 ; end of script
        ld a,(script_count)
        inc a
        cp (hl)
        jr c,.same
        xor a
        ld (script_count),a
        inc hl
        ld a,(hl)
        inc hl
        ld (script_ptr),hl
        ret
.same:  ld (script_count),a
        inc hl
        ld a,(hl)
        ret
    ELSE
        ld e,0
        ld a,0xDF                       ; P O I U Y
        in a,(254)
        ld d,a
        bit 1,d
        jr nz,$+4
        set K_LEFT,e
        bit 0,d
        jr nz,$+4
        set K_RIGHT,e
        ld a,0xF7                       ; 1 2 3 4 5
        in a,(254)
        bit 4,a
        jr nz,$+4
        set K_LEFT,e
        ld a,0xEF                       ; 0 9 8 7 6
        in a,(254)
        ld d,a
        bit 2,d
        jr nz,$+4
        set K_RIGHT,e
        bit 3,d
        jr nz,$+4
        set K_ROT,e
        bit 4,d
        jr nz,$+4
        set K_SOFT,e
        bit 0,d
        jr nz,$+4
        set K_HARD,e
        ld a,0xFB                       ; Q W E R T
        in a,(254)
        ld d,a
        bit 0,d
        jr nz,$+4
        set K_ROT,e
        bit 1,d
        jr nz,$+4
        set K_ROTCCW,e
        ld a,0xFD                       ; A S D F G
        in a,(254)
        bit 0,a
        jr nz,$+4
        set K_SOFT,e
        ld a,0x7F                       ; SPACE SYM M N B
        in a,(254)
        bit 0,a
        jr nz,$+4
        set K_HARD,e
        ld a,0xBF                       ; ENTER L K J H
        in a,(254)
        bit 4,a
        jr nz,$+4
        set K_PAUSE,e
        ld a,e
        ret
    ENDIF

tone:                                   ; D = pitch (higher = lower note), E = half-waves
        xor a
.wave:  xor 0x10
        out (254),a
        ld b,d
.delay: djnz .delay
        dec e
        jr nz,.wave
        ret

; --------------------------------------------------------------------- data --

        INCLUDE "pieces.inc"            ; pieces: 7 x 4 rotations x 4 cells (field offset, attr offset)

piece_attrs:                            ; I O T S Z J L: bright paper colour, black ink
        db 0x68, 0x70, 0x58, 0x60, 0x50, 0x48, 0x78
kick_table:
        db 0, -1, 1, -2, 2
grav_table:                             ; frames per row for levels 1-20
        db 40, 35, 30, 26, 22, 18, 15, 12, 10, 8, 7, 6, 5, 4, 4, 3, 3, 2, 2, 1
line_scores:                            ; BCD, times the level
        db 0x00,0x00,0x40, 0x00,0x01,0x00, 0x00,0x03,0x00, 0x00,0x12,0x00

block_tile:
        db %11111111
        db %10000001
        db %10000001
        db %10000001
        db %10000001
        db %10000001
        db %10000001
        db %11111111
brick_tile:
        db %11111011
        db %11111011
        db %11111011
        db %00000000
        db %11011111
        db %11011111
        db %11011111
        db %00000000

logo_small:                             ; character, column, attribute
        db 'T', 11, 0x42
        db 'E', 13, 0x46
        db 'T', 15, 0x44
        db 'R', 17, 0x45
        db 'I', 19, 0x43
        db 'S', 21, 0x47
        db 0

big_logo:                               ; attribute, then five rows of three cells
        db 0x50, %111, %010, %010, %010, %010      ; T
        db 0x70, %111, %100, %110, %100, %111      ; E
        db 0x60, %111, %010, %010, %010, %010      ; T
        db 0x68, %110, %101, %110, %101, %101      ; R
        db 0x58, %111, %010, %010, %010, %111      ; I
        db 0x78, %011, %100, %010, %001, %110      ; S

title_strs:
        db 10, 5, 0xC7, "PRESS ANY KEY TO PLAY", 0
        db 13, 7, 0x46, "O  P", 0
        db 13, 16, 0x07, "MOVE", 0
        db 14, 7, 0x46, "Q  W", 0
        db 14, 16, 0x07, "ROTATE", 0
        db 15, 7, 0x46, "A", 0
        db 15, 16, 0x07, "SOFT DROP", 0
        db 16, 7, 0x46, "SPACE", 0
        db 16, 16, 0x07, "HARD DROP", 0
        db 17, 7, 0x46, "H", 0
        db 17, 16, 0x07, "PAUSE", 0
        db 19, 4, 0x05, "OR CURSOR KEYS 5 6 7 8, 0", 0
        db 22, 7, 0x45, "HIGH SCORE", 0
        db 0xFF

game_strs:
        db 3, 1, LABEL_ATTR, " SCORE  ", 0
        db 8, 1, LABEL_ATTR, "  HIGH  ", 0
        db 13, 1, LABEL_ATTR, " LINES  ", 0
        db 3, 23, LABEL_ATTR, "  NEXT  ", 0
        db 9, 23, LABEL_ATTR, " LEVEL  ", 0
        db 15, 23, 0x05, "O P MOVE", 0
        db 16, 23, 0x05, "Q W TURN", 0
        db 17, 23, 0x05, "A   SOFT", 0
        db 18, 23, 0x05, "SPC DROP", 0
        db 19, 23, 0x05, "H  PAUSE", 0
        db 0xFF

over_strs:
        db 10, 12, 0xD7, "  GAME  ", 0
        db 11, 12, 0xD7, "  OVER  ", 0
        db 0xFF
pause_str:
        db 19, 1, 0xD7, " PAUSED ", 0
unpause_str:
        db 19, 1, 0x00, "        ", 0

    IFDEF TEST
        INCLUDE "test.inc"              ; test_seq (pieces) and test_script (frames, keys)
test_seq_ptr:   dw test_seq
script_ptr:     dw test_script
script_count:   db 0
    ENDIF

; ---------------------------------------------------------------- variables --

save_sp:        dw 0
seed:           dw 0xACE1
title_count:    db 0
hiscore:        db 0, 0, 0
bag:            db 0, 1, 2, 3, 4, 5, 6
small_bcd:      db 0, 0, 0
pattr:          db 0
plot_attr:      db 0
halts:          dw 0                    ; frames the game has waited for
t_start:        dw 0                    ; FRAMES when play began / ended (for timing tests)
t_end:          dw 0

vars_start:
score:          db 0, 0, 0              ; BCD, high byte first
lines:          db 0, 0
level_bcd:      db 0
level_bin:      db 0
to_level:       db 0
grav_interval:  db 0
grav_count:     db 0
lock_timer:     db 0
lock_resets:    db 0
das:            db 0
keys_prev:      db 0
kheld:          db 0
knew:           db 0
soft_block:     db 0
dirty:          db 0
score_dirty:    db 0
cur_piece:      db 0
next_piece:     db 0
bag_idx:        db 0
cur_rot:        db 0                    ; rot, x, y, ghost_y: copied as a block
cur_x:          db 0
cur_y:          db 0
ghost_y:        db 0
drawn_rot:      db 0
drawn_x:        db 0
drawn_y:        db 0
drawn_gy:       db 0
drawn_valid:    db 0
nfull:          db 0
fullrows:       ds 20
vars_end:

field_guard:    ds 16                   ; a wall row above the field
field:          ds 21*16                ; 20 rows of 10 cells + 6 wall cells, then a floor row
code_end:
