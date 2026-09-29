# THM565 flash and calibration tools

*Contributed by Cyclotron.*

A native reimplementation of the RS-232 protocol Tektronix's 1994
`QLOADER.EXE` uses to reload firmware, burn the instrument identity, and
load and burn calibration constants on THM5xx "TekMeter" instruments, plus
a second tool that automates the manual calibration procedure from the
vendor's `ADJUST.TXT`. QLOADER only runs under DOS. I wanted the same
operations from a normal Windows or Linux machine, without DOSBox.

## How I got the protocol

I started from `QLOADER.EXE` itself, disassembling the x86 code to read
off the command strings, the S-record transfer framing, and the cal block
checksum. I checked each piece against a real serial capture from a THM565,
serial B090062, including one complete flash from firmware 0.54 to 2.0.
That gave me a working PC-side picture, but not the instrument's side of
the exchange: what the firmware does with each byte it receives, and
whether the flash actually gets written.

The instrument's CPU turned out to be a Z180, not the Z80 I assumed at
first from the strings alone. I built a software emulator of the THM565's
Z180 board (Z180 core, MMU banking, the ASCI UART, the flash chip's
command state machine) and ran the real `QLOADER.EXE` and this
reimplementation against it side by side, comparing the resulting flash
image byte for byte instead of trusting either program's own success
message. `docs/PROTOCOL.md` has the PC-side protocol in full and
`docs/FIRMWARE-NOTES.md` has the instrument firmware side; the emulator
itself stays in the private `THM500/` reference folder, since its core is
a GPL-licensed Z180 emulator I used to validate this MIT-licensed tool, not
something I'm shipping as part of it. `FIRMWARE-EMULATION.md` there has
the emulator build and the differential tests this tool's correctness
rests on.

`tests/` has a start on an MIT-clean regression suite that replays the
real wire transcripts from those differential tests instead of depending
on the emulator: `tests/runs/*/wire.txt` are the recorded exchanges, and
`tests/fake_serial.py` is meant to play one back as a drop-in pyserial
replacement. It isn't finished. The transcript parser is a stub and the
fake port doesn't yet feed it anything, so `tests/test_replay.py` only
checks that the transcript files exist, not that this tool's behavior
matches them.

## Firmware 2.0 cannot be reflashed over serial

This instrument, once it was updated to firmware 2.0, cannot be reflashed
over serial again by any tool, including QLOADER itself. I confirmed it
two ways: reading the 2.0 firmware's `$RELOAD CODE` handler in the
disassembly, and then watching it run in the emulator. It downloads the
S-record image into RAM and reboots into the unchanged flash. It never
issues an erase or a program command. QLOADER does not check the per-line
acknowledgement bytes it gets back, so a full reload session looks
completely normal on screen and reports the new version string, while the
flash contents never change.

`flash.py`'s `reload_code()` now catches this itself (the loader answers
its first line with an accept byte instead of a line acknowledgement) and
stops before streaming the rest of the file for nothing. I don't have a
way to update this instrument's firmware short of removing the flash chip
or reprogramming it in circuit. There is one route neither the
disassembly nor the emulator rules out: a custom RAM-resident image
carrying its own erase and program code, since the 2.0 loader will run
whatever S-records it is given. I have not built or tried that, and it
should only be tried in the emulator first.

Older firmware (0.54, and the V1_04 and AQ103 images this repo can also
send) erases and reprograms correctly. The real flash of this instrument
went through that path before it was updated to 2.0.

## Six defects, found by the emulator

I ran the original version of this package against the emulator before
changing anything, specifically to see whether it would reproduce known
problems rather than just to confirm it worked. It found six, all now
fixed and re-verified to produce byte-identical flash contents to QLOADER
on two full scenarios (a config-and-cal-only run, and a full reload plus
config plus cal on firmware that can still erase). None of this has been
re-verified against real hardware since the fixes; the emulator is the
only thing that has exercised this code since.

