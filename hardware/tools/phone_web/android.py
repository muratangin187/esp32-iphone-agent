"""An Android phone through scrcpy: H.264 video (decoded to JPEG with PyAV) and touch/key/text control.

Speaks the scrcpy 5.0 wire protocol (doc/develop.md, app/src/control_msg.c). The server jar must match the version
of the `scrcpy` binary exactly; both come from the same package. One adb forward, two sockets: video, then control.
"""
import asyncio
import io
import os
import random
import re
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import av

SERVER_JAR_CANDIDATES = ["/usr/share/scrcpy/scrcpy-server", "/opt/homebrew/share/scrcpy/scrcpy-server",
                         "/usr/local/share/scrcpy/scrcpy-server"]
REMOTE_JAR = "/data/local/tmp/scrcpy-server-web.jar"
POINTER_FINGER = 0xFFFFFFFFFFFFFFFE  # SC_POINTER_ID_GENERIC_FINGER (-2)

# Android KeyEvent codes
KEYS = {"home": 3, "back": 4, "enter": 66, "backspace": 67, "tab": 61, "esc": 111, "space": 62, "delete": 112,
        "up": 19, "down": 20, "left": 21, "right": 22, "recents": 187, "power": 26, "vol_up": 24, "vol_down": 25,
        "menu": 82, "page_up": 92, "page_down": 93, "move_home": 122, "move_end": 123}


class AndroidError(RuntimeError):
    pass


async def _run(*args: str, timeout: float = 30) -> str:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise AndroidError(f"timeout: {' '.join(args[:4])}")
    if p.returncode:
        raise AndroidError(f"{' '.join(args[:5])}: {out.decode(errors='replace').strip()[:300]}")
    return out.decode(errors="replace")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def scrcpy_version() -> str:
    out = await _run("scrcpy", "--version")
    m = re.search(r"scrcpy (\d+(?:\.\d+)+)", out)
    if not m:
        raise AndroidError("cannot read the scrcpy version")
    return m.group(1)


