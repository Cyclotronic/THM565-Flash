# THM5xx QLOADER protocol notes

Reverse-engineered from `QLOADER.EXE` (Borland C++ 1991, DOS MZ, small model,
25716 bytes) by static disassembly (capstone, x86-16) cross-validated against
`DEFAULT.CAL`. Goal: enough detail to reimplement flashing/cal/config natively
(e.g. pyserial) instead of running the DOS binary.

Binary layout facts used throughout: load module starts at file offset 512
(header is 32 paragraphs). CS = 0 at entry (IP=0). DS is a separate segment
starting at flat (CS-relative) offset **0x4A20 (18976)** — confirmed by
correlating 10/11 known string addresses against disassembled immediate
operands. All "ds_off" values below are `file_offset - 512 - 18976`.

## Command inventory (string address -> code cross-reference)

| Command / string | ds_off | code addr | context |
|---|---|---|---|
| `$RELOAD CODE` | 0x06d4 | 0x7c5 | firmware download |
| `$BURN CONFIG` | 0x092e | 0xa5b | write instrument type into flash |
| `INIT` | 0x09b2 | 0xabe | reinit after config burn |
| `default.typ` | 0x0c18 | 0x115e | type-number lookup table |
| `$DUMP CAL 2?` | 0x0faa | 0x152e | fetch COMMENT block |
| `$DUMP CAL 0?` | 0x0fc6 | 0x1560 | fetch DMM cal block |
| `$DUMP CAL 1?` | 0x109e | 0x1683 | fetch SCOPE cal block |
| `$LOAD CAL 0` | 0x12f6 | 0x1947 | push DMM cal block back |
| `$LOAD CAL 1` | 0x1327 | 0x19a6 | push SCOPE cal block back |
| `$BURN CAL` | 0x1360 | 0x19d6 | commit both cal arrays to flash |

## Cal-block checksum (CONFIRMED)

Routine at code offset `0x14ff`:

```
checksum(buf, len):
    cs = 0x5A
    for b in buf[0:len]:
        cs ^= b
    return cs
```

Wire/file block format (as in `DEFAULT.CAL`, hex-ASCII encoded, `>`...`<`
delimited): `[type_byte][checksum][~checksum][payload...]`

- `type_byte` (buf[0]) — not covered by the checksum, meaning/values not yet
  identified (`0xC3` for the THM565 DMM default block).
- `checksum` (buf[1]) = `checksum(buf[3:], len-3)`.
- `~checksum` (buf[2]) = bitwise NOT of buf[1].
- payload (buf[3:]) — IEEE-754 little-endian floats, gain/offset constants.

Verified two ways:

1. Against `DEFAULT.CAL`'s DMM block by hand: payload is 25x the 4-byte
   sequence `00 00 80 3F` (1.0f). XOR-chaining from seed `0x5A` over an odd
   number (25) of repeats of that 4-byte group lands on `0xE5`; even repeats
   return to `0x5A`. Header bytes in the file are `C3 E5 1A`, i.e.
   checksum=`0xE5`, complement=`0x1A` = `0xFF-0xE5`. Matches exactly.
2. Against a **live capture** of the real THM565 answering `$DUMP CAL 0?`
   and `$DUMP CAL 1?` (see Live capture below) - real, non-degenerate DMM
   and SCOPE payloads, both computed-vs-stored checksum AND complement match
   exactly. This is a much stronger check than (1) since the payload isn't
   a single repeated value.

On mismatch the loader emits `invalid checksum (expected 0x%02x, actual
0x%02x)` (checksum) or `invalid checksum compliment (...)` (complement) and
retries the `$DUMP CAL n?` up to 3 times before giving up
(`NO {DMM,SCOPE} CAL DATA AVAILABLE AFTER 3 TRIES`).

Hex decoding happens at code offset `0x1496`: pairs of ASCII hex chars ->
bytes, nibble validated via `0x145f` (accepts `0-9`, `A-F`); an odd total
digit count trips `error: hex ascii string length %d is odd`.

## `$RELOAD CODE` firmware download (code 0x7c5–0x957)

1. Send `$RELOAD CODE`.
2. Read one response byte (via 0xebe). If it's not `1` (accept), loop
   prompting/retrying until a `\r` (0x0d) is seen, then abort with
   "Quartz rejected RELOAD CODE command."
