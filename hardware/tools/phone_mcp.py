# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""MCP server for every phone the web UI (phone_web/server.py) serves: the iPhone and Android phones, one interface.

    claude mcp add phones -- uv run hardware/tools/phone_mcp.py
    claude mcp add phones -- ssh xps 'cd ~ && uv run ~/path/hardware/tools/phone_mcp.py'    # from another machine

It is a thin client of the web server's HTTP API (default http://127.0.0.1:8765, `PHONE_WEB_URL` overrides), because
only one process may own the ESP32's serial port and the phone's scrcpy session. Coordinates are fractions (0..1) of the
screenshot, x to the right and y downwards. Look at a new screenshot after every action.
"""
import json
import os
import time
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP, Image

BASE = os.environ.get("PHONE_WEB_URL", "http://127.0.0.1:8765").rstrip("/")
mcp = FastMCP("phones")


def _http(path: str, data: dict | None = None, timeout: float = 60) -> bytes:
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise RuntimeError(f"{e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"the phone web server is not reachable at {BASE} ({e.reason}); "
                           "start it: systemctl --user start phone-web") from None


def _devices() -> list[dict]:
    return json.loads(_http("/api/devices"))


def _resolve(device: str) -> str:
    """'ios'/'iphone', 'android', or an id from phone_devices."""
    devs = _devices()
    ids = [d["id"] for d in devs]
    key = device.strip().lower()
    if key in ids:
        return key
    if key in ("iphone", "ios"):
        return "ios"
    if key == "android":
        for d in devs:
            if d["kind"] == "android":
                return d["id"]
    matches = [i for i in ids if key in i.lower()]
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(f"unknown device {device!r}; known: {', '.join(ids)}")


def _act(device: str, **action) -> object:
    res = json.loads(_http(f"/api/{_resolve(device)}/action", action))
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "failed"))
    return res.get("result")


@mcp.tool()
def phone_devices() -> list[dict]:
    """The phones available: id, kind (ios or android), name, screen size, mirroring state. Start here."""
    return [{k: d.get(k) for k in ("id", "kind", "name", "size", "mirroring", "error")} for d in _devices()]


@mcp.tool()
def phone_screenshot(device: str) -> Image:
    """The phone's current screen as a JPEG. `device` is 'ios', 'android' or an id from phone_devices.
    All coordinates in the other tools are fractions (0..1) of this image."""
    time.sleep(0.4)  # let the screen settle after the last action
    return Image(data=_http(f"/api/{_resolve(device)}/screenshot.jpg"), format="jpeg")


@mcp.tool()
def phone_tap(device: str, x: float, y: float, hold_ms: int = 0) -> object:
    """Tap at (x, y). hold_ms > 0 is a long press. On the iPhone a tap takes about 1-3 s (a Bluetooth mouse moves the
    pointer there); on Android it is instant. Take a screenshot afterwards: a tap is only an acknowledgement."""
    return _act(device, t="tap", x=x, y=y, hold=hold_ms)


@mcp.tool()
def phone_swipe(device: str, x1: float, y1: float, x2: float, y2: float) -> object:
    """Press at (x1, y1), drag to (x2, y2), release: scrolls lists, switches pages, pulls panels."""
    return _act(device, t="swipe", x1=x1, y1=y1, x2=x2, y2=y2)


@mcp.tool()
def phone_scroll(device: str, x: float, y: float, amount: float) -> object:
    """Mouse-wheel scroll at (x, y): positive amount reveals content below (like scrolling down), negative above."""
    return _act(device, t="scroll", x=x, y=y, dy=amount)


@mcp.tool()
def phone_type(device: str, text: str) -> object:
    """Type text into the focused field. Android takes any text (accents and emoji are pasted). The iPhone takes
    printable ASCII only, and its hardware-keyboard layout decides the letters (a Turkish Q layout turns i into ı)."""
    return _act(device, t="text", s=text)


@mcp.tool()
def phone_key(device: str, key: str) -> object:
    """Press a key: enter, backspace, tab, esc, space, up, down, left, right, delete, home. Android also: back, recents,
    notifications, settings (panel), power, vol_up, vol_down. The iPhone has no back/recents key."""
    return _act(device, t="key", k=key)


@mcp.tool()
def phone_home(device: str) -> object:
    """Go to the Home Screen."""
    return _act(device, t="key", k="home")


if __name__ == "__main__":
    mcp.run()
