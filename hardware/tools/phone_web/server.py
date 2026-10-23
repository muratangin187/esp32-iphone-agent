# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp>=3.9", "av>=12", "pyserial>=3.5", "psutil>=5.9", "pillow>=10", "numpy>=1.26", "scipy>=1.11"]
# ///
"""Web UI to see and use phones from a browser: one page, any device, click/drag/type.

    uv run hardware/tools/phone_web/server.py [--port 8765] [--host 0.0.0.0]

Devices: the iPhone behind the ESP32 + UxPlay (phone_screen.py), and every Android phone adb can see (scrcpy).
No password: the server is meant for localhost, the LAN and Tailscale. It refuses requests whose Host header is a
name it does not know (DNS rebinding) and cross-site requests (Origin must match Host).
"""
import argparse
import asyncio
import json
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from android import AndroidDevice, AndroidError  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
IDLE_STOP_S = 30  # stop an Android's scrcpy this long after its last viewer left (saves the phone's battery)


# ---------------------------------------------------------------- host / origin checks
def host_allowed(host_header: str) -> bool:
    host = host_header.strip()
    if host.startswith("["):  # an IPv6 literal
        return True
    host = host.rsplit(":", 1)[0] if ":" in host else host
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host) or host == "localhost":
        return True
    me = socket.gethostname().lower()
    return host.lower() in (me, me + ".local") or host.lower().endswith(".ts.net")


@web.middleware
async def guard(request: web.Request, handler):
    if not host_allowed(request.headers.get("Host", "")):
        return web.Response(status=403, text="unknown host name")
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.headers.get("Host"):
        return web.Response(status=403, text="cross-site request")
    return await handler(request)


# ---------------------------------------------------------------- devices
class Client:
    def __init__(self, ws: web.WebSocketResponse):
        self.ws, self.busy = ws, False


class Entry:
    """A device plus the browsers watching it."""

    def __init__(self, id_: str, kind: str, name: str):
        self.id, self.kind, self.name = id_, kind, name
        self.clients: set[Client] = set()
        self.dev = None
        self.idle_task: asyncio.Task | None = None
        self.shown = 0  # frames pushed, for the fps readout
        self.fps = 0.0
        self._fps_t = time.time()

    async def push(self, jpg: bytes) -> None:
        self.shown += 1
        now = time.time()
        if now - self._fps_t >= 1:
            self.fps, self.shown, self._fps_t = self.shown / (now - self._fps_t), 0, now
        for c in list(self.clients):
            if c.busy or c.ws.closed:
                continue  # a slow viewer skips frames instead of building latency
            c.busy = True
            asyncio.create_task(self._send(c, jpg))

    @staticmethod
    async def _send(c: Client, jpg: bytes) -> None:
        try:
            await c.ws.send_bytes(jpg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            c.busy = False

    async def say(self, msg: dict) -> None:
        for c in list(self.clients):
            if not c.ws.closed:
                try:
                    await c.ws.send_json(msg)
                except Exception:  # noqa: BLE001
                    pass


registry: dict[str, Entry] = {}


async def sh(*args: str, timeout: float = 8) -> str:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return ""
    return out.decode(errors="replace")


async def discover() -> None:
    """Add the iPhone (once) and any Android phone adb can see (deduplicated by hardware serial)."""
    if "ios" not in registry:
        registry["ios"] = Entry("ios", "ios", "iPhone")
    seen: dict[str, str] = {}
    for line in (await sh("adb", "devices", "-l")).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        hw = (await sh("adb", "-s", serial, "shell", "getprop", "ro.serialno", timeout=5)).strip() or serial
        if hw in seen:
            continue
        seen[hw] = serial
        model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), hw)
        id_ = "android-" + hw
        if id_ not in registry:
            registry[id_] = Entry(id_, "android", model.replace("_", " "))
        registry[id_].adb_serial = serial  # type: ignore[attr-defined]
    for id_, e in list(registry.items()):
        if e.kind == "android" and id_[8:] not in seen and not e.clients:
            registry.pop(id_)


async def ensure_device(e: Entry) -> None:
    """Create and start the device behind an entry (an Android's scrcpy only while someone watches)."""
    if e.kind == "android":
        if e.dev is None:
            e.dev = AndroidDevice(e.adb_serial, e.name, e.push)  # type: ignore[attr-defined]
        await e.dev.start()
    else:
        if e.dev is None:
            from ios import IPhoneDevice  # imported late: it opens no hardware until used
            e.dev = IPhoneDevice(e.name, e.push)
        await e.dev.start()


def device_state(e: Entry) -> dict:
    d = e.dev
    st = {"id": e.id, "kind": e.kind, "name": e.name, "fps": round(e.fps, 1),
          "size": list(d.size) if d else [0, 0], "error": getattr(d, "error", None) if d else None,
          "packets": getattr(d, "packets", None) if d else None, "jpegs": getattr(d, "frames", None) if d else None}
    if e.kind == "ios" and d:
        st.update(d.status())
    return st


