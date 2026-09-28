"""Low-level THM5xx serial protocol: framing, checksums, and cal-block codec.

Everything in this module is validated against real capture data from a
THM565 (serial B090062), not just the QLOADER.EXE disassembly - see
PROTOCOL.md in the THM500/ reference folder for how each piece was derived.
"""
from __future__ import annotations

import dataclasses
import time

import serial

DEFAULT_BAUD = 1200
DEFAULT_BYTESIZE = serial.EIGHTBITS
DEFAULT_PARITY = serial.PARITY_NONE
DEFAULT_STOPBITS = serial.STOPBITS_ONE

CODE_TRANSFER_BAUD = 9600
CODE_TRANSFER_BYTESIZE = serial.SEVENBITS
CODE_TRANSFER_PARITY = serial.PARITY_EVEN
CODE_TRANSFER_STOPBITS = serial.STOPBITS_ONE

CR = b"\r"

# Reply deadlines, measured on a real THM565 (1200 baud, ~8.3 ms per byte).
# A single 2 s default was too short for several commands:
#   INIT restarts the firmware; its banner arrives ~5.7 s later.
#   $DUMP CAL 1? returns ~393 characters, ~3.3 s on the wire.
#   $BURN CONFIG / $BURN CAL with VPP off give up after 100 program pulses
#   per byte and answer " 1" 3-5 s later.
DEFAULT_TIMEOUT = 2.0
INIT_TIMEOUT = 15.0
DUMP_TIMEOUT = 10.0
BURN_TIMEOUT = 30.0


def cal_checksum(payload: bytes) -> int:
    """The cal-block checksum: XOR of every payload byte, seeded with 0x5A.

    Confirmed against two independent real cal blocks (DMM and SCOPE) pulled
    live from the instrument - both checksum and its bitwise complement
    matched exactly, not just the degenerate all-1.0 DEFAULT.CAL sample.
    """
    checksum = 0x5A
    for b in payload:
        checksum ^= b
    return checksum


@dataclasses.dataclass
class CalBlock:
    """One DMM or SCOPE calibration block.

    Wire/file format (hex-ASCII, `>`...`<` delimited): a leading tag byte
    whose meaning is not yet identified (always seen as 0xC3 on this
    instrument), then the checksum, then its bitwise complement, then the
    payload (IEEE-754 little-endian floats: gain/offset constants).
    """

    tag: int
    payload: bytes

    @classmethod
    def decode(cls, hex_text: str) -> "CalBlock":
        raw = bytes.fromhex("".join(hex_text.split()))
        tag, checksum, complement = raw[0], raw[1], raw[2]
        payload = raw[3:]
        computed = cal_checksum(payload)
        if computed != checksum:
            raise ValueError(f"cal block checksum mismatch: expected 0x{checksum:02x}, got 0x{computed:02x}")
        if (~computed) & 0xFF != complement:
            raise ValueError(
                f"cal block complement mismatch: expected 0x{complement:02x}, got 0x{(~computed) & 0xFF:02x}"
            )
        return cls(tag=tag, payload=payload)

    def encode(self) -> str:
        checksum = cal_checksum(self.payload)
        complement = (~checksum) & 0xFF
        raw = bytes([self.tag, checksum, complement]) + self.payload
        return raw.hex().upper()