| # | Defect | Consequence |
| --- | --- | --- |
| 1 | `$BURN CONFIG` identity was 32 characters; the firmware reads exactly 36 with no timeout | Whatever came next on the wire completed the identity and got burned into the write-once config slot |
| 2 | `$LOAD CAL` success check expected a trailing `0`; the real reply is a bare CR | Every successful cal load was reported as a failure |
| 3 | `reload_code()` sent `$RELOAD CODE` before `BAUD 3`, and used a 2 s timeout for the accept byte | On erase-capable firmware, the tool gave up mid-erase and left the flash entirely blank |
| 4 | `INIT`'s reply (the reboot banner) arrives about 5.7 s later on real hardware | The tool failed on its very first command |
| 5 | Several other replies (`$DUMP CAL 1?`, a VPP-off burn) also exceed a 2 s timeout | Same kind of spurious timeout as #4 |
| 6 | A trailing CR after payload writes drew an extra, unexpected reply | The next command would read that stray reply as its own |

`calibrate.py` had two more, caught the same way: `$TEST n?` answers a
bare number with no status field, which the old code parsed as a status
line, and the DMM and SCOPE indices were swapped.

## Hardware needed

An RS-232 connection to the instrument, 1200 baud 8N1 for most commands.
The firmware-reload phase switches to 9600 7E1 mid-session and back on its
own.

You also need the 067-1446-99 test fixture, including its VPP switch. VPP
gates flash writes, and gates them per step, not once for the whole
session: `$RELOAD CODE`, `$BURN CONFIG`, and `$BURN CAL` each need it on
independently. `flash.py` prompts for VPP before every gated step rather
than once at the start, because I tried the single up-front prompt first
and got it wrong (defect 3 above).

Getting VPP wrong is a real risk, not just an inconvenience. On
erase-capable firmware, an interrupted or mistimed `$RELOAD CODE` can
leave the flash chip fully erased and blank. On firmware that has already
been reflashed, VPP off during `$BURN CONFIG` or `$BURN CAL` fails
cleanly (see `docs/FIRMWARE-NOTES.md` for the "THM???" identity string
this instrument showed after one such clean failure, which turned out to
be a blank config slot, not a garbled write), but the config slot can then
never be programmed again without a flash erase, and 2.0 firmware has no
way to erase itself.

Plans and a bill of materials for the THM565 itself, and for the
067-1446-99 fixture, were shared on EEVblog by user TERRA Operative:
<https://www.eevblog.com/forum/testgear/tektronix-thm56x-portable-scope-hackteardowndiscussion/msg6265840/#msg6265840>.
None of the work above would have been possible without that fixture.
Thanks to TERRA Operative for posting it.

## Tools

- `thm565tools/flash.py`: firmware reload, config burn, cal load and burn.
- `thm565tools/calibrate.py`: the `ADJUST.TXT` DMM and scope calibration
  sequence.
- `thm565tools/protocol.py`: shared serial framing, checksum, timeouts,
  and the cal-block codec used by both.

```
pip install -r requirements.txt
python -m thm565tools.flash --port COM21 --rom v2_0.rom --serial B090062 --sw-version 2.0
python -m thm565tools.calibrate --port COM21
```

## What I've confirmed and what I haven't

Confirmed against real captured traffic and reproduced in the emulator:
the cal dump, load and burn block format and checksum, the `$BURN CONFIG`
identity format (36 raw bytes, no line terminator), the baud switch around
`$RELOAD CODE`, the per-line S-record transfer framing, and realistic
reply timing for every command this tool sends.

Confirmed by emulation but not yet by a live capture: that 2.0 firmware's
`$RELOAD CODE` cannot write flash under any input (the historical real
capture only ever exercised the erase-capable 0.54 loader), and that the
six fixes above make this tool's output byte-identical to QLOADER's.

Still open: real per-byte program-pulse and erase timing on actual flash
silicon (the emulator's flash model matches the commands, not the
timing), how a line-acknowledgement error mid-transfer behaves on real
hardware, the DMM and scope analog calibration steps in `calibrate.py`
(these need the instrument's analog front end, which the emulator does not
model), and why the cal block's tag byte uses the specific values it does
(0xFF blank, 0xC3 live, 0x00 retired are established; why 0xC3 rather than
some other value is not).

The instrument answers a much larger serial command set than this tool
uses: scope and DMM measurement and configuration, clock and date,
backlight, key lock and injection, screen, setting and waveform upload and
download, print. The full 60-command table this tool's subset was found
inside is in `docs/FIRMWARE-NOTES.md`, since this package only implements
the firmware, config and cal commands.
