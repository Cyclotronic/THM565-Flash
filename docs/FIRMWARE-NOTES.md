# THM565 Firmware Notes

## Part 1: Serial Command Surface and Flash/Cal Write Path


Second static pass over `V2_0.ROM` (via `V2_0.bin`), continuing
[`FIRMWARE-DISASSEMBLY.md`](FIRMWARE-DISASSEMBLY.md). Nothing here executed
QLOADER, opened a port or touched the instrument. Where a claim is checked
against the live capture it cites `capture.log` timestamps (seconds of day,
as printed by `capture_bridge.py`). "Confirmed" means read directly from code
or bytes; "inferred" means the code only fits one reading but that reading
was not proven.

Several findings of the first pass are wrong and are corrected here; the
list is at the top of `FIRMWARE-DISASSEMBLY.md`.

Addresses: `0x1234` is a root (common-area-0) address; `bb:1234` is logical
`0x1234` in bank BBR `bb` (physical = logical + `bb`·0x1000 for logical
0x3000-0x7fff); bytecode addresses are written `bc:1234` (physical
0x1a800 + 0x1234).

## Tooling added

All offline, all reproducible:

| Script | Output | What |
| --- | --- | --- |
| `z180.py` | - | Z180 layer over vendored `z80dis`: decodes IN0/OUT0/TST/MLT/SLP/OTIM, which plain z80dis gets wrong |
| `z180_trace.py` | `V2_0-z180-listing.txt`, `V2_0-functions.json` | Recursive descent across banks: follows the banked-call trampoline, switch tables, inline-argument helpers. 47,629 instructions / 95,557 code bytes reached from reset, vectors and the 237 banked-function descriptors |
| `tekvm.py` | `V2_0-bytecode.txt`, `vm-opcodes.json` | Disassembler for the bytecode VM (below). 3,799 instructions reached from the VM start and the command handlers; 0 overlapping decodes, 0 undecodable opcodes |
| `disasm_handlers.py` (extended) | `handler-disassembly.txt`, `loader-V2_0.txt`, `loader-V1_04.txt` | Linear dumps, now Z180-aware, `--bin`/`--out` options |

## CPU is a Z180, and the memory map

**Confirmed.** Reachable code (`z180_trace.py`) contains 16 `OUT0`, 10 `IN0`
(Z180-only `ED 39 nn` / `ED 38 nn`) and 32 `MLT`; the ASCI and PRT registers
are additionally reached with `OUT (C)`/`IN (C)` and B = 0. The reset code
programs the Z180 MMU:

```
0814  3e83    LD A,0x83 ; 0816 OUT0 (0x3a),A  CBAR = 0x83
0819  3e78    LD A,0x78 ; 081b OUT0 (0x38),A  CBR  = 0x78
081e  af      XOR A     ; 081f OUT0 (0x39),A  BBR  = 0x00
```

So logical 0x0000-0x2fff is fixed flash (physical = logical), logical
0x3000-0x7fff is a 20 KB bank window (physical = logical + BBR·0x1000), and
logical 0x8000-0xffff is RAM at physical 0x80000-0x87fff. The S-record image
is laid out exactly for this: code runs 0x3000-0x7ff6, 0x8000-0xcff9,
0xd000-0x11fee, 0x12000-0x16ff8, 0x17000-0x192ee, i.e. BBR 0x00, 0x05,
0x0a, 0x0f, 0x14. The S0 header text is `?ABS_ENTRY_MOD`, an IAR XLINK
module name - consistent with the IAR-style banked calling convention below.

Earlier notes found "no IN/OUT": `z80dis` returns an empty 2-byte
instruction for IN0/OUT0 (real length 3) and `NEG` for MLT/TST.

Banked calls (**confirmed**): `LD BC,desc / CALL 0x1815`, `LD HL,desc /
CALL 0x1812`, or `LD IY,desc / CALL 0x1818`. The trampoline (0x1812-0x182a)
saves BBR, loads `[addr_lo][addr_hi][BBR][00]` from the descriptor, sets
BBR and jumps; banked functions return through `JP 0x182b` (or `0x187c`,
which unwinds IX first), which restores BBR. The 237 descriptors sit at
0x2ac6-0x2e79. BBR 0x16 and 0x17 descriptors point at data, not code.

Clock (**inferred from two confirmed baud settings**): the serial init
(below) writes CNTLB0 = 0x0b for 1200 baud and 0x08 for 9600, both of which
PROTOCOL.md observed live. On a Z180 with prescale ÷10 and DR ÷64 those
values give 1200 and 9600 only for φ = 6.144 MHz. PRT0 is loaded with 0x0c35
(0f:7fbc) and interrupts through vector 2 (0x0d8f -> 0x0b01), which at φ/20
gives a 98.3 Hz, 10.17 ms tick.

Interrupts (**confirmed**): I = 0, IL = 0x20 (0x0803-0x080a), so the Z180
internal vector table is at 0x0020. Only INT1 (0x0dfb), PRT0 (0x0d8f) and
ASCI0 (0x0db3) have handlers; the rest point at `EI/RETI` (0x0e1f). INT0 is
`RST 38` -> 0x0dd7. `FIRMWARE-DISASSEMBLY.md` called 0x0004-0x07ff "not a
real interrupt-vector table" - the gaps are unpopulated, but 0x0020-0x003a
is the real vector table and 0x0040-0x04d2 is real code (the loader and the
flash primitives, below).

## The bytecode VM (the reason the command-table addresses looked wrong)

**Confirmed.** Almost all user-interface and serial-command logic is a
bytecode program, not Z180 code. The interpreter is bank-0 function 0x38b7:

- Bytecode lives at physical 0x1a800-0x1ffff (descriptor 0x2e32 = L 0x3800,
  BBR 0x17). The VM copies bytes 0-0xfff into RAM 0xab50 at start (0a:7a8e)
  and pages further 0x800-byte pages into 0xb350 on demand (0a:7ab2,
  0x3375); bytecode address X is physical 0x1a800 + X.
- Dispatch is the 243-way switch at 0x3c49 (inline table at 0x3c4c,
  opcodes 0x02-0xf4, default = no-op 0x7e65). Every opcode handler ends
  `JP 0x7e65`, which stores the fetch pointer as the new PC (0xa84e) and
  returns to the loop head 0x38c4.
- Operand fetchers: 0x3000 byte, 0x304d 16-bit, 0x319d 32-bit, 0x3253
  counted string. The all-high-bit value (0x80 / 0x8000 / 0x80000000)
  means "pop from the VM stack" - that is how a command's `#` arguments
  reach its handler.
- VM stack at 0xaa80, pointer (0xa855); variables at 0xca16, bit flags at
  0xca0c, both cleared on every VM restart (0x39b8-0x3a52).

`0x38c4` is the loop head the first pass flagged. Its two branches are now
read:

- **`0x3bf1` = execute one instruction.** Taken while a handler is running
  ((0xa84c) != 0). It fetches the opcode at 0xab50 + PC and dispatches.
