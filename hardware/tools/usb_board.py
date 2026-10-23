# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Talk to the board over USB serial (the board plugged into this computer, Bluetooth only to the phone).

    uv run hardware/tools/usb_board.py status
    uv run hardware/tools/usb_board.py snap --png out/screen.png
    uv run hardware/tools/usb_board.py cmd "move 100 0" click

The board's serial protocol is the BLE `cmd` protocol (docs/ble-protocol.md): "<id> <command>" in, "<id> <reply>"
out. A video frame is one H.264 access unit: a header line "video <flags> <ts_ms> <len>\\n" followed by <len> raw bytes.
Needs ffmpeg to turn a snapshot into a PNG. Before any HID command, make sure `hid` is `ble`: a board plugged into a
computer is also a USB mouse and keyboard to it, and in `auto` mode a command would move this computer's pointer.
"""
import argparse
import glob
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import serial

FLAG_IDR, FLAG_CONFIG = 4, 8


class BoardError(RuntimeError):
    pass


def find_port() -> str:
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        raise BoardError("no board found (looked for /dev/ttyACM* and /dev/cu.usbmodem*)")
    return ports[0]


class UsbBoard:
    def __init__(self, port: str | None = None):
        self.s = serial.Serial(port or find_port(), 115200, timeout=0.2, exclusive=True)  # one owner: web UI or MCP
        self.next_id = 1
        time.sleep(0.3)
        self.s.reset_input_buffer()

    def close(self):
        self.s.close()

    def _readline(self, deadline: float) -> bytes | None:
        buf = b""
        while time.monotonic() < deadline:
            c = self.s.read(1)
            if c == b"\n":
                return buf
            buf += c
        return None

    def _readexact(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n and time.monotonic() < deadline:
            buf += self.s.read(min(n - len(buf), 65536))
        if len(buf) < n:
            raise BoardError(f"video frame cut short ({len(buf)} of {n} bytes)")
        return buf

    def cmd(self, line: str, timeout: float = 10, frames: list | None = None) -> str:
        """Send one command and return the reply text; video frames that arrive meanwhile go to `frames`."""
        rid = str(self.next_id)
        self.next_id += 1
        self.s.write(f"{rid} {line}\n".encode())
        deadline = time.monotonic() + timeout
        while True:
            raw = self._readline(deadline)
            if raw is None:
                raise BoardError(f"no reply to {line!r} within {timeout:.0f} s")
            text = raw.decode("utf-8", "replace").strip()
            if text.startswith("video "):
                try:
                    _, flags, ts, n = text.split()
                    data = self._readexact(int(n), deadline)
                except (ValueError, BoardError):
                    continue  # a cut-off frame: resynchronise on the next header line
                if frames is not None:
                    frames.append((int(flags), int(ts), data))
                continue
            if text.startswith(rid + " ") or text == rid:
                return text[len(rid):].strip()
            # anything else (boot lines, a stale reply) is not ours

    def command(self, line: str, timeout: float = 10) -> str:
        reply = self.cmd(line, timeout)
        if reply.startswith("err"):
            raise BoardError(reply)
        return reply

    def snapshot_h264(self) -> bytes:
        """The current GOP (SPS/PPS + every access unit since the last IDR) as Annex-B."""
        frames: list = []
        reply = self.cmd("snap", timeout=30, frames=frames)
        if reply.startswith("err"):
            raise BoardError(reply)
        if not frames:
            raise BoardError("snap returned no video")
        return b"".join(data for _, _, data in frames)

    def screenshot_png(self, png: Path) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise BoardError("ffmpeg not found")
        with tempfile.TemporaryDirectory() as d:
            h264 = Path(d) / "snap.h264"
            h264.write_bytes(self.snapshot_h264())
            png.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(h264), "-update", "1", str(png)],
                               capture_output=True, text=True)
        if r.returncode or not png.exists():
            raise BoardError(f"ffmpeg failed: {r.stderr.strip()}")
        return png


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the first /dev/ttyACM* or /dev/cu.usbmodem*)")
    sub = ap.add_subparsers(dest="what", required=True)
    sub.add_parser("status")
    sp = sub.add_parser("snap")
    sp.add_argument("--png", type=Path, default=Path("snap.png"))
    cp = sub.add_parser("cmd")
    cp.add_argument("lines", nargs="+")
    a = ap.parse_args()
    try:
        b = UsbBoard(a.port)
        if a.what == "status":
            print(b.command("status"))
        elif a.what == "snap":
            t0 = time.monotonic()
            print(f"wrote {b.screenshot_png(a.png)} in {time.monotonic() - t0:.1f} s")
        else:
            for line in a.lines:
                t0 = time.monotonic()
                print(f"{line} -> {b.cmd(line)} ({(time.monotonic() - t0) * 1000:.0f} ms)")
    except (BoardError, serial.SerialException) as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
