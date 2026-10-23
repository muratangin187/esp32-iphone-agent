# /// script
# requires-python = ">=3.10"
# dependencies = ["psutil>=5.9"]
# ///
"""The phone's screen on this computer: UxPlay receives AirPlay mirroring, GStreamer turns it into JPEG frames.

    uv run hardware/tools/phone_screen.py --name "iPhone Agent"      # keeps /tmp/iphone/live.jpg + live.json current

Choose the name in the phone's Control Center > Screen Mirroring. Needs `uxplay` (https://github.com/FDH2/UxPlay) with
the GStreamer plugins jpegenc and avdec_h264, and avahi for Bonjour; the phone and this computer share one network.
With a firewall, allow the LAN to reach UxPlay's fixed ports (`-p`): TCP 7000 7001 7100, UDP 6000 6001 7011, and
UDP 5353 (Bonjour).
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import psutil

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class PhoneScreen:
    """Keeps UxPlay running and the newest JPEG frame of the phone's screen."""

    def __init__(self, name: str = "iPhone Agent", fps: int = 4, quality: int = 85, uxplay: str | None = None):
        self.name, self.fps, self.quality = name, fps, quality
        self.uxplay = uxplay or shutil.which("uxplay") or str(Path.home() / ".local/bin/uxplay")
        self.frame: bytes | None = None
        self.seq = 0  # frames received so far
        self.updated = 0.0
        self._proc: subprocess.Popen | None = None
        self._stop = False
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._supervise, daemon=True)

    def start(self) -> "PhoneScreen":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # -- UxPlay supervision: it exits on some disconnects, so start it again every time
    def _supervise(self) -> None:
        while not self._stop:
            read_fd, write_fd = os.pipe()
            sink = (f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
                    f"jpegenc quality={self.quality} ! fdsink fd={write_fd}")
            self._proc = subprocess.Popen(
                [self.uxplay, "-n", self.name, "-nh", "-a", "-reset", "0", "-p", "-vs", sink],  # -p: fixed ports (firewall)
                pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(write_fd)
            self._read(read_fd)
            self.frame = None
            code = self._proc.wait()
            if not self._stop:
                time.sleep(1)  # restart; the phone must choose the receiver in Screen Mirroring again

    def _read(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as f:
            while not self._stop:
                chunk = f.read(1 << 16)
                if not chunk:
                    return
                buf += chunk
                while True:
                    a = buf.find(SOI)
                    if a < 0:
                        buf = b""
                        break
                    b = buf.find(EOI, a + 2)
                    if b < 0:
                        buf = buf[a:]
                        break
                    frame, buf = buf[a:b + 2], buf[b + 2:]
                    with self._cond:
                        self.frame, self.updated = frame, time.time()
                        self.seq += 1
                        self._cond.notify_all()

    # -- state
    @property
    def mirroring(self) -> bool:
        """A phone is connected to UxPlay (established TCP connections of its process)."""
        try:
            conns = psutil.Process(self._proc.pid).net_connections(kind="tcp")
        except (psutil.Error, AttributeError):
            return False
        return any(c.status == psutil.CONN_ESTABLISHED for c in conns)

    def wait_frame(self, after: float = 0.0, timeout: float = 5.0) -> bytes | None:
        """The first frame newer than `after`; if the screen did not change, the newest one we have."""
        deadline = time.time() + timeout
        with self._cond:
            while self.updated <= after and time.time() < deadline:
                self._cond.wait(timeout=max(0.0, deadline - time.time()))
            return self.frame


class FileScreen:
    """The same view of the screen, read from the files a running `phone_screen.py` writes (see main()).

    The receiver outlives any client: restarting the MCP server does not end the phone's Screen Mirroring session.
    """

    def __init__(self, path: Path = Path("/tmp/iphone/live.jpg")):
        self.path = path
        self.status_path = path.with_suffix(".json")

    def _status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return {}

    @property
    def frame(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError:
            return None

    @property
    def updated(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def mirroring(self) -> bool:
        st = self._status()
        return bool(st.get("mirroring")) and time.time() - st.get("written", 0) < 5

    @property
    def receiver_running(self) -> bool:
        return time.time() - self._status().get("written", 0) < 5

    def start(self) -> "FileScreen":
        return self

    def stop(self) -> None:
        pass

    def wait_frame(self, after: float = 0.0, timeout: float = 5.0) -> bytes | None:
        deadline = time.time() + timeout
        while self.updated <= after and time.time() < deadline:
            time.sleep(0.03)
        return self.frame


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="iPhone Agent", help="receiver name in Screen Mirroring")
    ap.add_argument("--out", type=Path, default=Path("/tmp/iphone/live.jpg"), help="newest frame; status goes next to it")
    ap.add_argument("--fps", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = run until killed)")
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    status = a.out.with_suffix(".json")
    screen = PhoneScreen(a.name, fps=a.fps).start()
    print(f"receiver '{a.name}' started; choose it in Control Center > Screen Mirroring", flush=True)
    end = time.time() + a.seconds if a.seconds else float("inf")
    last, last_status = 0, 0.0
    try:
        while time.time() < end:
            time.sleep(0.05)
            if screen.seq != last and screen.frame:
                last = screen.seq
                tmp = a.out.with_suffix(".tmp")
                tmp.write_bytes(screen.frame)
                tmp.replace(a.out)
            if time.time() - last_status >= 1:
                last_status = time.time()
                tmp = status.with_suffix(".jtmp")
                tmp.write_text(json.dumps({"mirroring": screen.mirroring, "seq": screen.seq, "name": a.name,
                                           "written": last_status}))
                tmp.replace(status)
    finally:
        screen.stop()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    main()
