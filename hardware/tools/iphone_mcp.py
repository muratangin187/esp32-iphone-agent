# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "pyserial>=3.5", "psutil>=5.9", "pillow>=10", "numpy>=1.26", "scipy>=1.11"]
# ///
"""MCP server: let an agent see and use a real iPhone from this computer.

    eyes  UxPlay receives the phone's AirPlay screen mirroring (phone_screen.py) and yields JPEG frames
    hands an ESP32 on USB serial (usb_board.py) is the phone's Bluetooth mouse and keyboard

    uv run hardware/tools/iphone_mcp.py             # stdio MCP server
    uv run hardware/tools/iphone_mcp.py --check     # print status, take a screenshot, no MCP

Coordinates are fractions of the screenshot (0..1, x to the right, y downwards). The Bluetooth mouse is relative:
a tap homes the pointer into the top-left corner, moves it with the calibrated scale and clicks. iOS pointer
acceleration depends on speed, so moves are axis-aligned and paced like the calibration.
"""
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone_screen import FileScreen, PhoneScreen  # noqa: E402
from usb_board import BoardError, UsbBoard  # noqa: E402

CAL_FILE = Path(os.environ.get("IPHONE_AGENT_CAL", Path.home() / ".config/iphone-agent/calibration.json"))
# Measured on an iPhone 14 Pro Max at the default AssistiveTouch tracking speed (frame 498x1080): the pointer
# centre sits at about (22, 21) px after `home`, then moves 0.78 px per HID unit on either axis. Stored as
# fractions of the frame so a different UxPlay resolution keeps working.
DEFAULT_CAL = {"x0": 22.6 / 498, "y0": 21.0 / 1080, "kx": 0.7727 / 498, "ky": 0.79 / 1080}
POINTER_PX = (1200, 6000)  # area range of the pointer circle in the 498x1080 frame, scaled with the frame