3. On accept: stream the `.ROM` file (Motorola S-record text) to the serial
   port **line by line**, translating `\n` (0x0a) to `\r` (0x0d) as the line
   terminator (buffer at 0x2616, flushed via write-call `0xf1c` once `\r` is
   hit). This matches the S19 files on disk being sent essentially verbatim,
   one S-record line per write.
4. Progress reporting: every 10 lines, print one tick character from a
   lookup table at ds_off `0xaa`; every 200 lines print a status line; at
   line 8000 (`0x1f40`) print a completion marker — matches the on-screen
   `0 ... 8000` progress ruler string.
5. At EOF: send a 1-byte terminator, literal value `0x04` (ASCII EOT) —
   built at 0x2616/0x2617 as bytes `04 00` then written with length 1.
6. Prompt "Turn the adapter board VPP switch OFF", wait for ENTER/ESC
   (`0x17de`) — ESC aborts with error code 0xb.
7. Print "download complete / instrument off is normal" text, prompt to
   power the instrument back on.

Not yet pinned down: exact framing/response-check *during* the S-record
stream (whether each line gets an ACK char back, or it's send-and-forget
until EOT) — the disassembly above shows only line buffering + a periodic
progress tick, not a per-line read-back; needs either deeper disassembly of
0xebe's call sites during the loop, or a live capture to settle it
empirically.

## Still open (need either more disassembly or a live capture)

- Serial init: actual baud rate (string `BAUD 3` looks like a debug/log
  line printing a baud *table index*, not a literal rate — DOS INT14 baud
  tables commonly map index 3 to 600 or 1200; not confirmed here).
- Whether `$RELOAD CODE` line transfer expects per-line ACKs.
- `$BURN CONFIG` (0xa5b) and `$LOAD/BURN CAL` (0x1947–0x19d6) exact byte
  framing (should be similar shape to `$DUMP CAL`, not yet disassembled in
  detail).
- Meaning of the unchecked type/tag byte at buf[0] of each cal block.
- Function inventory needed: `0xebe` (serial read w/ timeout), `0xf1c`/
  `0xf5c` (serial write), `0x17de`/`0x1a0f` (keypress wait), `0x3f7c` (file
  getc) — not yet fully disassembled, only inferred from call context.

## Live capture (2026-09-27)

Captured via `capture_bridge.py` + `dosbox_thm565.conf` (DOSBox nullmodem
client → local TCP → real COM21 → the 067-1446-99 fixture → this THM565),
running `qloader -tTHM565 -nc -nv -cs capture1 -p1 v2_0.rom` (cal-dump only,
nothing burned). Full log: `capture.log`.

New facts learned that couldn't come from static disassembly:

- **This instrument's serial number is B090062**, currently running
  **firmware SW 0.54** — older than either `.ROM` file on disk (`V1_04`,
  `V2_0`), decoded from the `$DUMP CAL 2?` COMMENT block:
  `03THM565    B090062   SW 0.54 ` (type digit, model, serial, firmware).
- Confirmed 1200 baud 8N1 is correct for the `$DUMP CAL` exchange (bytes
  arrive at ~8.3ms/byte spacing, matching 1200 baud exactly).
- Confirmed command framing is bare `$DUMP CAL n?` + `\r`, response is the
  hex-ASCII block + ` 0` + `\r` (the trailing ` 0` is a separate status
  code, not part of the checksum-covered block).
- Real DMM (25 floats) and SCOPE (48 floats) blocks both validate the XOR
  checksum exactly against non-degenerate data — a much stronger check than
  the single-value `DEFAULT.CAL` sample above.
- Both blocks use tag byte `0xC3` (same as in `DEFAULT.CAL`) — consistent
  with it being a fixed format marker rather than block-type-specific,
  though still not conclusively identified.

## VPP-off attempt on `$RELOAD CODE` — wedges the instrument (2026-09-27)

Tried the "pretend mode" plan: `qloader -tTHM565 -csold -clold -p1 v2_0.rom`
with VPP physically left off. Confirmed the `BAUD 3` → 9600 7E1 switch is
exactly right (0xFA as an INT14 AH=0 parameter byte decodes to 9600/7/Even/1
bit-for-bit), and the bridge now delays 300ms after seeing `BAUD 3` sent so
the `0\r` ack is captured before switching — that part works cleanly.