class THM565Serial:
    """Thin wrapper around the RS-232 link: line framing and baud switching.

    Not a general-purpose serial helper - it exists to keep the exact framing
    conventions (CR-terminated commands, single-byte acks, the mid-session
    baud/parity change for code transfer) in one place rather than repeated
    across flash.py and calibrate.py.
    """

    def __init__(self, port: str, timeout: float = DEFAULT_TIMEOUT):
        self.ser = serial.Serial(
            port=port,
            baudrate=DEFAULT_BAUD,
            bytesize=DEFAULT_BYTESIZE,
            parity=DEFAULT_PARITY,
            stopbits=DEFAULT_STOPBITS,
            timeout=timeout,
        )

    def close(self):
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send_command(self, text: str) -> None:
        """Send a bare command line, CR-terminated (no trailing space)."""
        self.ser.write(text.encode("ascii") + CR)

    def read_line(self, timeout: float | None = None) -> str:
        """Read one CR-terminated response line as text.

        Most command responses are `<data> 0\\r` on success. Raises
        TimeoutError if no CR arrives within the timeout.
        """
        deadline = time.monotonic() + (timeout if timeout is not None else self.ser.timeout)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(1)
            if not chunk:
                continue
            if chunk == CR:
                return buf.decode("ascii", errors="replace")
            buf += chunk
        raise TimeoutError(f"no response within timeout (got so far: {bytes(buf)!r})")

    def command(self, text: str, timeout: float | None = None) -> str:
        """Send a command and return its response line (without trailing status).

        Convention observed on every command exercised so far: response ends
        in ` 0` on success. Raises RuntimeError if the trailing status isn't
        `0` (i.e. the instrument reported an error), and returns the leading
        text otherwise (for DUMP CAL, that's the hex-ASCII block).
        """
        self.send_command(text)
        line = self.read_line(timeout=timeout)
        if not line.rstrip().endswith("0"):
            raise RuntimeError(f"command {text!r} did not report success: {line!r}")
        return line.rsplit(" ", 1)[0]

    def query(self, text: str, timeout: float | None = None) -> str:
        """Send a command whose reply is a bare value with no status field
        (e.g. `$TEST 0?` answers `10`), and return that value."""
        self.send_command(text)
        return self.read_line(timeout=timeout).strip()

    def init(self) -> str:
        """Send INIT (full firmware restart) and return the power-on banner.

        The banner (`THM... SW VERSION x.xx 0`) only arrives once the
        instrument has rebooted, ~5.7 s after the command on a real THM565.
        """
        return self.command("INIT", timeout=INIT_TIMEOUT)

    def command_with_payload(self, command_text: str, payload: bytes | str, delay_s: float,
                              timeout: float | None = None) -> str:
        """Send a command line, wait `delay_s`, then send a fixed-length payload.

        `$BURN CONFIG` and `$LOAD CAL 0/1` are NOT single concatenated
        writes - disassembly and a real capture both confirm two separate
        writes with a fixed delay between them (measured ~1.0s for BURN
        CONFIG, ~2.0s for LOAD CAL, matching the 0x3e8/0x7d0 millisecond
        constants in QLOADER.EXE).

        The payload is sent WITHOUT a line terminator. The firmware reads it
        as an exact number of characters (2 type digits + 36 identity bytes
        for $BURN CONFIG; 2 hex digits per record byte for $LOAD CAL), not
        as a line, and QLOADER sends none. A trailing CR would reach the
        command reader as an empty command and draw an extra
        `COMMAND NOT DEFINED 0` reply, which would then be mistaken for the
        reply to the next command.

        Returns the raw reply line (without its CR). Interpreting it is the
        caller's job: $BURN CONFIG answers ` 0`/` 1`, $LOAD CAL answers a
        bare CR (an empty line) whether or not the data was usable.
        """
        if isinstance(payload, str):
            payload = payload.encode("latin-1")
        self.send_command(command_text)
        time.sleep(delay_s)
        self.ser.write(payload)
        # write() returns as soon as the bytes are queued; the reply cannot
        # start before the last of them is on the wire (a SCOPE block is
        # 390 characters, 3.25 s at 1200 baud).
        wire_s = len(payload) * 10 / self.ser.baudrate
        return self.read_line(timeout=(timeout if timeout is not None else self.ser.timeout) + wire_s)

    @staticmethod
    def status_ok(reply: str) -> bool:
        """True for a reply whose trailing status field is `0`."""
        parts = reply.rstrip().rsplit(" ", 1)
        return len(parts) == 2 and parts[1] == "0" or reply.strip() == "0"

    def switch_to_code_transfer_baud(self) -> None:
        """Reconfigure the local port for the $RELOAD CODE phase.

        Must be called only after the instrument's `0` ack for `BAUD 3` has
        been read at the OLD baud - switching before that clobbers the ack
        itself (this exact bug was found and fixed in the capture bridge
        used to reverse-engineer this protocol; see PROTOCOL.md).
        """
        self.ser.apply_settings(
            {
                "baudrate": CODE_TRANSFER_BAUD,
                "bytesize": CODE_TRANSFER_BYTESIZE,
                "parity": CODE_TRANSFER_PARITY,
                "stopbits": CODE_TRANSFER_STOPBITS,
            }
        )

    def switch_to_default_baud(self) -> None:
        """Revert to 1200 8N1 after the code-transfer phase."""
        self.ser.apply_settings(
            {
                "baudrate": DEFAULT_BAUD,
                "bytesize": DEFAULT_BYTESIZE,
                "parity": DEFAULT_PARITY,
                "stopbits": DEFAULT_STOPBITS,
            }
        )
