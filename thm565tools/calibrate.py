"""Automates the ADJUST.TXT manual calibration procedure over serial.

This replaces the ProComm-terminal-plus-manual-reading workflow in
ADJUST.TXT with scripted prompts, but it cannot replace the physical
actions ADJUST.TXT calls for: setting the calibration generator, reading
the instrument's own front-panel display to verify a value, and (for scope
gain cal) alternating the generator's polarity in response to the
instrument's '+'/'-' prompts. Those remain operator steps; this module
handles the serial side and tells the operator what to do at each one.

Two manual, non-serial steps from ADJUST.TXT are NOT automated at all and
must be done by the operator before running this:
  - Section II.B: front-panel Menu -> UTILITY -> TIME OUTS -> POWER OFF ->
    NEVER (no serial command for this).
  - Physical hookup of the calibration generator and 067-1446-99 fixture.
"""
from __future__ import annotations

import argparse
import sys

from .protocol import THM565Serial

# DC volts: (dmm mode, target value to set the generator to, label)
DC_VOLTS_STEPS = [
    (0, "400 mV DC", "0.400"),
    (1, "4 V DC", "4.000"),
    (2, "40 V DC", "40.00"),
    (3, "400 V DC", "400.0"),
    (4, "850 V DC", "850"),
]

AC_VOLTS_STEPS = [
    (5, "400 mV AC at 60 Hz", "0.400"),
    (6, "4 V AC", "4.000"),
    (7, "40 V AC", "40.00"),
    (8, "400 V AC", "400.0"),
    (9, "600 V AC", "600"),
]

RESISTANCE_STEPS = [
    (10, "100 ohms", "100"),
    (11, "1 K ohms", "1000"),
    (12, "10 K ohms", "10000"),
    (13, "100 K ohms", "100000"),
    (14, "1 M ohms", "1000000"),
    (15, "10 M ohms", "10000000"),
]

# Scope V/D table (ADJUST.TXT Table 1): (v/d index, v/d label, cal voltage, start code)
SCOPE_VD_TABLE = [
    (0, "5 mV", "20 mV", "00EF"),
    (1, "10 mV", "40 mV", "00C2"),
    (2, "20 mV", "80 mV", "006B"),
    (3, "50 mV", "200 mV", "00A0"),
    (4, "100 mV", "400 mV", "0026"),
    (5, "200 mV", "800 mV", "00C2"),
    (6, "500 mV", "2 V", "00DE"),
    (7, "1 V", "4 V", "00A0"),
    (8, "2 V", "8 V", "0026"),
    (9, "5 V", "20 V", "00C6"),
    (10, "10 V", "40 V", "0071"),
    (11, "20 V", "80 V", "00B8"),
    (12, "50 V", "200 V", "0030"),
    (13, "100 V", "400 V", "00C6"),
    (14, "200 V", "800 V", "0071"),
    (15, "500 V", "1000 V", "00A5"),
]


def check_cals_remaining(link: THM565Serial) -> tuple[int, int]:
    # `$TEST n?` answers the bare number of free slots (`10\r`), with no
    # ` 0` status field. 0 = SCOPE, 1 = DMM (FIRMWARE-SERIAL-FLASH.md,
    # `$TEST #?`; confirmed in emulation: one used DMM slot -> `$TEST 1?` = 9).
    scope = int(link.query("$TEST 0?"))
    dmm = int(link.query("$TEST 1?"))
    print(f"Cals remaining - DMM: {dmm}, SCOPE: {scope} (max 10 each; reload firmware to reset)")
    return dmm, scope


def dmm_calibrate_step(link: THM565Serial, mode: int, target_label: str) -> None:
    link.command(f"dmm mode {mode}")
    input(f"\n>>> Set the calibration generator to {target_label}, then press ENTER. ")
    link.command("$DMM CALIBRATE")
    print(f"  dmm mode {mode} ({target_label}): cal passed")