But `$RELOAD CODE` itself then wedges: the instrument's display changes to
show `CLS` (it received and acted on *something*), but it never sends back
the single accept byte `qloader` is waiting for, so the DOS side sits in
"Program continuing......." forever — the read loop has no timeout and
ESC does nothing at this point. Recovery required **pulling power** from
the instrument; a normal power-cycle attempt didn't clear it.

Working theory: the instrument may gate the accept byte on sensing real VPP
hardware state, not just on receiving the command — i.e. QLOADER's on-screen
prompt can be answered dishonestly, but the instrument's own hardware check
(if it has one) can't be. Not proven, but the display change + total serial
silence + un-recoverable-without-hard-power-cycle pattern fits that better
than a leftover comms bug (framing was independently confirmed correct).

**Practical implication:** the VPP-off "risk-free" capture plan does not
work past the `$RELOAD CODE` handshake. Observing that phase for real
would mean actually asserting VPP — i.e. a genuine flash attempt, not a
safe dry run. Cal constants are already backed up locally (`-csold` from
the earlier successful `-nc` pass) as a precondition if that's the chosen
path.

## Successful real flash to firmware 2.0 (2026-09-27)

With VPP genuinely on, `qloader -tTHM565 -csold -clold -p1 v2_0.rom` through
`capture_bridge.py` completed a full real flash of THM565 B090062 from SW
0.54 to SW 2.0. The `$RELOAD CODE` S-record transfer (359,308 bytes over
~12 minutes) completed cleanly for the first time in any attempt this
session - confirms the earlier VPP-off wedge really was gated on VPP, not a
bridge or protocol bug.

One operator error along the way, worth recording exactly because it's an
easy mistake to repeat: VPP was left off for the immediately-following
`$BURN CONFIG` step (which writes the `THM565` identity string) and the
cal load/burn. Result: the firmware itself was fine (`SW VERSION 2.00`
came back correctly), but the model-name field was corrupted to
`THM??? SW VERSION 2.00` (0x3F filler bytes) - a partial/garbled write,
not a clean failure. The instrument still booted and talked serial
normally throughout; this was cosmetic/identity corruption, not a brick.

**Fix, confirmed working:** rerun with `-nc` (skip the already-successful
code download) and VPP genuinely on this time:

    qloader -tTHM565 -nc -csold -clold -p1 v2_0.rom

`$BURN CONFIG` returned `0` (success) and the instrument now correctly
reports `THM565 SW VERSION 2.00`. Cal constants round-tripped correctly
(`$LOAD CAL 0`/`$LOAD CAL 1` both returned `0`) and were verified byte-for-
byte identical to the pre-flash backup (`B090062.cal`).

**Lesson for a modern reimplementation:** `$RELOAD CODE`, `$BURN CONFIG`,
and `$BURN CAL` are three independently VPP-gated writes, not one combined
operation - a program that automates this must make VPP timing an explicit,
per-step checkpoint (or an operator prompt per step), not a single
up-front "is VPP on?" gate before the whole sequence.

## Per-line $RELOAD CODE framing (confirmed from the real transfer)

Counted directly against the real 359,308-byte transfer: 8066 CR-terminated
S-record lines sent, 8067 single-byte responses came back - one `0x01`
accept up front, 8065 `0x02` per-line acks, and one distinct final-record
ack (`0x04`, on the S9 terminator line). So it's a real per-line handshake,
not a blind blast: send line, read exactly one ack byte, repeat.

## Code review findings (2026-09-28) - explaining the "unclean" behaviors

Went back to the disassembly specifically to explain what we could never
observe live (the silent VPP-off hangs, and how a "clean" rejection would
have looked instead). No live capture involved this time, pure static
analysis, cross-checked against what we already captured.

**Why the hang has no escape (confirmed, not inferred):** the byte-read
primitive at code offset `0xebe` is a tight BIOS INT14 polling loop
(`AH=3` get status, `AH=2` receive if data-ready) that masks the received
byte to 7 bits and **unconditionally loops back to the top whenever the
result is 0** - which conflates "no data ready yet" with "a literal 0x00
byte was received" and treats both as "keep polling." There is no counter,
no timeout, and no keyboard/ESC check anywhere in this function. It only
ever returns once a nonzero byte shows up. This is the entire mechanism
behind every indefinite hang we hit - confirmed by reading the function
itself, not just inferred from symptoms.