- **`0x3a57` = event scheduler.** Taken when idle. It picks the
  highest-priority pending event: 1 start ((0xaa6a), vector (0xaa78)),
  2 key ((0xbb50), table 0xbb52), 3 **serial command matched** ((0xaa6c),
  vector (0xaa6d)), 4-7 timers/other ((0xaa6f), (0xaa74), (0xaa73),
  (0xaa75)). A lower event number preempts a running one: PC, (0xa84a),
  (0xa84c), (0xa850) are pushed on the VM stack (0x3aff-0x3b16) and popped by
  opcode 0xab. For event 3 the matched command's `#` arguments are pushed
  first (0x3b74-0x3bba).

The command table therefore holds **bytecode** addresses. The Z180 bytes
that happen to sit at 0x4ace/0x4ad3/0x4ad7 in bank 0 fall inside the
handler for VM opcode 0x3f (0x4a8e-0x4ae7), and the first pass's dump ran on
through the handlers for 0x40-0x4a, which is why the first pass saw "tiny stubs sharing the
`JP 0x7e65` epilogue". Opcode operand formats in `vm-opcodes.json` are
derived by enumerating every path through each handler; only c9/ca
(counted case lists) and cf/d0 (one shared handler) vary, and are
special-cased.

## Deliverable 1: serial interaction surface

### Receive/transmit primitives

| Routine | Where | Behaviour (confirmed) |
| --- | --- | --- |
| serial init | 0x0e8b | STAT0 = 0x08 (RX interrupt only); CNTLB0 = 0x08 if E != 0 else 0x0b (9600 / 1200); CNTLA0 = 0x64 (8 data, no parity, 1 stop); (0xaa6b) = E; L != 0 also clears both rings |
| ASCI0 ISR | 0x0db3 -> 0x0edf | RX: if RDRF and no OVRN/PE/FE, byte `& 0x7f` into 80-byte ring 0xa1c5 (tail 0xa1c3, head 0xa1c2); **a full ring silently drops the byte**; posts event 0x1c. RX error: read and discard, rewrite CNTLA0 = 0x64 (clears error flags), post event 0. TX: drains 80-byte ring 0xa21a (head 0xa217, tail 0xa218), turns TIE off when empty, posts 0x1d |
| getc | 14:3a69 (desc 0x2d92) | one-byte push-back ((0xa605)/(0xa606), set by 14:3abf); otherwise blocks on event 0x1c until the ring is non-empty - **no timeout** |
| putc | 14:3ac7 / 14:3b5a (desc 0x2d9a) | **LF is sent as CR** (14:3acd-14:3ad5); writes TDR0 directly when idle, else queues, blocking on 0x1d when full |
| puts | 14:3dfd (0x2db6) | NUL-terminated |
| hex out | 14:3ce0 (0x2daa) byte, 14:3d25 (0x2dae) word | uppercase ASCII hex |
| hex in | 14:3b66 (0x2d9e) byte, 14:3bd5 (0x2da2) word | two getc per byte; on a non-hex char pushes it back and returns an error flag |
| number out | 14:3e22 (0x2dba) | integer formatted by 0f helper 0x2ca2, used by VM opcode 0x90 for query replies |

**BAUD 3 is 9600 8N1 on the instrument, not 7E1.** QLOADER switches the PC
to 9600 7E1 (PROTOCOL.md). The frames are the same length, so the
instrument receives the PC's parity bit as data bit 7 and the ISR masks it
off (0x0f63 `AND 0x7f`); the instrument transmits bit 7 = 0, which a 7E1
receiver reads as the parity bit, and QLOADER masks received bytes to 7 bits
anyway. It works, but by masking, not by matching settings.

### Line reader and command matcher

Serial commands are served by a separate task, 05:7fb6 (desc 0x2d8e),
running alongside the VM (a small kernel; `LD L,n / CALL 0x2d0e` waits for
event n, `CALL 0x2cf2` posts one - inferred from usage):

1. Wait until the command table is enabled ((0xa603), set by VM opcode
   0x8e).
2. Read a line (05:7eee): getc until any character of the terminator set
   ((0xa553), set by opcode 0x8b to just CR). Upper-case letters are folded
   to lower case unless (0xa604) is set. More than 128 characters: the line
   is dropped and `WARNING RS232 COMMAND TOO LONG DISCARDED` (string 0x2a06)
   goes to 0x2c92, the same message routine that reports `FLASH PROGRAM
   FAILED` (presumably the display; not traced).
3. **A line starting with `*` is ignored entirely - no match, no reply**
   (05:7fcc).
4. Otherwise match it against the registered patterns (05:7bc3), set event
   3 and **wait for event 0x0f**, which only VM opcode 0xab (end of an event
   handler) posts for event 3 (00:6994). Commands are therefore strictly
   one at a time; anything the host sends meanwhile waits in the 80-byte RX
   ring, and overflows are dropped.

Pattern rules (05:7ad4, 05:7bc3): upper-case pattern letters are mandatory,
lower-case pattern letters optional, input case is irrelevant (it was
folded), spaces must match, `?` is literal. `#` parses a number (05:77ee),
`@` a channel digit (05:77a9); up to 10 values are collected into 0xa5db and
pushed onto the VM stack before the handler runs. No match runs the default
handler registered by opcode 0x8e: bc:4eb5, which replies
`COMMAND NOT DEFINED 0\r` (note the trailing ` 0`, the success code).

### Completeness of the 60-entry table

**Confirmed complete for V2_0.** The table is not a data structure the Z180
code walks; it is 60 consecutive VM instructions, opcode 0x8d `CMD handler,
"pattern"`, executed once at start-up: bc:00a6 `8c` (disable and clear),
bc:00a7 `8b "\r"` (terminator), bc:00ab-bc:0543 the 60 `8d` entries,
bc:0544 `8e 0x4eb5` (enable, default handler), bc:0547 `ab`. The Z180 side
keeps up to 200 pointers (0xa3c2, bound 0xc8 at 05:7e6b). Evidence that
there is no second table or other dispatch path:

- `find_command_table.py` scans the whole image for the `8d` layout with an
  exact length check and finds only these 60.
- In the 72% of the bytecode that `tekvm.py` reaches, opcodes 0x8b, 0x8c
  and 0x8e occur once each and 0x8d exactly 60 times. A linear sweep of
  every unreached gap finds none of them, nor any 0x89, 0x8f, 0x9c-0x9f,
  0xa1 or 0xa2.
- The only Z180 readers of the serial ring are getc and the ISR. Every
  direct getc caller is accounted for: the line reader (05:7efd), the hex
  readers (14:3b90), `$BURN CONFIG`'s identity read (14:382c), and a scope
  channel-calibration routine (05:41a6, reached from 05:58ed) that reads a
  hex word and one more character when its flag argument is set - which
  command sets that flag was not traced. The hex readers are in turn called
  by `$LOAD CAL`, `$BURN CONFIG`, `DOWNLOAD #` (0f:635c) and `LOAD ... #`
  (0f:647d, via 0f:641f), which therefore all take a hex payload after the
  command line. None of these compares input against command strings.
- One unreferenced facility exists: 05:79c6 (desc 0x2d66) would read a
  0x1000-byte bytecode image as hex into the VM window and restart the VM,
  and 05:7a56 (desc 0x2d6a) would dump it (`FATAL: INCORRECTED FORMATTED
  PROGRAM`, string 0x29e1). No code or data anywhere references either
  descriptor, so it is dead in V2_0.

`command-table.json` itself is correct; only its interpretation (`addr` as a
Z80 entry point) was wrong.

### What each command does

