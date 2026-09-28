"""Firmware reload, identity (config) burn, and cal load/burn for THM5xx.

IMPORTANT - VPP is per-step, not once for the whole sequence:
`$RELOAD CODE`, `$BURN CONFIG`, and `$BURN CAL` are three INDEPENDENTLY
VPP-gated flash writes. A real session on a THM565 (serial B090062) left
VPP off for the config/cal steps after a successful VPP-on code reload: the
code write took, and $BURN CONFIG and $BURN CAL failed outright (` 1`), which
left the freshly erased config slot blank (the instrument then reports
`THM???`). This module deliberately prompts for VPP before every gated step
rather than once at the start, to avoid repeating that mistake. See
PROTOCOL.md and FIRMWARE-SERIAL-FLASH.md.

The config slot can be programmed ONCE per flash erase, and only a code
reload by a loader that erases flash resets it. A wrong identity burned
there stays there.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .protocol import THM565Serial, CalBlock, DUMP_TIMEOUT, BURN_TIMEOUT, INIT_TIMEOUT

# $RELOAD CODE handshake, from the one real, complete transfer (0.54 -> 2.0):
# one 0x01 accept after the loader has erased the flash (11.7 s on the real
# instrument), one 0x02 per S-record line (8065 of them, S0 and S9 included),
# and 0x04 in reply to the EOT byte. The loader then waits for 0x05 and turns
# the instrument off.
ACCEPT_BYTE = 0x01
LINE_ACK_BYTE = 0x02
EOT_BYTE = 0x04
EOT_REPLY_BYTE = 0x04
POST_VPP_OFF_BYTE = 0x05

# The loader answers the accept only after erasing the whole chip.
ACCEPT_TIMEOUT = 120.0
# The firmware reprograms its UART ~0.2 s after acknowledging BAUD 3.
BAUD_SWITCH_SETTLE = 1.0

# $BURN CONFIG identity: exactly 36 bytes, in the layout QLOADER sends and
# the firmware reads back with $DUMP CAL 2? - model (10), serial (10), "SW ",
# version (5), NUL, 7 spaces. Checked against the bytes QLOADER sent in a
# real session: b"THM565    B090062   SW 2.0  \x00       ".
IDENTITY_LEN = 36


def confirm_vpp(state: str) -> None:
    """Block until the operator confirms the physical VPP switch state.

    Software cannot sense the switch - this exists specifically so each
    gated step gets its own explicit checkpoint instead of one assumption
    made at the start of a long sequence.
    """
    input(f"\n>>> Set the adapter board VPP switch {state.upper()}, then press ENTER. ")


def dump_cal(link: THM565Serial) -> dict[str, CalBlock]:
    """Read back COMMENT/DMM/SCOPE cal blocks. No VPP needed - read only."""
    comment = link.command("$DUMP CAL 2?", timeout=DUMP_TIMEOUT)
    dmm = CalBlock.decode(link.command("$DUMP CAL 0?", timeout=DUMP_TIMEOUT))
    scope = CalBlock.decode(link.command("$DUMP CAL 1?", timeout=DUMP_TIMEOUT))
    return {"comment": comment, "dmm": dmm, "scope": scope}


def backup_cal(link: THM565Serial, path: Path) -> dict[str, CalBlock]:
    blocks = dump_cal(link)
    text = (
        f"COMMENT >{blocks['comment']}<\n"
        f"DMM     >{blocks['dmm'].encode()}<\n"
        f"SCOPE   >{blocks['scope'].encode()}<\n"
    )
    path.write_text(text, encoding="ascii")
    print(f"Cal backup written to {path}")
    return blocks


def reload_code(link: THM565Serial, rom_path: Path) -> None:
    """Stream a Motorola S-record ROM image. Requires VPP on for the duration.

    Sequence (QLOADER's, confirmed against the real transfer): BAUD 3 and its
    ack at 1200 8N1; switch to 9600 7E1; $RELOAD CODE; wait for the 0x01
    accept, which the loader sends only after erasing the flash; one line,
    one 0x02 ack; EOT, answered 0x04; VPP off; 0x05, after which the
    instrument turns itself off.

    Any ack other than 0x02 stops the transfer. For the loader in 0.54/V1.04
    firmware a mid-stream 0x01 means it has erased the flash again and
    restarted, so continuing would leave a partial image. The loader in V2.0
    firmware answers 0x01 to every non-S1 record (the S0 header is the very
    first line) and has no flash erase or program path at all - it copies S1
    records to RAM and reboots into the unchanged flash - so it cannot be
    used to reflash; see FIRMWARE-SERIAL-FLASH.md.
    """
    confirm_vpp("ON")
    link.command("BAUD 3")  # ack is read at the OLD baud, THEN switch - order matters
    link.switch_to_code_transfer_baud()
    time.sleep(BAUD_SWITCH_SETTLE)

    link.send_command("$RELOAD CODE")
    old_timeout = link.ser.timeout
    link.ser.timeout = ACCEPT_TIMEOUT
    try:
        accept = link.ser.read(1)
    finally:
        link.ser.timeout = old_timeout
    if not accept or accept[0] != ACCEPT_BYTE:
        raise RuntimeError(f"instrument did not accept $RELOAD CODE (got {accept!r})")

    lines = rom_path.read_text(encoding="ascii").splitlines()
    print(f"Streaming {len(lines)} S-record lines...")
    for i, line in enumerate(lines):
        link.ser.write(line.encode("ascii") + b"\r")
        ack = link.ser.read(1)
        if not ack:
            raise TimeoutError(f"no ack for line {i} ({line[:16]}...)")
        if ack[0] != LINE_ACK_BYTE:
            if ack[0] == ACCEPT_BYTE and i == 0:
                raise RuntimeError(
                    "the loader answered 0x01 to the S0 header: this is the V2.0-style RAM loader, "
                    "which cannot erase or program flash. Nothing has been written; power-cycle "
                    "the instrument to leave the loader.")
            if ack[0] == ACCEPT_BYTE:
                raise RuntimeError(
                    f"loader restarted (0x01) at line {i}: it has erased the flash again. "
                    f"Do not continue from here - rerun the whole transfer.")
            raise RuntimeError(f"unexpected ack 0x{ack[0]:02x} for line {i}: {line}")
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(lines)}")

    link.ser.write(bytes([EOT_BYTE]))
    eot = link.ser.read(1)
    if not eot or eot[0] != EOT_REPLY_BYTE:
        raise RuntimeError(f"no 0x04 reply to EOT (got {eot!r})")
    confirm_vpp("OFF")
    link.ser.write(bytes([POST_VPP_OFF_BYTE]))
    link.ser.flush()
    link.switch_to_default_baud()
    link.ser.reset_input_buffer()
    input("\n>>> The instrument turns itself off after a reload. Turn it back ON, then press ENTER. ")
    banner = link.read_line(timeout=INIT_TIMEOUT)
    print(f"Code reload complete. Instrument reports: {banner.strip()}")


def burn_config(link: THM565Serial, type_digit: int, model: str, serial_no: str, sw_version: str) -> None:
    """Write the identity string. Requires VPP on - see module docstring.

    Sent as two separate writes with a delay in between (`$BURN CONFIG\\r`,
    wait ~1s, then the 38-byte payload with no terminator) - NOT one
    concatenated write. See THM565Serial.command_with_payload.
    """
    payload = config_payload(type_digit, model, serial_no, sw_version)
    confirm_vpp("ON")
    reply = link.command_with_payload("$BURN CONFIG", payload, delay_s=1.0, timeout=BURN_TIMEOUT)
    if not link.status_ok(reply):
        raise RuntimeError(f"$BURN CONFIG failed: {reply!r} (VPP off, or the config slot is already "
                           f"programmed - it can be written once per flash erase)")
    link.init()
    print("Config burn complete.")


def config_payload(type_digit: int, model: str, serial_no: str, sw_version: str) -> bytes:
    """2 type digits + the 36-byte identity, byte-for-byte as QLOADER builds it.

    The firmware reads exactly 2 + 36 characters with no timeout, so a short
    payload leaves it waiting and the next bytes sent - typically the next
    command - complete the identity and get burned into the write-once
    config slot.
    """
    if not 0 <= type_digit <= 99:
        raise ValueError("type must be 0-99")
    for name, value, width in (("model", model, 10), ("serial", serial_no, 10), ("sw version", sw_version, 5)):
        if len(value) > width:
            raise ValueError(f"{name} {value!r} is longer than its {width}-character field")
    identity = f"{model:<10}{serial_no:<10}SW {sw_version:<5}\x00{' ' * 7}".encode("ascii")
    assert len(identity) == IDENTITY_LEN
    return f"{type_digit:02d}".encode("ascii") + identity


def load_and_burn_cal(link: THM565Serial, blocks: dict[str, CalBlock]) -> None:
    """Restore previously-backed-up cal constants. Requires VPP on.

    Each $LOAD CAL n is also a two-write, delayed sequence (~2s), not a
    concatenated command+payload write.

    $LOAD CAL answers a bare CR whatever happened (the firmware does no
    checksum test; on a hex error it silently re-reads the record from
    flash), so the reply carries no status. Each block is therefore read
    back with $DUMP CAL and compared before anything is burned.
    """
    confirm_vpp("ON")
    for n, key in ((0, "dmm"), (1, "scope")):
        reply = link.command_with_payload(f"$LOAD CAL {n}", blocks[key].encode(), delay_s=2.0)
        if reply.strip():
            raise RuntimeError(f"$LOAD CAL {n}: expected a bare CR, got {reply!r}")
        readback = link.command(f"$DUMP CAL {n}?", timeout=DUMP_TIMEOUT)
        if CalBlock.decode(readback).payload != blocks[key].payload:
            raise RuntimeError(f"$LOAD CAL {n}: read-back does not match what was sent; not burning")
    reply = link.command("$BURN CAL", timeout=BURN_TIMEOUT)
    print(f"Cal load/burn complete ({reply.strip() or 'ok'}).")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--rom", type=Path, required=True)
    ap.add_argument("--type-digit", type=int, default=3, help="THM565 = 3 (see DEFAULT.TYP)")
    ap.add_argument("--model", default="THM565")
    ap.add_argument("--serial", required=True)
    ap.add_argument("--sw-version", required=True)
    ap.add_argument("--backup", type=Path, default=Path("cal_backup.cal"))
    ap.add_argument("--skip-code", action="store_true", help="only redo config+cal burn")
    args = ap.parse_args(argv)

    with THM565Serial(args.port) as link:
        link.init()
        blocks = backup_cal(link, args.backup)

        if not args.skip_code:
            reload_code(link, args.rom)
            link.init()

        burn_config(link, args.type_digit, args.model, args.serial, args.sw_version)
        load_and_burn_cal(link, blocks)

        final = link.init()
        print(f"Final state: {final}")


if __name__ == "__main__":
    sys.exit(main())