**What the "clean rejection" path actually requires:** the caller (at
`0x7c5`) reads one byte via `0xebe`. If it's exactly `1`, accept. Otherwise
it enters a loop that ECHOES every subsequent byte to the screen (`printf
"%c"`) and keeps calling `0xebe` again until it sees a literal CR (`0x0d`)
- only then does it print "Quartz rejected RELOAD CODE command." and return
a clean error. Since `0xebe` itself never returns on true silence, this
means: if the instrument sends absolutely nothing, `qloader` hangs on the
*very first* read, before ever reaching the echo/reject loop. The clean
rejection message is only reachable if the instrument sends *something*
(even garbage) that eventually includes a CR. This matches our repeated
VPP-off observations exactly - total silence, not a rejection message -
which supports the "erase gate blocks even the accept/reject decision, not
just the write" reading of the wedge.

**`$BURN CONFIG` and `$LOAD CAL 0/1` are two separate writes with a fixed
delay, not one concatenated write.** Confirmed both in the disassembly and
by checking the raw captured bytes (an earlier read of the capture as text
made it look like the command and payload were concatenated with no
separator - they aren't; a `\r` and a real delay were just easy to miss
when eyeballing decoded text). Measured against the real capture: `$BURN
CONFIG\r`, then ~1.0s (disassembly constant `0x3e8` = 1000ms), then the
identity payload. `$LOAD CAL 0\r`/`$LOAD CAL 1\r`, then ~2.0s (`0x7d0` =
2000ms), then the hex payload. `thm565tools/flash.py` originally sent
these as one concatenated write and has been corrected to match.

**`qloader` itself silently swallows `$BURN CAL` errors.** The response
check at `0x19ea` looks at byte 1 of the response buffer for `'0'`; if it's
anything else, it prints `$BURN CAL error "%s"` - but the function then
unconditionally returns success (`xor ax,ax`) regardless, so the caller/UI
never actually learns the operation failed. Worth knowing if anyone ever
trusts `qloader`'s own success/failure reporting for this step; our
reimplementation checks the response itself rather than relying on
`qloader`'s exit behavior, so it doesn't inherit this bug.

**The `$KEY INPUT` "special hack" block is cleanly type-gated**, not a
generic error path - it's guarded by `cmp word ptr [0x189c], 4` (THM571's
type number per `DEFAULT.TYP`), so it's structurally impossible to hit
against a THM565 (type 3). Explains why we never saw it in any capture.

## What qloader actually checks for errors (2026-09-28)

Two very different patterns, confirmed by disassembly:

- **`$RELOAD CODE` accept byte**: strictly checked, `cmp di, 1` - must be
  exactly `1`, nothing looser.
- **`$BURN CAL` and `$BURN CONFIG`**: both check response byte 1 for
  literal `'0'` (0x30); anything else triggers `printf` of an error message
  (`"$BURN CAL error \"%s\""` / `"$BURN CONFIG error \"%s\""`). **In both
  cases, confirmed by disassembly, the function returns success regardless**
  - the error is cosmetic, never propagated to the caller or exit status.
- **The per-line ack during the S-record transfer itself is NOT checked at
  all.** Found the exact read: right after each line is sent (`call 0xf1c`
  at `0x851`), the next instruction is `call 0xebe` (`0x856`) storing the
  result in `di` - which is then never referenced again anywhere in the
  loop. It's read purely to consume/pace the stream, not validated. Since
  `0xebe` blocks forever on silence, this loop can only ever hang on a
  dead instrument; it cannot detect or reject a "wrong" ack byte, because
  it never looks at the value.

Net effect: the highest-stakes operation (firmware transfer) has zero
per-line validation, while the two lower-stakes burn operations have
validation that's real but silently discarded. `thm565tools/flash.py`
checks the per-line ack strictly (rejects anything other than the two
observed values) - stricter than `qloader` itself, worth knowing since a
real transfer that `qloader` would silently accept could get rejected by
our tool instead.

## Remaining open items

- The `0xC3` tag byte's meaning in cal blocks.
- `-nk`, `-b`, `-tf` command-line paths, and anything specific to
  THM550/560/571 - never exercised, no hardware to test them against.