From `V2_0-bytecode.txt`. "Reply" is what goes back on the wire.
Replies were checked against `capture.log` where the capture has them.

| Command | Bytecode | What it does | Reply |
| --- | --- | --- | --- |
| `$RELOAD CODE` | bc:4ad7 | op 03 0 (display control), op 1d 0, sleep 300 (≈305 ms: opcode 0xb0 sleeps value × 0.1 PRT ticks), draw box, draw `CLS` at (0x0e,0x05), then op 0x9c = `CALL 0x0231`, which never returns (loader, below) | none from the VM; the loader's bytes follow |
| `$BURN CAL` | bc:4ace | op 9f 1 -> 14:3684: commit both RAM cal records to their next free flash slots (below) | ` 0\r` ok, ` 1\r` fail (plus `FLASH PROGRAM FAILED` on the display) |
| `$BURN CONFIG` | bc:4ad3 | op a1 -> 0a:7cf9 (snapshots a value into 0xa3ba) then 14:380c: read 2 hex digits (type) + 36 raw chars (identity) from the port, then program the one config slot if it is blank | ` 0\r` / ` 1\r` |
| `$LOAD CAL #` | bc:4b5f | op 9e POP -> 14:3982: # = 0 DMM (0x67 bytes into 0xa32d), 1 SCOPE (0xc3 bytes into 0xa26a), read as hex from the port, **RAM only**, no checksum check; on a hex error re-validates from flash (14:35a8) | **`\r` only** |
| `$DUMP CAL #?` | bc:4b5a | op 9d POP -> 14:38f1: 0 DMM RAM record as hex, 1 SCOPE, 2 config type as 2 hex digits + 36 raw identity chars | data, then ` 0\r` |
| `$TEST #?` | bc:4b55 | op e9 POP -> 14:4d31: 0 / 1 = free SCOPE / DMM cal slots left (14:39f0, 10 - first blank index); 2 display test then reset; 3 via 05:76d3 (not traced); 4 RAM test (0x091e) with PASSED/ERROR message; 5 repeat 2+4 until error, then reset; 100-112 call 0f:6b95 (not traced) | number, then `\r` |
| `INIT` | bc:4b86 | op a2 = `CALL 0x0000`: full restart, back to 1200 baud (start-up bytecode bc:065c sets baud var 0x4e = 0) | none; the power-on banner follows |
| `BAUD #` | bc:4b8a | 0 -> 1200, 3 -> 9600; anything else rejected. Replies first, sleeps 200 (≈203 ms), then reprograms the ASCI (bc:367e -> op 0x89 -> 0x0e8b) | ` 0\r`, or ` 1\r` for other values |
| `ID?` | bc:4aa1 | `THM` + `550 `/`560 `/`565 ` for config type var 0x2d = 1/2/3, **else `??? `**, + `SW VERSION 2.00`; also the power-on banner | `...  0\r` |
| `$KEY INPUT # #` | bc:4b80 | op e8 -> 0f:6cf7 with both args (key event injection, not traced further). **No type gating in the firmware** - the type-4 check is QLOADER-side only | ` 0\r` |
| `KEY BOARD LOCK #` | bc:4b7b | op e7: (0x8002) = (arg == 0) | ` 0\r` |
| `UPLOAD #?` / `DOWNLOAD #` | bc:4bb4 / bc:4bb9 | op f1 / f0 -> 0f:6906 / 0f:618b, 7-way switch on arg over RAM tables at 0xce30.. (not traced further) | ` 0\r` |
| `LOAD`/`GET SCReen|SETting|WFM #` | bc:4bbe-bc:4bdc | op f2 / f3 with block type 0/1/2 -> 0f:64db / 0f:6779 (not traced) | ` 0\r` |
| `POWER OFF` | bc:4afa | reply, sleep 200, op e0 -> 14:4c97 | ` 0\r` |
| `$STB?` | bc:4af1 | fixed string | `110 0\r` |
| `LOW BATtery?` | bc:4b6b | VM flag bit 8 | ` 1\r` low, ` 0\r` |
| `POWEROff TimeOut #` | bc:4aa5 | 0 = never (-1), 1 = 300000, via op a8 timer vector | ` 0\r` |
| clock/date, back light, `PRINT?` | bc:4b07-bc:4b52, bc:4b72, bc:4eaf | ops e4/e5/e6 (clock fields), 7e, and a print subroutine | query values then ` 0\r` |
| scope, DMM, `SOPmode`, `SACQ` (27 entries) | bc:4c08-bc:4e84 | measurement/scope setup; several reject bad values with ` 1\r`; the `$... CALIBRATE` ones end with a bare `\r` | mostly ` 0\r` |

Capture cross-checks (all match the code): `$LOAD CAL 0`/`1` answered a bare
CR (83991.519, 84025.731); `$BURN CONFIG` and `$BURN CAL` answered ` 1\r`
with VPP off (83966.003, 84028.966) and ` 0\r` with VPP on (84477.770,
84538.528); `INIT` produced the banner; `BAUD 3` answered ` 0\r`.

## Deliverable 2: the write paths, from the firmware's side

### Flash primitives (V2_0, used by the cal/config burns)

**Confirmed.** Three root routines at 0x0040-0x0230:

- `0x0041`: turn an IAR far pointer (logical L, BBR b) into BBR = b,
  HL = L.
- `0x00bc`: program one byte. If the byte already reads back equal, return
  success. Otherwise build this sequence on the stack and `CALL` it through
  `JP (HL)` at 0x0040, so it executes from RAM while the flash is not in
  read mode:

  ```
  62 6b        LD H,D / LD L,E          ; target
  36 40        LD (HL),0x40             ; program setup
  71           LD (HL),C                ; data
  e3 e3 e3 e3  EX (SP),HL x4            ; program pulse
  36 c0        LD (HL),0xC0             ; program verify
  e3 e3 e3 e3  EX (SP),HL x4
  4e           LD C,(HL)                ; read back
  36 00        LD (HL),0x00             ; back to read mode
  46 00 c9     LD B,(HL) / NOP / RET
  ```

  Retry until the read-back matches or 100 (0x64) pulses have been spent;
  return C = 1 on failure. This is the Intel 28F0x0-family 12 V
  "quick-pulse" algorithm (0x40/0xC0/0x00). Four `EX (SP),HL` are 64 clock
  states, about 10 µs at 6.144 MHz with no wait states. ~~The firmware
  never writes DCNTL~~ - **corrected by emulation
  ([`FIRMWARE-EMULATION.md`](FIRMWARE-EMULATION.md)):** 0a:74f0 writes
  DCNTL = 0x00 early in boot (`LD BC,0x32 / OUT (C),E`, missed here because
  the port is in BC), so no wait states apply and the emulated pulse is
  11.56 µs (71 T-states).
- `0x018f`: program a buffer at a far pointer + offset: DI, save BBR, map,
  call 0x00bc per byte, **stop at the first failure**, restore BBR, EI.

