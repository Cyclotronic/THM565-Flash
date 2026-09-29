# THM565 flash and calibration tools

Native (no DOSBox, no vendor QLOADER.EXE) reimplementation of the RS-232
protocol Tektronix's 1994 `QLOADER.EXE` uses to reload firmware, burn the
instrument identity string, and load/burn calibration constants on THM5xx
"TekMeter" instruments - plus a second tool automating the manual
calibration procedure from the vendor's `ADJUST.TXT`.

## Status

Reverse-engineered by disassembly of `QLOADER.EXE` and the instrument's own
Z180 firmware, by live serial capture against a real THM565 (serial
B090062, including one complete, successful real flash from firmware 0.54
to 2.0), and validated against a from-scratch software emulation of the
instrument's Z180 board that reproduces those real captures byte-for-byte.
The full writeups - including exactly what's confirmed from real data,
confirmed by emulation, or still only inferred from disassembly - live in
the private `THM500/` reference folder alongside the original vendor files
(now adapted for publication): `PROTOCOL.md` (the PC-side
protocol), `docs/FIRMWARE-NOTES.md` (the instrument's firmware, static
analysis), and `FIRMWARE-EMULATION.md` (the emulator build and the
differential tests against QLOADER this tool's correctness rests on).

Six defects were found in this package by that emulation testing, fixed,
and re-verified to produce byte-identical final flash contents to QLOADER
across two full scenarios (config+cal only, and a full reload+config+cal on
firmware that can still be erased) - see `FIRMWARE-EMULATION.md` section 5
for each one. Nothing here has been re-verified against real hardware since
those fixes.

## IMPORTANT: firmware 2.0 cannot be reflashed over serial at all

This was confirmed both by static disassembly and by running the real
firmware in emulation: **on a THM565 already running firmware 2.0 (as this
project's own test instrument now is), `$RELOAD CODE` cannot change the
flash contents, no matter what sends it.** The 2.0 firmware's reload
handler downloads S-records into RAM and reboots into the *unchanged* flash
- it never issues an erase or program command. QLOADER's on-screen
"success" and the reported SW version cannot detect this either; a full
session that looks completely normal can produce a final flash image
byte-for-byte identical to one where the code was never sent at all.

`flash.py`'s `reload_code()` now detects this specific case (an 0x01 ack on
the very first, S0, line) and stops immediately rather than streaming the
whole file for nothing. There is no known way to update this instrument's
firmware except removing the flash chip or reprogramming it in-circuit.
(One theoretical, untried, high-risk route exists - a custom RAM-resident
image with its own erase/program code, delivered the same way - see
`FIRMWARE-EMULATION.md` section 7. It has never been attempted and should
only ever be tried in emulation first.)

Older firmware (0.54, and the V1_04/AQ103 images this repo can also send)
does erase and reprogram correctly - the historical real flash of this
exact instrument went through that path before it was upgraded to 2.0.

## Hardware required

- RS-232 connection to the instrument (1200 baud 8N1 for most commands; the
  firmware-reload phase switches to 9600 7E1 mid-session and back).
- The 067-1446-99 test fixture / adapter board, including its VPP switch.
  **VPP gates flash writes, and gates them per-step, not once for the whole
  session** - `$RELOAD CODE`, `$BURN CONFIG`, and `$BURN CAL` each need it
  on independently. `flash.py` prompts for VPP before every gated step
  specifically because a single up-front prompt was tried in practice and
  got this wrong.

  Getting VPP wrong is a real risk, not just an inconvenience. On
  erase-capable firmware (0.54/V1.04-class), an interrupted or mistimed
  `$RELOAD CODE` can leave the flash chip fully erased and blank - this was
  found as an actual defect in an earlier version of `reload_code()`
  (defect 3, now fixed) via emulation, not observed on real hardware. On
  firmware that has already been reflashed, VPP off during `$BURN CONFIG`/
  `$BURN CAL` fails cleanly (the earlier "corrupted THM??? model name"
  incident on this project's instrument was a clean write-once-slot
  rejection, not a garbled write - see `docs/FIRMWARE-NOTES.md`) - but the
  config slot can then never be programmed again without a flash erase, and
  2.0 firmware has no way to erase itself.

## Hardware and community resources

Plans and a bill of materials for the THM565 itself, and for the
067-1446-99 test fixture this repo's flashing and calibration commands
depend on, were shared on EEVblog by user TERRA Operative:
<https://www.eevblog.com/forum/testgear/tektronix-thm56x-portable-scope-hackteardowndiscussion/msg6265840/#msg6265840>

None of the flashing or testing behind this repo would have been possible
without that fixture. Thanks to TERRA Operative for posting it.

## Tools

- `thm565tools/flash.py` - firmware reload, config burn, cal load/burn.
- `thm565tools/calibrate.py` - the ADJUST.TXT DMM/scope calibration sequence.
- `thm565tools/protocol.py` - shared serial framing, checksum, timeouts, and
  cal-block codec used by both.

```
pip install -r requirements.txt
python -m thm565tools.flash --port COM21 --rom v2_0.rom --serial B090062 --sw-version 2.0
python -m thm565tools.calibrate --port COM21
```

## What's confirmed vs. inferred

Confirmed against real captured traffic and reproduced in emulation: cal
dump/load/burn block format and checksum, the `$BURN CONFIG` identity
format (exactly 36 raw bytes, no line terminator), the baud-switch
choreography around `$RELOAD CODE`, the per-line S-record transfer framing
(accept byte, one ack byte per line, the EOT exchange), and realistic reply
timing for every command this tool sends (see `protocol.py`'s timeout
constants).

Confirmed by emulation, not yet by a live capture: that 2.0 firmware's
`$RELOAD CODE` cannot write flash under any input (the historical real
capture only ever exercised the erase-capable 0.54 loader); that fixing the
six known defects makes this tool's output byte-identical to QLOADER's on
two full scenarios.

Still open: real per-byte program-pulse timing and real erase timing on
actual flash silicon (the emulator's flash model is not chip-accurate
timing-wise, only command-accurate); the exact behavior of a line-ack error
mid-transfer on real hardware (never observed live - the emulator's version
of this is a model, not a measurement); the DMM/scope analog calibration
steps in `calibrate.py`, which need the instrument's analog front end and
were not exercisable in software emulation; and the meaning of the cal
block's tag byte beyond "slot state" (0xFF blank / 0xC3 live / 0x00
retired - established, but not why 0xC3 specifically).

The instrument supports a much larger serial command set than this tool
uses (scope/DMM measurement and configuration, clock/date, backlight, key
lock and injection, screen/setting/waveform upload-download, print) - the
full 60-command table this tool's own subset was found inside is documented
in `docs/FIRMWARE-NOTES.md` in docs/FIRMWARE-NOTES.md,
since this package only implements the firmware/config/cal subset.