class Phone:
    def __init__(self, name: str = "iPhone Agent"):
        # By default read the frames of the long-running receiver (phone_screen.py), so the phone's mirroring
        # session survives restarts of this server; IPHONE_AGENT_INPROCESS=1 runs UxPlay inside this process.
        self.screen = (PhoneScreen(name).start() if os.environ.get("IPHONE_AGENT_INPROCESS") else FileScreen())
        self.board: UsbBoard | None = None
        self.lock = threading.RLock()
        self.cal = self._load_cal()

    # ---- setup
    def _load_cal(self) -> dict:
        try:
            return {**DEFAULT_CAL, **json.loads(CAL_FILE.read_text())}
        except (OSError, ValueError):
            return dict(DEFAULT_CAL)

    def _save_cal(self) -> None:
        CAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CAL_FILE.write_text(json.dumps(self.cal, indent=1))

    def _board(self) -> UsbBoard:
        if self.board is None:
            self.board = UsbBoard()
            hid = self.board.cmd("hid")  # a board plugged into a computer must never drive that computer's USB HID
            if hid.startswith("ok") and "ble" not in hid:
                self.board.command("hid ble")
        return self.board

    def _cmd(self, line: str, timeout: float = 15) -> str:
        with self.lock:
            try:
                reply = self._board().cmd(line, timeout)
            except Exception:  # noqa: BLE001  the board may have been replugged
                self.board = None
                reply = self._board().cmd(line, timeout)
        if reply.startswith("err"):
            raise BoardError(f"{line.split()[0]}: {reply}")
        return reply

    # ---- eyes
    def frame(self, after: float = 0.0, timeout: float = 4.0) -> tuple[bytes, np.ndarray]:
        jpg = self.screen.wait_frame(after, timeout)
        if not jpg:
            raise BoardError("no screen: start phone_screen.py, then on the iPhone open Control Center > "
                             "Screen Mirroring > 'iPhone Agent'")
        return jpg, np.asarray(Image.open(io.BytesIO(jpg)).convert("L"), dtype=np.int16)

    def status(self) -> dict:
        try:
            board = json.loads(self._cmd("status"))
        except Exception as e:  # noqa: BLE001
            board = {"error": str(e)}
        jpg = self.screen.frame
        size = Image.open(io.BytesIO(jpg)).size if jpg else None
        return {"mirroring": self.screen.mirroring, "frame_age_s": round(time.time() - self.screen.updated, 1) if jpg else None,
                "frame_size": size, "bluetooth_hid": board.get("hid"), "board": board, "calibration": self.cal}

    # ---- pointer
    def _find_pointer(self, before: np.ndarray, after: np.ndarray, avoid=None):
        """Centre (px) of the pointer circle: the blob that appeared between two frames."""
        h, w = after.shape
        scale = (w / 498) * (h / 1080)
        d = np.abs(before - after) > 28
        d[: int(70 * h / 1080)] = False  # the status bar's clock changes by itself
        d = ndimage.binary_closing(d, iterations=3)
        lab, n = ndimage.label(d)
        best = None
        for i in range(1, n + 1):
            ys, xs = np.nonzero(lab == i)
            if not POINTER_PX[0] * scale <= len(ys) <= POINTER_PX[1] * scale:
                continue
            c = (float(xs.mean()), float(ys.mean()))
            if avoid and np.hypot(c[0] - avoid[0], c[1] - avoid[1]) < 30 * w / 498:
                continue
            if best is None or len(ys) > best[0]:
                best = (len(ys), c)
        return best[1] if best else None

    def _units(self, fx: float, fy: float) -> tuple[int, int]:
        c = self.cal
        return round((fx - c["x0"]) / c["kx"]), round((fy - c["y0"]) / c["ky"])

    def move_to(self, fx: float, fy: float, verify: bool = True) -> dict:
        """Put the pointer at the fractional position; returns the measured error in frame fractions."""
        fx, fy = min(max(fx, 0.03), 0.97), min(max(fy, 0.02), 0.98)
        with self.lock:
            self._cmd("home")
            time.sleep(0.3)
            _, base = self.frame(after=0, timeout=2)  # pointer in the corner: the reference for the diff
            ux, uy = self._units(fx, fy)
            if uy > 0:
                self._cmd(f"move 0 {uy}")
            if ux > 0:
                self._cmd(f"move {ux} 0")
            if not verify:
                return {"verified": False}
            return self._verify(fx, fy, base, last_moves_at=time.time())

    def _verify(self, fx: float, fy: float, base: np.ndarray, last_moves_at: float) -> dict:
        """Find the pointer by diffing against the frame taken right after `home`; nudge it if it is off."""
        prev, last, err = base, None, None
        for attempt in range(3):
            time.sleep(0.25)
            _, now = self.frame(after=last_moves_at, timeout=1.0)
            h, w = now.shape
            p = self._find_pointer(prev, now, avoid=last)
            if p is None:
                return {"verified": False} if err is None else {"verified": True, "error": err}
            ex, ey = fx - p[0] / w, fy - p[1] / h
            err = [round(ex, 3), round(ey, 3)]
            if (abs(ex) < 0.012 and abs(ey) < 0.008) or attempt == 2:
                return {"verified": True, "error": err}
            cx, cy = round(ex / self.cal["kx"]), round(ey / self.cal["ky"])
            prev, last = now, p
            if cy:
                self._cmd(f"move 0 {cy}")
            if cx:
                self._cmd(f"move {cx} 0")
            last_moves_at = time.time()
        return {"verified": True, "error": err}

    def calibrate(self) -> dict:
        """Home, move by known amounts on each axis and locate the pointer by image diff; stores the fit."""
        pts = {"x": [], "y": []}
        with self.lock:
            for axis in ("x", "y"):
                for d in (150, 300, 450):
                    self._cmd("home")
                    time.sleep(0.3)
                    _, home = self.frame(after=time.time(), timeout=2)
                    self._cmd("move 0 250" if axis == "x" else "move 250 0")
                    _, a = self.frame(after=time.time(), timeout=2)
                    p0 = self._find_pointer(home, a)
                    self._cmd(f"move {d} 0" if axis == "x" else f"move 0 {d}")
                    _, b = self.frame(after=time.time(), timeout=2)
                    p1 = self._find_pointer(a, b, avoid=p0)
                    if p1:
                        h, w = b.shape
                        pts[axis].append((d, p1[0] / w if axis == "x" else p1[1] / h))
            self._cmd("home")
        out = {}
        for axis in ("x", "y"):
            if len(pts[axis]) < 3:
                raise BoardError(f"calibration failed on the {axis} axis: pointer not found ({len(pts[axis])} of 3)")
            k, c = np.polyfit([p[0] for p in pts[axis]], [p[1] for p in pts[axis]], 1)
            out["k" + axis], out[axis + "0"] = float(k), float(c)
        self.cal = {**self.cal, **out}
        self._save_cal()
        return self.cal

    # ---- gestures
    def tap(self, fx: float, fy: float, hold_ms: int = 0, verify: bool = True) -> dict:
        with self.lock:
            info = self.move_to(fx, fy, verify)
            if hold_ms > 0:
                self._cmd("press")
                time.sleep(hold_ms / 1000)
                self._cmd("release")
            else:
                self._cmd("click")
        return info

    def swipe(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        with self.lock:
            info = self.move_to(x1, y1, verify=False)
            self._cmd("press")
            time.sleep(0.12)
            u1x, u1y = self._units(x1, y1)
            u2x, u2y = self._units(x2, y2)
            dx, dy = u2x - u1x, u2y - u1y
            # a drag is one paced move; iOS accelerates the pointer with speed, so the landing point is approximate
            if dy:
                self._cmd(f"move 0 {dy}")
            if dx:
                self._cmd(f"move {dx} 0")
            time.sleep(0.12)
            self._cmd("release")
        return info

    def scroll(self, fx: float, fy: float, lines: int) -> None:
        with self.lock:
            self.move_to(fx, fy, verify=False)
            self._cmd(f"scroll {int(lines)} 0")


# ---------------------------------------------------------------- MCP
def serve() -> None:
    from mcp.server.fastmcp import FastMCP, Image as McpImage

    mcp = FastMCP("iphone")
    phone = Phone()

    def shot() -> McpImage:
        time.sleep(0.4)  # let the screen settle after the last action; frames arrive only when it changes
        jpg, _ = phone.frame(after=0, timeout=4)
        return McpImage(data=jpg, format="jpeg")

    @mcp.tool()
    def iphone_status() -> dict:
        """Is the iPhone's screen mirrored (UxPlay) and is the Bluetooth mouse/keyboard connected? Start here."""
        return phone.status()

    @mcp.tool()
    def iphone_screenshot() -> McpImage:
        """The iPhone's current screen as a JPEG. All coordinates elsewhere are fractions (0..1) of this image."""
        return shot()

    @mcp.tool()
    def iphone_tap(x: float, y: float, hold_ms: int = 0) -> dict:
        """Tap at (x, y), fractions of the screenshot (0,0 top-left). hold_ms > 0 makes it a long press.
        Look at a new screenshot afterwards: a tap is only an acknowledgement, not a result."""
        return phone.tap(x, y, hold_ms)

    @mcp.tool()
    def iphone_swipe(x1: float, y1: float, x2: float, y2: float) -> dict:
        """Press at (x1, y1), drag to (x2, y2) and release: scrolls lists, switches pages, pulls panels. Fractions."""
        return phone.swipe(x1, y1, x2, y2)

    @mcp.tool()
    def iphone_scroll(x: float, y: float, lines: int) -> str:
        """Mouse-wheel scroll at (x, y); positive lines scroll content up (reveal what is below), negative down."""
        phone.scroll(x, y, lines)
        return "ok"

    @mcp.tool()
    def iphone_type(text: str) -> str:
        """Type printable ASCII into the focused text field (no ç ş ğ ı ö ü or emoji: the keyboard is ASCII only)."""
        if not text.isascii() or any(ord(c) < 32 for c in text):
            raise BoardError("only printable ASCII can be typed (one line, no accents or emoji)")
        return phone._cmd(f"type {text}")

    @mcp.tool()
    def iphone_key(combo: str) -> str:
        """Press a key or combination: enter, esc, tab, backspace, space, up, down, left, right, cmd+space, cmd+h ..."""
        return phone._cmd(f"key {combo}")

    @mcp.tool()
    def iphone_home() -> str:
        """Go to the Home Screen (the hardware Home / AC Home key)."""
        return phone._cmd("homescreen")

    @mcp.tool()
    def iphone_calibrate() -> dict:
        """Re-measure the pointer scale (after changing AssistiveTouch tracking speed). Needs a mostly static screen."""
        return phone.calibrate()

    mcp.run()


def check() -> None:
    phone = Phone()
    time.sleep(3)
    print(json.dumps(phone.status(), indent=1, default=str))
    jpg, _ = phone.frame(timeout=6)
    Path("/tmp/iphone").mkdir(exist_ok=True)
    Path("/tmp/iphone/check.jpg").write_bytes(jpg)
    print("wrote /tmp/iphone/check.jpg")
    phone.screen.stop()


if __name__ == "__main__":
    check() if "--check" in sys.argv else serve()
