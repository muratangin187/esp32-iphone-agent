"""The iPhone for the web UI: frames from the UxPlay receiver (phone_screen.py), hands through iphone_mcp.Phone."""
import asyncio
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from iphone_mcp import Phone  # noqa: E402

IOS_KEYS = {"enter", "esc", "tab", "backspace", "space", "up", "down", "left", "right"}


class IPhoneError(RuntimeError):
    pass


class IPhoneDevice:
    kind = "ios"

    def __init__(self, name: str, on_frame):
        self.name, self.on_frame = name, on_frame
        self.phone = Phone()
        self.latest: bytes | None = None
        self.size = (0, 0)
        self.frames = 0
        self.error: str | None = None
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="iphone")  # board commands one at a time
        self._task: asyncio.Task | None = None
        self._live = [0.0, 0.0]
        self._live_task: asyncio.Task | None = None
        self._mtime = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        pass  # the receiver is a separate service; nothing to release

    async def _poll(self) -> None:
        """Pick up the newest frame the receiver wrote (live.jpg), about 20 times a second."""
        while True:
            m = self.phone.screen.updated
            if m != self._mtime:
                self._mtime = m
                jpg = self.phone.screen.frame
                if jpg:
                    self.latest, self.frames = jpg, self.frames + 1
                    if self.size == (0, 0) or self.frames % 50 == 1:
                        self.size = Image.open(io.BytesIO(jpg)).size
                    await self.on_frame(jpg)
            await asyncio.sleep(0.05)

    def status(self) -> dict:
        return {"mirroring": self.phone.screen.mirroring, "receiver": self.phone.screen.receiver_running}

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, fn, *args)
        except Exception as e:  # noqa: BLE001
            raise IPhoneError(str(e)) from e

    # ------------------------------------------------------------ actions
    async def tap(self, fx: float, fy: float, hold_ms: int = 0):
        return await self._call(self.phone.tap, fx, fy, hold_ms)

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 0):
        return await self._call(self.phone.swipe, x1, y1, x2, y2)

    async def scroll(self, fx: float, fy: float, dy: float):
        lines = -round(dy * 4) or (-1 if dy > 0 else 1)  # wheel down (dy > 0) scrolls content up: negative lines
        return await self._call(self.phone.scroll, fx, fy, lines)

    async def text(self, s: str):
        if not s.isascii() or any(ord(c) < 32 for c in s):
            raise IPhoneError("the iPhone keyboard is ASCII only (no ç ş ğ ı ö ü or emoji)")
        return await self._call(self.phone._cmd, f"type {s}")

    async def key(self, k: str):
        if k == "home":
            return await self._call(self.phone._cmd, "homescreen")
        if k in IOS_KEYS or "+" in k:
            return await self._call(self.phone._cmd, f"key {k}")
        raise IPhoneError(f"no {k!r} key on an iPhone")

    # Live mode: the browser mouse drives the pointer like a remote desktop's. Moves are summed while the
    # board is busy and sent as one paced move.
    async def live_move(self, dfx: float, dfy: float):
        self._live[0] += dfx
        self._live[1] += dfy
        if not self._live_task or self._live_task.done():
            self._live_task = asyncio.create_task(self._drain_live())

    async def _drain_live(self):
        while abs(self._live[0]) > 1e-4 or abs(self._live[1]) > 1e-4:
            dx, dy = self._live
            self._live = [0.0, 0.0]
            await self._call(self.phone.live_move, dx, dy)

    async def board_status(self):
        """The ESP32's own status (opens the serial port): is the Bluetooth mouse connected to the phone?"""
        return await self._call(lambda: __import__("json").loads(self.phone._cmd("status")))

    async def button(self, what: str):
        cmd = {"click": "click", "press": "press", "release": "release"}[what]
        return await self._call(self.phone._cmd, cmd)