class AndroidDevice:
    kind = "android"

    def __init__(self, serial: str, name: str, on_frame, max_size: int = 1280, fps: int = 30,
                 bitrate: int = 6_000_000, quality: int = 80):
        self.serial, self.name = serial, name
        self.on_frame = on_frame  # async callback(jpeg bytes)
        self.max_size, self.fps, self.bitrate, self.quality = max_size, fps, bitrate, quality
        self.size = (0, 0)  # the video frame size, which is also the touch coordinate space
        self.latest: bytes | None = None
        self.frames = 0
        self.packets = 0  # H.264 packets received from the phone, for diagnosis
        self.ready = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._server: asyncio.subprocess.Process | None = None
        self._dec: av.CodecContext | None = None
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="h264")  # decode + JPEG off the event loop
        self._ctrl: asyncio.StreamWriter | None = None
        self._video: asyncio.StreamWriter | None = None
        self._port = 0
        self._start_lock = asyncio.Lock()
        self.error: str | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._server is not None and self._server.returncode is None and self._ctrl is not None

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            await self.stop()
            self.error = None
            self.ready.clear()
            try:
                await self._start()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                await self.stop()
                raise

    async def _start(self) -> None:
        jar = next((p for p in SERVER_JAR_CANDIDATES if os.path.exists(p)), None)
        if not jar:
            raise AndroidError("scrcpy-server not found (install scrcpy)")
        version = await scrcpy_version()
        adb = ["adb", "-s", self.serial]
        await _run(*adb, "push", jar, REMOTE_JAR)
        scid = random.getrandbits(31)
        self._port = _free_port()
        await _run(*adb, "forward", f"tcp:{self._port}", f"localabstract:scrcpy_{scid:08x}")
        self._server = await asyncio.create_subprocess_exec(
            *adb, "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/", "com.genymobile.scrcpy.Server", version,
            f"scid={scid:08x}", "log_level=info", "audio=false", "tunnel_forward=true", "control=true",
            f"max_size={self.max_size}", f"max_fps={self.fps}", f"video_bit_rate={self.bitrate}", "cleanup=true",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # The adb forward accepts connections before the device listens: the connection is alive once the dummy byte
        # arrives. Keep one connection and wait for it (the server needs a few seconds to start); reconnect only
        # when adb closes it because nothing was listening yet.
        vr = None
        deadline = time.time() + 40
        while time.time() < deadline and vr is None:
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self._port)
                try:
                    await asyncio.wait_for(r.readexactly(1), max(1.0, deadline - time.time()))
                    vr, self._video = r, w
                except (asyncio.IncompleteReadError, ConnectionError):
                    w.close()
            except OSError:
                pass
            if vr is None:
                if self._server.returncode is not None:
                    raise AndroidError("the scrcpy server exited at start-up")
                await asyncio.sleep(0.4)
        if vr is None:
            raise AndroidError("could not connect to the scrcpy server on the phone")
        # The server accepts video, then control, and only then starts talking: open control before reading more.
        _, self._ctrl = await asyncio.open_connection("127.0.0.1", self._port)
        await vr.readexactly(64)  # device name
        self._tasks = [asyncio.create_task(self._video_loop(vr))]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        for w in (self._ctrl, self._video):
            if w:
                w.close()
        self._ctrl = self._video = None
        self._dec = None
        if self._server and self._server.returncode is None:
            self._server.terminate()
            try:
                await asyncio.wait_for(self._server.wait(), 3)
            except asyncio.TimeoutError:
                self._server.kill()
        self._server = None
        if self._port:
            try:
                await _run("adb", "-s", self.serial, "forward", "--remove", f"tcp:{self._port}", timeout=5)
            except AndroidError:
                pass
            self._port = 0

    # ------------------------------------------------------------ video
    def _new_decoder(self) -> None:
        self._dec = av.CodecContext.create("h264", "r")
        self._dec.options = {"flags": "low_delay"}  # no reordering delay: frames come out as they go in

    def _decode(self, data: bytes, dec: "av.CodecContext") -> list[bytes]:
        """One packet of the stream to zero or more JPEGs (runs in the decode thread)."""
        out = []
        for packet in dec.parse(data):
            for frame in dec.decode(packet):
                buf = io.BytesIO()
                frame.to_image().save(buf, "JPEG", quality=self.quality)
                out.append(buf.getvalue())
        return out

    async def _video_loop(self, r: asyncio.StreamReader) -> None:
        loop = asyncio.get_running_loop()
        try:
            await r.readexactly(4)  # codec id ("h264")
            while True:
                hdr = await r.readexactly(12)
                if hdr[0] & 0x80:  # session packet: the video size, again when the phone rotates
                    w, h = struct.unpack(">II", hdr[4:12])
                    self.size = (w, h)
                    self._new_decoder()
                    continue
                n = struct.unpack(">I", hdr[8:12])[0]
                data = await r.readexactly(n)
                self.packets += 1
                if self._dec is None:
                    continue
                for jpg in await loop.run_in_executor(self._pool, self._decode, data, self._dec):
                    self.latest, self.frames = jpg, self.frames + 1
                    self.ready.set()
                    await self.on_frame(jpg)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.error = self.error or "video stream ended"
            if self._ctrl:
                self._ctrl.close()
                self._ctrl = None

    # ------------------------------------------------------------ control
    def _send(self, data: bytes) -> None:
        if not self._ctrl:
            raise AndroidError("the phone is not connected")
        self._ctrl.write(data)

    def _pos(self, fx: float, fy: float) -> bytes:
        w, h = self.size
        x = min(max(int(fx * w), 0), w - 1)
        y = min(max(int(fy * h), 0), h - 1)
        return struct.pack(">iiHH", x, y, w, h)

    def touch(self, action: str, fx: float, fy: float) -> None:
        a = {"down": 0, "up": 1, "move": 2}[action]
        pressure = 0 if action == "up" else 0xFFFF
        self._send(struct.pack(">BBQ", 2, a, POINTER_FINGER) + self._pos(fx, fy) + struct.pack(">HII", pressure, 0, 0))

    def key(self, name: str) -> None:
        code = KEYS.get(name)
        if code is None:
            raise AndroidError(f"unknown key {name!r}")
        for action in (0, 1):
            self._send(struct.pack(">BBIII", 0, action, code, 0, 0))

    def text(self, s: str) -> None:
        """ASCII goes through key injection; anything else is pasted (scrcpy's clipboard route handles Unicode)."""
        if s.isascii() and s.isprintable():
            b = s.encode()[:300]
            self._send(struct.pack(">BI", 1, len(b)) + b)
        else:
            b = s.encode()
            self._send(struct.pack(">BQBI", 9, int(time.time() * 1000) & 0x7FFFFFFF, 1, len(b)) + b)

    def scroll(self, fx: float, fy: float, dx: float, dy: float) -> None:
        def fp(v: float) -> int:
            return int(max(-1.0, min(1.0, v / 16)) * 32767)
        self._send(struct.pack(">B", 3) + self._pos(fx, fy) + struct.pack(">hhI", fp(dx), fp(dy), 0))

    def panel(self, which: str) -> None:
        self._send(bytes([{"notifications": 5, "settings": 6, "collapse": 7}[which]]))

    async def tap(self, fx: float, fy: float, hold_ms: int = 0) -> None:
        self.touch("down", fx, fy)
        await asyncio.sleep(max(hold_ms, 40) / 1000)
        self.touch("up", fx, fy)

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300) -> None:
        steps = max(4, ms // 16)
        self.touch("down", x1, y1)
        for i in range(1, steps + 1):
            await asyncio.sleep(ms / 1000 / steps)
            self.touch("move", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        self.touch("up", x2, y2)