There is **no erase code anywhere in V2_0**: no `LD (HL),0x20`/`0xA0` and
no stack-built equivalent (the only `LD (IX+d),0x36` sequences are
0x00bc's). A byte can only be programmed from 1s to 0s.

**There is no VPP sense.** Nothing reads a VPP line; without 12 V the
0x40/0xC0 commands do nothing, every verify fails, and the routine gives up
after 100 pulses per byte. The failure is reported, not hidden: `FLASH
PROGRAM FAILED` on the display (string 0x29c6 via 0x2c92) and ` 1` on the
wire. QLOADER then discards it (PROTOCOL.md).

### Cal and config storage: append-only slots

**Confirmed** (14:35a8-14:3a69). The `.ROM` image defines the whole
area as 0xFF, i.e. blank after every reflash:

| Record | Flash (descriptor) | Slots | RAM copy | Layout |
| --- | --- | --- | --- | --- |
| config | 0x199ff (0x2e26) | **1** × 0x2a | 0xa394 | tag, type, 36-byte identity, 4 bytes from 0a:7cf9 |
| DMM cal | 0x19a2f (0x2e2e) | 10 × 0x67 | 0xa32d | tag, checksum, ~checksum, 0x64 payload (25 floats) |
| SCOPE cal | 0x19e77 (0x2e2a) | 10 × 0xc3 | 0xa26a | tag, checksum, ~checksum, 0xc0 payload (48 floats) |

The **tag byte is a slot state**, which answers PROTOCOL.md's open question
about `0xC3`: 0xFF = blank, **0xC3 = live record** (written by
`LD A,0xc3` at 14:36d9, 14:3771, 14:3862), 0x00 = superseded. The checksum
is 0x0849's XOR-from-0x5A over the payload, the same algorithm QLOADER
uses.

- **`$BURN CAL`** (14:3684): for SCOPE, then DMM: find the first slot whose
  tag is 0xFF; stamp the RAM tag 0xC3; program the whole RAM record into that
  slot; if that worked and the slot is not slot 0, program the previous
  slot's tag to 0x00. **No blank slot left -> failure.** So each record
  survives 10 burns per reflash; `$TEST 0?`/`$TEST 1?` report how many are
  left.
- **Boot load** (14:35a8, called from main 14:5066): keep the RAM record
  if its complement and checksum are valid; otherwise copy in the first
  slot whose tag is non-zero. The flash copy's checksum is not checked on
  that path. The RAM copy survives the instrument's soft power-off:
  `POWER OFF` at 84051.487, power-on at 84287.537, and `$DUMP CAL 0?` at
  84449.752 still returns the values loaded by `$LOAD CAL`, although both
  burns had failed and the flash slots were blank.
- **`$BURN CONFIG`** (14:380c): the RAM copy (type, identity) is updated from
  the port unconditionally, then the flash slot is programmed **only if its
  tag is 0xFF**. The config is therefore **write-once per flash erase**.
  The boot path (14:38a5) re-reads flash every time and, if the slot is
  blank, sets type 0 and a blank identity.

### Correction to PROTOCOL.md's "corrupted model name"

**Confirmed from code and capture.** `THM??? SW VERSION 2.00` is not a
partial write. The `ID?` routine (bc:57a8) prints the literal `??? ` for any
config type other than 1-3. After the reflash the config slot was blank
(the image makes it 0xFF), so type = 0. The VPP-off `$BURN CONFIG` then
failed outright and said so (` 1\r` at 83966.003); `$DUMP CAL 2?` at
84448.215 still returned `00` plus 36 spaces, the blank-config fallback.
Because nothing had been programmed, the slot was still blank, which is the
only reason the later VPP-on `$BURN CONFIG` could succeed (` 0\r`,
84477.770). A second `$BURN CONFIG` would now fail.

### Delay constants

PROTOCOL.md's ≈1.0 s after `$BURN CONFIG` and ≈2.0 s after `$LOAD CAL` are
QLOADER-side (0x3e8, 0x7d0 there). **The firmware has no matching delay:**
the handlers block in getc with no timeout. The pause only matters because
the RX ring holds 80 bytes and drops the rest - a DMM payload is 206 hex
characters, so a host that starts sending before the handler is consuming
risks silent loss. The firmware-side delays are the VM sleeps: 300 in
`$RELOAD CODE`, 200 in `BAUD` and `POWER OFF`, each ×0.1 PRT ticks (≈305 ms
and ≈203 ms, assuming the kernel delay at 0x2cce counts PRT0 ticks).

### `$RELOAD CODE`: two different loaders

Opcode 0x9c's handler is `CALL 0x0231` in V2_0. The equivalent opcode in
V1_04 (0xa4 there; the bytecode is otherwise the same `CLS` sequence at
physical 0x1dad4, V1_04 bytecode address 0x4ad4) is `CALL 0x0041` (V1_04
0x663c), and AQ103 matches V1_04 (0x65bf). **The two loaders are
different programs.** Full listings: `loader-V2_0.txt`,
`loader-V1_04.txt`.

**V1_04 / AQ103 loader (0x0041-0x0509, plus the flash primitives at
0x050a-0x06f9 that it shares with the burn code) - confirmed:**

1. DI, SP = 0xf0f0, copy logical 0x0000-0x0fff to RAM, set BBR = 0x80 and
   CBAR = 0x80, so the loader carries on from its RAM copy and flash is only
   reachable through the CA1 window. Flash byte `a` is written at physical
   0x40000 + `a` (CBR = 0x38 + `a`>>12, offset 0x8000 | `a`&0xfff).
   Presumably an alias of the flash's normal 0x00000 decode; that cannot be
   settled statically.
2. Polled ASCI0 at **9600 7E1** (CNTLA0 0x62, CNTLB0 0x08 - a true 7E1,
   unlike `BAUD 3`).
3. **Pre-program every byte to 0x00, erase (0x20, 0x20), wait, then
   erase-verify (0xA0, read, expect 0xFF), re-erasing until clean.** A
   pre-program failure jumps back to 0x00c7, i.e. steps 2-3 again. The
   pre-program/verify loops cover CBR 0x38-0x3b (16 KB) in V1_04 and 0x38-0x3f (32 KB) in AQ103
   (bytes 0xef and 0x1a6 are the only other loader differences); the chip
   erase itself is whole-chip for this flash family. Why only 16/32 KB is
   verified is not settled.
4. **Only then send 0x01.**
5. Per line: collect characters until CR or 0x04. `S1`: 2 hex digits
   count, 4 address, data; `S2`: 6 address digits. Other record types
   (S0, S9) are acknowledged and ignored. **No S-record checksum check and no
   hex-digit validation.** Each data byte goes through the same quick-pulse
   routine (0x0585, 100 pulses). Reply **0x02** per line.
6. A programming failure, or a receive error (getc returns 0 on
   PE/FE/OVRN), jumps back to 0x00c7: **the chip is erased again and 0x01
   is sent again mid-stream.**
7. On 0x04: reply 0x04, then wait for **0x05**, then `OUT (0x80e3),0` and
   HALT - the instrument turns itself off.

This explains every live observation (PROTOCOL.md, `capture.log`), assuming
the 0.54 firmware on the instrument at the time carried this loader:

- 0x01 arrived 11.7 s after `$RELOAD CODE` (83245.541 -> 83257.258): the
  erase cycle.
- **VPP gate, confirmed mechanism:** without 12 V the pre-program step can
  never verify, so step 3 loops forever with interrupts off, running from
  RAM - `CLS` on the display and total silence; nothing that would service
  the power key is running (inferred). It is not a VPP sense line; it is an erase that cannot
  complete.
- 8,065 records (1 S0 + 771 S1 + 7,292 S2 + 1 S9) got 8,065 × 0x02 -
  **including S0 and S9**. The 0x04 was the reply to QLOADER's EOT byte, not
  to the S9 line. PROTOCOL.md's "8066 lines" also counts the `$RELOAD
  CODE` line.
- QLOADER sent **0x05** at 83941.088, 53.5 s after the 0x04 (after the
  operator answered the VPP-off prompt); the instrument next appears at
  83948.697 with a 0xFF and its banner. PROTOCOL.md does not mention the
  0x05.

Hazard for a reimplementation: if 0x01 arrives mid-stream, the flash has
just been erased again. Every record sent before it is gone, so the host
must restart the whole file, not continue. QLOADER ignores the per-line
ack, so it would carry on and leave a flash with only the records after the
failure.

**V2_0 loader (0x0231-0x04d2) - confirmed:**

1. DI, SP = 0xf000, copy 0x0000-0x07ff to RAM, BBR = CBAR = 0x80 (same
   trick); SP = 0x7800.
2. Polled 9600 7E1 (0x0457), send **0x01 immediately. No erase, no VPP
   dependence.**
3. Per line: **only `S1` records are accepted**, and their bytes are stored
   with a plain `LD (HL),C` at the 16-bit address, which at this point
   is RAM. Reply 0x02. Any other record type (S0, S2, S9) re-initialises
   the UART and replies **0x01**.
4. On 0x04: reply 0x04 and jump to 0x0800 - in RAM, where the S1 records
   have just put the new root image. That is the ordinary reset code, whose
   `CBAR = 0x83` at 0x0816 maps flash back over 0x0000-0x2fff, so execution
   continues from the unchanged flash. No 0x05 wait and no power-off.

As read statically, **V2_0's `$RELOAD CODE` cannot reprogram flash**: it is
a download-and-run-in-RAM loader. Fed a `.ROM` file, it would store the root
S1 records in RAM, reboot into the old flash, and reply 0x01 rather than 0x02
to 7,294 of the 8,065 lines - which QLOADER, not checking the ack value,
would not notice. Whether Tektronix meant V2.0 to be reflashed by first
sending a RAM-resident programmer, or meant it never to be field-reflashed,
the code does not say. The consequence stands either way: **the instrument
is now running firmware whose own `$RELOAD CODE` has no erase path, and
its config slot is programmed and has no erase path either.** No bytes or
branch were found that contradict this, but it has not been tested.

## What is resolved and what would need a live capture

Resolved from disassembly alone:

- CPU/MMU, banking, interrupt, UART and flash primitives.
- The complete command set and the dispatcher; per-command behaviour for
  everything firmware/cal/config/serial related.
- The `$BURN CAL`/`$BURN CONFIG`/`$LOAD CAL`/`$DUMP CAL` formats and slot
  logic, the meaning of the 0xC3 tag, and the failure replies. The capture
  confirms all of these.
- Why VPP-off `$RELOAD CODE` wedges and VPP-off burns fail cleanly. The
  wedge part rests on the 0.54 loader behaving like V1_04's, which the
  capture's timing and ack pattern match exactly but which cannot be read
  directly - 0.54 is not on disk.

Resolved by emulation since (2026-09-28, the firmware executed on an
emulated Z180 board and driven by the real QLOADER and by `thm565tools`;
details and caveats in [`FIRMWARE-EMULATION.md`](FIRMWARE-EMULATION.md)):

- **V2_0's `$RELOAD CODE` behaves as read** - 0x01 after 0.71 s with no
  erase, 0x01 for S0/S2/S9 lines, 0x02 for S1, 0x04 for EOT, reboot into
  the unchanged flash; the flash model received no command writes at all.
  As a positive control V1_04's loader erased and reprogrammed the emulated
  chip to exactly the V2_0 image with the historical ack pattern.
  **Confirmed by emulation.**
- DCNTL: the firmware does write it (0x00, see the flash primitives above);
  emulated program pulse 11.56 µs.
- The VPP-off wedge reproduces with V1_04's loader (no accept, pre-program
  loop with interrupts off, flash untouched); VPP-off burns answer ` 1`.

Would still need a live capture (operator sign-off required after the
earlier wedge):

- The physical flash decode (0x40000 alias), RAM size and ADC behaviour -
  the emulator assumes what the firmware needs.
- Real erase/program pulse counts on the actual chip.
- `$TEST 3?` and `$TEST 100-112?`, the `UPLOAD`/`DOWNLOAD`/`LOAD`/`GET`
  block formats, `$KEY INPUT` key codes - dispatch is known, payloads not
  traced.

Open static items: the kernel services (0x0e22 service codes, event
numbers), op_xx names beyond those in `tekvm.py`'s `NAMES`, and what
0a:7cf9 stores into the config record's last four bytes.


## Part 2: Emulation Validation


Third pass on the serial/flash path, after
[`FIRMWARE-DISASSEMBLY.md`](FIRMWARE-DISASSEMBLY.md) and
[`FIRMWARE-SERIAL-FLASH.md`](FIRMWARE-SERIAL-FLASH.md). The firmware images
were executed on an emulated Z180 board and driven by the real `QLOADER.EXE`
(under DOSBox) and by `thm565tools`. **No serial port, instrument or
DOSBox-to-hardware bridge was used anywhere.**

"Confirmed by emulation" below means observed while the unmodified firmware
ran on the emulator. It is only as good as the board model (next section),
and the parts of that model that are assumptions are listed. "Inferred" means
still read from code only. Timestamps are emulated seconds unless marked
wall; `capture.log` times are seconds of day, as in the earlier notes.

## Answer first

- **V2_0's `$RELOAD CODE` cannot change flash - confirmed by emulation.**
  Driven with the full `V2_0.ROM` (8,065 lines) by a reference host, by
  `thm565tools` and by QLOADER itself (`qloader -tTHM565 -csold -clold -p1
  v2_0.rom`, the historical command), the V2_0 loader sent
  0x01 0.7-0.8 s after the command (no erase), answered 771 S1 lines with
  0x02 and the S0, 7,292 S2 and S9 lines with 0x01, answered EOT with 0x04,
  jumped to 0x0800 and rebooted into the old firmware. The flash chip model
  received **zero** command writes from `$RELOAD CODE` to the reboot; the
  code image is byte-identical afterwards. Exactly the static reading.
- **QLOADER reports a V2_0 "reflash" as successful.** It ignores the line
  acks, the V2_0 loader reboots on its own, and the config/cal steps that
  follow succeed; the whole session is indistinguishable from a working one
  on the operator's screen, yet the final flash is byte-identical to a run
  that skipped the code download. A reflash of this instrument cannot be
  judged from QLOADER's output or from the reported version.
- **The emulator can reflash when the firmware can (positive control).**
  With `V1_04.bin` resident, the same file produced one 0x01 after the
  pre-program/erase cycle (5.26 s), 8,065 x 0x02 (S0 and S9 included), 0x04
  for EOT, a wait for 0x05, then `OUT (0x80e3),0` / HALT - the same handshake
  counts as the historical 0.54 -> 2.0 capture. Afterwards the flash array
  equals the `V2_0.ROM` image at all 127,605 image addresses and is 0xFF at
  the other 3,467. Run through QLOADER, the whole historical session
  reproduces - reload, power-off, power-on, `$BURN CONFIG`, `$LOAD CAL` x2,
  `$BURN CAL`, `INIT` -> `THM565 SW VERSION 2.00 0` - and the resulting flash
  image is byte-identical to the one the fixed `thm565tools` produces.
- So the conclusion of `FIRMWARE-SERIAL-FLASH.md` stands and is now
  observed, not just read: **on this instrument (V2.0 installed), no serial
  path reprograms the code flash; `$RELOAD CODE` is a RAM download that
  reboots into unchanged flash.** Nothing found in emulation opens another
  route: the only flash-writing code in V2_0 is the quick-pulse
  programmer at 0x00bc, reached only from `$BURN CAL`/`$BURN CONFIG`, and it
  can only clear bits. See "What this does and does not settle" for the one
  theoretical route (a RAM-resident programmer) that neither static analysis
  nor emulation excludes.
- **`thm565tools` had six defects, not two.** All six were reproduced in
  emulation and each is tied to ground truth in `capture.log` or the
  disassembly; all six are fixed and re-verified (section 5). Two of them
  were dangerous on real hardware: the short `$BURN CONFIG` payload
  burns a corrupted identity into the write-once config slot on the next
  command, and the old `reload_code()` gives up on a 0.54/V1.04-class loader
  while it is erasing, leaving a fully erased flash.

## 1. What was built

All under `emulator/` (private):

| Path | What | Licence / provenance |
| --- | --- | --- |
| `z180emu/` | Upstream clone, untouched: github.com/mtdev79/z180emu @ `32592c7` (2021-07-28) | GPL-2.0-or-later (core MAME-derived, ASCI BSD-3) |
| `z180sys/core/` | Copy of `z180emu/z180/` with fidelity patches, each marked `z180sys patch` | as upstream |
| `z180sys/z180sys.c`, `build.cmd` | Generic system layer, built to `z180sys.dll` with the MSVC 2022 Build Tools already on this machine (no MSYS2 needed) | own code linked with GPL core -> GPL |
| `z180sys/z180sys.py` | ctypes binding: physical memory map (4 KB pages over caller-owned buffers), I/O and CSI/O callbacks, board timers, breakpoints, trace ring, ASCI frame queues | own code |
| `z180sys/serialwire.py` | Host side: `CoSim` (host code runs in emulated time), `VirtualSerial` (pyserial-compatible port), `TcpBridge` (real-time TCP, for DOSBox nullmodem) | own code |
| `z180sys/intel28f.py` | Pluggable Intel 28F010/020 command-register flash model | own code |
| `thm565/board.py` | THM565 board: memory map, flash wiring, VPP, ADC/INT0, stubs | own code |
| `thm565/*.py` | Probes and runners (`reload_probe.py`, `run_qloader.py`, `run_thm565tools.py`, ...) | own code |
| `runs/` | Every run's wire log, summary JSON and final flash image | outputs |

Nothing in `z180sys/` knows about the THM565; everything instrument-specific
is in `thm565/board.py`.

### Core patches (z180emu -> z180sys/core)

Each was needed to run this firmware faithfully:

1. ASCI bit timing now counts T-states (phi / (PS x DR x 2^SS) per bit).
   Upstream advanced one bit per 16 instructions and ignored DR.
2. PRT, ASCI and CSI/O interrupt requests are level-sensitive and
   re-evaluated before every instruction. Upstream latched them only if IFF1
   was set at the moment the condition arose - a byte received inside a
   DI section never interrupted.
3. ASCI STAT/CNTLB reads put /DCD0 and /CTS in bits 2 and 5 (upstream OR-ed
   them into bit 0, i.e. TIE and SS0), and the channel-0 /DCD0 test had
   inverted sense (permanent RX interrupt whenever RIE=1).
4. Writing EFR=0 to CNTLA clears OVRN/PE/FE; reading RDR always consumes the
   byte (upstream could never get past an errored byte). Receive buffer depth
   is configurable (default 2 = RDR + RSR).
5. Memory and external-I/O wait states from DCNTL are charged to every
   access (upstream charged them to DMA only); DCNTL resets to 0xF0 per the
   Z80180/HD64180 data sheet (upstream 0x00).
6. CSI/O implemented (upstream: RE/TE never clear). The firmware polls it.
7. IN0 and TSTIO set flags as documented; undefined-opcode TRAPs are
   counted and stop the run.
8. Stop/resume from breakpoints without re-running the interrupt check (a
   stop inside an EI shadow otherwise changes behaviour).

Emulation is deterministic: identical RAM/PC at checkpoints across slice
sizes.

### Board model: facts and assumptions

| Item | Model | Basis |
| --- | --- | --- |
| Clock | 6.144 MHz | Inferred earlier from both confirmed CNTLB0 baud values |
| Flash | 128 KB 28F010, mapped at 0x00000 and aliased every 128 KB below 0x80000 | 0x00000: V2_0 code/burns. 0x40000: V1_04 loader writes there. **The alias is an assumption both firmwares require**, not measured |
| RAM | 128 KB at 0x80000 | **Assumption.** The kernel heap is reached by far pointers from 0x88000 up; with 32 KB the firmware asserts `TIMER ALLOCATION FAILED` (string 0x2850) 0.24 s after reset |
| 0xC0000-0xFFFFF | 256 KB plain RAM | **Assumption.** Acquisition buffers (BBR 0xCC, 0x0948) |
| ADC | `OUT (0x807d),1` starts a conversion; 20 ms later INT0 is raised and 0x80e1 bit 2 set; reading 0x80e1 acknowledges; data ports read 0 | **Inferred** from 14:3181 and the INT0 handler 0x0e29 (bit 2 posts event 1, which ends the measurement wait). Without it the VM task never runs and nothing is sent on the port |
| Port 0x8000 | reads 0 ("not busy") | V1_04 polls it until 0 (0f:7a05) |
| Other external ports | read 0xFF; all accesses logged | - |
| CSI/O device | shifts in 0xFF | Unknown peripheral |
| VPP | switch attribute feeding the flash chip | Fixture switch; firmware cannot sense it (confirmed earlier) |

Unmodelled: display, keypad, scope acquisition, INT1, battery. The scope
commands (`$scope ch1 calibrate offset`) do not complete on this board.

## 2. Fidelity against ground truth

| Check | Real (`capture.log` / captures) | Emulated |
| --- | --- | --- |
| Power-on banner, V2_0 with blank config | `THM??? SW VERSION 2.00 0\r` | identical (3.7 s after reset) |
| `$DUMP CAL 2?`, blank config | `00` + 36 spaces + ` 0\r` (84448.215) | identical |
| `$DUMP CAL 1?` reply | 393 bytes, 3.213 s on the wire | 393 bytes, 3.27 s |
| `$LOAD CAL n` reply | bare CR (83991.519, 84025.731) | bare CR |
| `$BURN CAL` / `$BURN CONFIG`, VPP on | ` 0\r` | ` 0\r` |
| `$BURN CAL`, VPP off | ` 1\r` 3.14 s after the command | ` 1\r` after 2.4 s, flash unchanged |
| `$BURN CONFIG`, VPP off | ` 1\r` 4.67 s after the command (QLOADER pacing) | ` 1\r` after 3.55 s |
| `BAUD 3` | ` 0\r`, then 9600 | ` 0\r`, UART reprogrammed ~0.2 s later |
| `$RELOAD CODE` with VPP off, 0.54 loader | total silence, `CLS`, needed a power pull | V1_04: no accept in 60 s, loader spinning in pre-program with interrupts off, 4.3 M flash writes ignored, flash unchanged |
| Reload handshake, erase-capable loader | 1 x 0x01, 8,065 x 0x02, 0x04 for EOT, then 0x05 | V1_04: identical counts |
| Accept delay after `$RELOAD CODE` | 11.7 s (0.54) | 5.26 s (V1_04) |
| `INIT` -> banner | 5.5-5.7 s | 3.9 s |

The two timing differences are expected: 0.54's loader is not on disk, and the
flash model completes an erase in one 45.8 ms pulse where a real chip may
need several; `INIT` is shorter because the display and other boot-time
hardware are not modelled. Neither affects what bytes are exchanged.

## 3. `$RELOAD CODE` with V2_0 resident

Three independent drivers, same result:

| Driver | Result |
| --- | --- |
| `reload_probe.py` (QLOADER's documented sequence, co-simulated) | accept 0x01 after 0.712 s; S0 0x01; S1 771 x 0x02; S2 7,292 x 0x01; S9 0x01; EOT 0x04; banner after reboot; 0 flash commands; array unchanged. `runs/probe_V2_0_full.json` |
| QLOADER.EXE, DOSBox, `-tTHM565 -csold -clold -p1 v2_0.rom` | first reply 0x01 0.80 s after `$RELOAD CODE`; line acks 7,294 x 0x01 + 771 x 0x02; EOT 0x04; loader at 0x03c5 then 0x0800; banner `THM??? SW VERSION 2.00 0` 2.8 s after 0x05 without any power cycle. QLOADER does not look at the line acks and carries on to `$BURN CONFIG` (` 0`), `$LOAD CAL` x2, `$BURN CAL` (` 0`), `INIT`/`ID?` -> `THM565 SW VERSION 2.00 0`, i.e. a session that looks completely successful. The flash chip saw only the 340 program pulses of those burns and no erase, and **the final flash image is byte-identical to that of a `qloader -nc` run that never sent the code at all** (`runs/q_v20_reload2/` vs `runs/q_v20_nc/`). (`runs/q_v20_reload/` is an earlier run whose cal steps were cut short by a bug in the harness's operator model - it power-cycled the instrument during QLOADER's slow `$LOAD CAL` payload; its reload part is valid and identical.) |
| `thm565tools` (fixed) | stops at the S0 line with "V2.0-style RAM loader, nothing written"; array unchanged. `runs/t_reload_v20_fixed/` |

Loader trace (breakpoints): entry 0x0231 with CBAR 0x83; 0x02b7 "UART init +
send 0x01" for every non-S1 line; 0x03bd per S1 record; 0x03c5 on EOT, then
0x0800 with CBAR = BBR = 0x80 (the RAM copy), which reprograms CBAR to 0x83
and continues from flash. The S1 records land on the loader's own RAM copy
(0x0040-0x04d3, identical bytes for `v2_0.rom`); the loader stack at logical
0x7800 is outside the S1 range, so it survives.

## 4. Positive control: V1_04 resident

`reload_probe.py V1_04 V2_0.ROM`, fixed `thm565tools` (full flow) and QLOADER
(`runs/q_v104_reload/`: accept 5.2 s after the command, 8,065 x 0x02, EOT
0x04, 0x05, instrument off; operator power-on; banner
`THM??? SW VERSION 2.00 0`; `$BURN CONFIG` ` 0`; `$LOAD CAL` bare CR x2;
`$BURN CAL` ` 0`; `INIT`/`ID?` `THM565 SW VERSION 2.00 0`). Loader
milestones: entry 0x0041; erase
(0x20, 0x20) at 12.02 s after 16 KB pre-program; 0x01 after erase-verify;
8,065 records; EOT; wait for 0x05; `OUT (0x80e3),0`; HALT. Flash chip:
1 chip erase (45.8 ms pulse), 136,178 program pulses, no short pulses.
After the fixed tool's full flow (reload, power-on, `$BURN CONFIG`,
`$LOAD CAL` x2 with read-back, `$BURN CAL`) the flash equals the V2_0
image everywhere except 340 bytes: the config slot (42) and the first DMM
(103) and SCOPE (195) slots, and the instrument reports
`THM565 SW VERSION 2.00 0`.

Observation, not a failure: V1_04 pre-programs only CBR 0x38-0x3b (16 KB)
before the chip erase, so 107,444 of the 131,072 bytes are erased without
being pre-programmed to 0x00, which the 28F010 data sheet requires to avoid
over-erase. The real instrument survived this once; it is a margin question
for a real chip, not a protocol one.

## 5. `thm565tools`: defects found, fixed, re-verified

Original copied to `runs/thm565tools_orig/` before any change; the harness
runs either copy, unmodified, with only `serial.Serial`, `time` and `input`
replaced from outside.

| # | Defect | Emulated evidence (original code) | Ground truth | Fix |
| --- | --- | --- | --- | --- |
| 1 | `$BURN CONFIG` identity 32 chars (8/9/10 fields) + CR; firmware reads exactly 2 + 36 raw chars, no timeout | Tool times out; firmware still waiting for 3 characters. If the operator re-runs the tool, its `INIT\r` supplies them and the config slot is burned as `\xc3\x03THM565   B090062   SW 2.0       \rINI` + 4 bytes, answered ` 0`, then `COMMAND NOT DEFINED` for `T\r` (`runs/t_burnconfig_retry_orig/`). V2.0 cannot erase that slot | QLOADER payload `03THM565    B090062   SW 2.0  \x00       ` (38 bytes, capture 83961.338 and 84475.395) | `config_payload()`: model 10, serial 10, `SW `, version 5, NUL, 7 spaces; length-checked; identical bytes to QLOADER |
| 2 | `$LOAD CAL` success check requires trailing `0`; reply is a bare CR | `RuntimeError: '$LOAD CAL 0' payload did not report success: ''` (`runs/t_loadcal_given_orig/`) | 83991.519, 84025.731 | Expect a bare CR; since it carries no status, read the block back with `$DUMP CAL n?` and compare before `$BURN CAL` |
| 3 | `reload_code()` sends `$RELOAD CODE` before `BAUD 3` and reads the accept with the 2 s port timeout | V2_0: the loader's 0x01 goes out at 9600 7E1, the tool at 1200 8N1 reads 0xff and aborts, leaving the instrument inside the RAM loader with interrupts off. V1_04: the 2 s read times out during the erase, the tool aborts, **the flash is left entirely 0xFF** (`runs/t_reload_*_orig/`) | QLOADER: `BAUD 3` 83236.145, `$RELOAD CODE` 83244.581, accept 12.7 s later | BAUD 3 + ack, switch, 1 s settle, `$RELOAD CODE`, 120 s accept timeout; any ack other than 0x02 stops the transfer with a specific message (0x01 on S0 = V2.0 RAM loader; 0x01 later = flash re-erased); read the 0x04 EOT reply; after 0x05 prompt the operator to switch the instrument on and wait for the banner |
| 4 | `INIT` read with the 2 s default timeout; banner comes after the reboot | `flash.main()` fails at its very first command (`runs/t_skipcode_orig/`) | 5.5-5.7 s (83219->83224.985, 84044.933->84050.619, 84440.080->84445.800) | `THM565Serial.init()`, 15 s |
| 5 | `$DUMP CAL 1?` (393 chars, 3.3 s) and failing burns (3-5 s) exceed the 2 s timeout; payload reply deadline started before the payload was on the wire | `backup_cal()` fails on `$DUMP CAL 1?`; `$LOAD CAL 1` reply arrives 5.3 s after the command | 83231.821-83235.034; VPP-off burns above | Dump 10 s, burns 30 s, payload deadline + payload wire time |
| 6 | CR appended to the `$BURN CONFIG` and `$LOAD CAL` payloads | The CR arrives as an empty command line and draws `COMMAND NOT DEFINED 0\r`, which the next `command()` would take as its own reply | QLOADER sends no terminator (38 / 206 / 390 bytes, checked in the capture) | Payload written raw |

Also in `calibrate.py` (checked only as far as the model allows):
`$TEST n?` answers a bare number (`10\r`), so parsing it through the
status check fails as soon as fewer than 10 slots remain; and index 0 is
SCOPE, 1 is DMM (the tool had them swapped; confirmed by seeding one DMM
slot - `$TEST 1?` = 9, `$TEST 0?` = 10). Fixed with `THM565Serial.query()`;
its `INIT` uses `init()`. The DMM/scope calibration steps themselves need the
analog front end and were not validated.

Re-verification with the fixed code (`runs/t_*_fixed/`):

- `skipcode` (backup, burn config, load + read-back + burn cal, INIT):
  completes; config slot `c3 03 "THM565    B090062   SW 2.0  " 00 20x7 00 00 80 bf`;
  DMM/SCOPE slot 0 tagged 0xC3; `INIT` -> `THM565 SW VERSION 2.00 0`.
- `loadcal_given` with the real B090062 blocks from the capture: completes,
  slots hold `C3 3E C1 ...` / `C3 B5 4A ...`.
- `reload` with V2_0 resident: stops at the S0 line, nothing written.
- `full` with V1_04 resident: reload, power-on, config, cal - completes (section 4).

QLOADER vs `thm565tools` (fixed), same starting state, final 128 KB flash
images compared byte for byte:

| Sequence | QLOADER run | `thm565tools` run | Differing bytes |
| --- | --- | --- | --- |
| V2_0 resident, config + cal only (`qloader -nc -csold -clold`) vs `flash.main --skip-code` | `runs/q_v20_nc/` | `runs/t_skipcode_fixed/` | **0** |
| V1_04 resident, reload `V2_0.ROM` + config + cal | `runs/q_v104_reload/` | `runs/t_full_v104_fixed/` | **0** |

Both write the same 38-byte config payload and leave the config slot as
`c3 03 "THM565    B090062   SW 2.0  " 00 20x7 00 00 80 bf`. On the wire
they differ only in pacing: QLOADER sends payload characters ~10-90 ms apart
(its `$LOAD CAL 1` takes 34 s, as in the real capture), `thm565tools` at full
1200 baud; the firmware kept up (no overruns, read-back identical).

## 6. Other facts established by emulation

- **The firmware writes DCNTL = 0x00** at 0a:74f0 (physical 0x114f0,
  `LD BC,0x32 / LD E,0 / OUT (C),E`), about 2,000 T-states after reset.
  `FIRMWARE-SERIAL-FLASH.md` said DCNTL is never written; the listing misses
  it because the port is in BC. With zero wait states the program pulse
  (4 x `EX (SP),HL` between the data write and 0xC0) is **11.56 us**
  (71 T-states), just over the data sheet's 10 us; every byte programmed on
  its first pulse. The V1_04 erase pulse is 45.8 ms.
- **`INIT`'s reply is the banner after a full reboot**, 3.9 s emulated,
  5.5-5.7 s real.
- The firmware runs a small preemptive kernel (service entry 0x0e22,
  parameter block in HL, service 1 = wait for event with timeout, 8/9/10/12
  = timer services); the VM runs as one task among ~12. Its measurement task
  waits for ADC event 1 posted by the INT0 handler.
- An idle V2_0 halted with interrupts off 428 s after power-on in one run
  with no serial traffic - presumably the auto power-off timer (inferred).
- QLOADER specifics seen while running it: it asks for the serial number
  on stdin when `-b` is not given (C runtime line input, which reads to LF
  and buffers a chunk of the file); `$BURN CONFIG`'s payload goes out at
  10 ms per character (0x141c). Unattended runs therefore need a key file
  of `serial\r\n` followed by CRs for the ENTER prompts.

## 7. What this does and does not settle

Now resolved by emulation (previously "would need a live capture"):

- V2_0 `$RELOAD CODE` behaviour: immediate 0x01, 0x01 for non-S1 lines,
  reboot on EOT, flash unchanged - confirmed.
- The program pulse length for the emulated timing (11.56 us, given the
  DCNTL write above).

Still open:

- **A RAM-resident programmer.** V2_0's loader will execute whatever S1
  records put at 0x0800 (it jumps there on EOT). A purpose-built S1 image
  containing an erase/program routine and a receive loop could in principle
  reflash the chip without chip removal. Nothing on disk does this, it has
  never been tried, and building one is a separate, risky project; it is
  mentioned only because it is the one serial route neither analysis rules
  out. It must not be attempted on the instrument without emulator trials
  first.
- The physical flash decode (the 0x40000 alias), RAM size, and the ADC
  model are assumptions the firmware needs, not measurements.
- 0.54's loader is not on disk; its equivalence to V1_04's still rests on
  the matching ack pattern.
- Real erase/program pulse counts, real `INIT` timing, and everything
  involving the analog front end, display, keypad and scope.

## 8. How to rerun

```
cd analysis\emu\z180sys && build.cmd                       # z180sys.dll
cd ..\thm565
python probe_boot.py ..\..\V2_0.bin 4                      # boot + I/O census
python reload_probe.py V2_0 ..\..\..\V2_0.ROM              # reference reload
python run_thm565tools.py NAME skipcode|full|reload|loadcal_given|... [--fw V1_04] [--tools DIR]
python run_qloader.py NAME V2_0 --stdin-keys --sdl-driver "" --key-interval 0 --screen -- -tTHM565 -csold -clold -p1 v2_0.rom
```

## 9. Recommendations

- **Reuse.** `z180sys/` (patched core + `z180sys.c` + `z180sys.py` +
  `serialwire.py` + `intel28f.py`) has no THM565 knowledge and can move as a
  unit to a shared tools location for other Z80/Z180-family work; boards
  are separate Python modules like `thm565/board.py`. It is GPL-2.0-or-later
  because the core is.
- **Regression test for `thm565tools`.** The `run_thm565tools.py`
  scenarios are a ready regression suite. Shipping them with the public
  MIT repo would mean shipping or depending on the GPL core - the owner's
  decision. An MIT-clean alternative is to ship recorded instrument
  transcripts (from `runs/*/wire.txt`) replayed by a small fake port, and
  keep the emulator private.
- `THM565-Flash/README.md` still says VPP mistakes do not brick the
  instrument and describes the identity incident as a partial write; both
  are superseded (defects 1 and 3 above, and `FIRMWARE-SERIAL-FLASH.md`).
  Not edited here.