def dmm_calibrate_all(link: THM565Serial) -> None:
    print("\n=== DC VOLTS ===")
    for mode, label, _value in DC_VOLTS_STEPS:
        dmm_calibrate_step(link, mode, label)
    print("\n=== AC VOLTS ===")
    for mode, label, _value in AC_VOLTS_STEPS:
        dmm_calibrate_step(link, mode, label)
    print("\n=== RESISTANCE ===")
    for mode, label, _value in RESISTANCE_STEPS:
        dmm_calibrate_step(link, mode, label)


def scope_offset_calibrate(link: THM565Serial, channel: int) -> None:
    input(f"\n>>> Set the calibration generator to 0 V DC on scope channel {channel}, then press ENTER. ")
    link.command(f"$scope ch{channel} calibrate offset")
    print(f"  channel {channel} offset cal passed")


def scope_gain_calibrate(link: THM565Serial, channel: int) -> None:
    """Table 1 gain cal: alternate generator polarity until the instrument
    reports a final "0 <code>" result for each V/D range. The instrument
    outputs '+' when it wants the NEGATIVE input next and '-' when it wants
    the POSITIVE input next (see ADJUST.TXT IV.A.9) - this loop reads that
    prompt and tells the operator which way to flip the generator.
    """
    for vd_index, vd_label, voltage_label, start_code in SCOPE_VD_TABLE:
        link.command(f"scope ch{channel} v/d {vd_index}")
        input(
            f"\n>>> V/D {vd_label}: set the generator to +{voltage_label} DC on channel {channel}, "
            f"then press ENTER. "
        )
        link.send_command(f"$scope ch{channel} calibrate gain")
        link.send_command(start_code)
        polarity = "+"
        print(f"  waiting for gain cal to settle on V/D {vd_label}...")
        while True:
            resp = link.read_line(timeout=15)
            resp = resp.strip()
            if resp in ("+", "-"):
                wanted = "negative" if resp == "+" else "positive"
                input(f"    instrument wants {wanted} input now - flip the generator, then press ENTER. ")
                continue
            if resp.startswith("0"):
                print(f"  V/D {vd_label}: cal passed ({resp})")
                break
            if resp.startswith("1"):
                print(f"  V/D {vd_label}: cal FAILED - repeat this range")
                break
            print(f"  unexpected response {resp!r}, stopping this range")
            break


def calibrate_scope_channel(link: THM565Serial, channel: int) -> None:
    print(f"\n=== SCOPE CHANNEL {channel} ===")
    scope_offset_calibrate(link, channel)
    scope_gain_calibrate(link, channel)
    input(f"\n>>> Disable the 1000V generator output and disconnect from channel {channel}, then press ENTER. ")
    scope_offset_calibrate(link, channel)


def burn_cal(link: THM565Serial) -> None:
    input("\n>>> Enable the 12 volts on the 067-1446-99 test fixture, then press ENTER. ")
    link.command("$BURN CAL")
    print("Calibration constants burned to flash.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True)
    ap.add_argument("--channels", type=int, default=1, help="1 or 2 scope channels")
    ap.add_argument("--skip-dmm", action="store_true")
    ap.add_argument("--skip-scope", action="store_true")
    args = ap.parse_args(argv)

    with THM565Serial(args.port) as link:
        link.init()
        print(
            "Before continuing: confirm the front-panel Menu -> UTILITY -> "
            "TIME OUTS -> POWER OFF is set to NEVER (ADJUST.TXT II.B - no "
            "serial command for this)."
        )
        input("Press ENTER once confirmed. ")

        dmm_remaining, scope_remaining = check_cals_remaining(link)
        if dmm_remaining <= 0 or scope_remaining <= 0:
            print("No cal cycles remaining - firmware must be reloaded to reset the counter.")
            return 1

        if not args.skip_dmm:
            dmm_calibrate_all(link)
        if not args.skip_scope:
            calibrate_scope_channel(link, 1)
            if args.channels == 2:
                calibrate_scope_channel(link, 2)

        burn_cal(link)


if __name__ == "__main__":
    sys.exit(main())