# ---------------------------------------------------------------- actions
async def handle(e: Entry, m: dict):
    d, t = e.dev, m.get("t")
    if d is None:
        raise RuntimeError("device not started")
    if e.kind == "android":
        if t == "touch":
            d.touch(m["a"], m["x"], m["y"])
        elif t == "tap":
            await d.tap(m["x"], m["y"], int(m.get("hold", 0)))
        elif t == "swipe":
            await d.swipe(m["x1"], m["y1"], m["x2"], m["y2"], int(m.get("ms", 300)))
        elif t == "scroll":
            d.scroll(m["x"], m["y"], 0.0, -float(m.get("dy", 0)))  # wheel down reveals what is below
        elif t == "text":
            d.text(m["s"])
        elif t == "key":
            k = m["k"]
            d.panel(k) if k in ("notifications", "settings", "collapse") else d.key(k)
        else:
            raise RuntimeError(f"unknown action {t!r}")
        return None
    if t == "tap":
        return await d.tap(m["x"], m["y"], int(m.get("hold", 0)))
    if t == "swipe":
        return await d.swipe(m["x1"], m["y1"], m["x2"], m["y2"])
    if t == "scroll":
        return await d.scroll(m["x"], m["y"], float(m.get("dy", 0)))
    if t == "text":
        return await d.text(m["s"])
    if t == "key":
        return await d.key(m["k"])
    if t == "imove":
        return await d.live_move(float(m["dx"]), float(m["dy"]))
    if t == "ibtn":
        return await d.button(m["b"])
    raise RuntimeError(f"unknown action {t!r}")


# ---------------------------------------------------------------- HTTP
async def index(_: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def devices(_: web.Request) -> web.Response:
    await discover()
    return web.json_response([device_state(e) for e in registry.values()])


async def screenshot(request: web.Request) -> web.Response:
    e = registry.get(request.match_info["id"])
    if not e:
        raise web.HTTPNotFound()
    try:
        await ensure_device(e)
        if e.kind == "android":
            await asyncio.wait_for(e.dev.ready.wait(), 15)
            if e.idle_task:
                e.idle_task.cancel()
            e.idle_task = asyncio.create_task(_idle_stop(e))
    except (AndroidError, asyncio.TimeoutError) as ex:
        raise web.HTTPServiceUnavailable(text=str(ex))
    jpg = e.dev.latest
    if not jpg:
        raise web.HTTPServiceUnavailable(text="no frame yet")
    return web.Response(body=jpg, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def act(request: web.Request) -> web.Response:
    """POST one action (the same JSON the page sends over the WebSocket): for scripts and the MCP server."""
    e = registry.get(request.match_info["id"])
    if not e:
        raise web.HTTPNotFound()
    try:
        await ensure_device(e)
        res = await handle(e, await request.json())
    except Exception as ex:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(ex)}, status=400)
    return web.json_response({"ok": True, "result": res})


async def _idle_stop(e: Entry) -> None:
    await asyncio.sleep(IDLE_STOP_S)
    if not e.clients and e.dev:
        await e.dev.stop()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    e = registry.get(request.match_info["id"])
    if not e:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    client = Client(ws)
    e.clients.add(client)
    if e.idle_task:
        e.idle_task.cancel()
    try:
        await ws.send_json({"type": "state", **device_state(e), "starting": True})
        try:
            await ensure_device(e)
            if e.kind == "android":
                await asyncio.wait_for(e.dev.ready.wait(), 20)
        except Exception as ex:  # noqa: BLE001
            await ws.send_json({"type": "error", "msg": f"could not start {e.name}: {ex or 'timeout'}"})
        await ws.send_json({"type": "state", **device_state(e)})
        if e.dev and e.dev.latest:
            await ws.send_bytes(e.dev.latest)
        status_task = asyncio.create_task(_status_loop(e, ws))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            asyncio.create_task(_run_action(e, ws, m))
        status_task.cancel()
    finally:
        e.clients.discard(client)
        if not e.clients and e.kind == "android" and e.dev:
            e.idle_task = asyncio.create_task(_idle_stop(e))
    return ws


async def _run_action(e: Entry, ws: web.WebSocketResponse, m: dict) -> None:
    t0 = time.time()
    try:
        info = await handle(e, m)
        if "id" in m and not ws.closed:
            await ws.send_json({"type": "done", "id": m["id"], "ms": round((time.time() - t0) * 1000), "info": info})
    except Exception as ex:  # noqa: BLE001
        if not ws.closed:
            await ws.send_json({"type": "error", "id": m.get("id"), "msg": str(ex)})


async def _status_loop(e: Entry, ws: web.WebSocketResponse) -> None:
    while not ws.closed:
        await asyncio.sleep(1.5)
        try:
            await ws.send_json({"type": "state", **device_state(e)})
        except Exception:  # noqa: BLE001
            return


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/", index), web.get("/api/devices", devices),
                    web.get("/api/{id}/screenshot.jpg", screenshot), web.post("/api/{id}/action", act),
                    web.get("/ws/{id}", ws_handler)])
    return app


async def on_start(app: web.Application) -> None:
    await discover()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    app = make_app()
    app.on_startup.append(on_start)
    web.run_app(app, host=a.host, port=a.port, print=lambda *_: print(f"http://{a.host}:{a.port}", flush=True))


if __name__ == "__main__":
    main()
