# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22"]
# ///
"""Point the board at your home Wi-Fi over BLE (or back to its own hotspot).

    uv run hardware/tools/set_wifi.py            # asks for the Wi-Fi name and password (hidden), 2.4 GHz only
    uv run hardware/tools/set_wifi.py --ap       # back to the board's own hotspot

The password is typed at the prompt, never on the command line, and is not printed. In `sta` mode the phone stays
on the home Wi-Fi and sees the board in Screen Mirroring through Bonjour; the router must not isolate clients.
Writing `net` needs an encrypted link: BlueZ is told to bond first, macOS asks by itself.
"""
import argparse
import asyncio
import getpass
import json
import sys

from bleak import BleakClient, BleakScanner

BASE = "6e400001-a9e8-4b1b-9c39-6d6f8a3c"
NET, STATUS = BASE + "0005", BASE + "0003"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ap", action="store_true", help="switch back to the board's own hotspot")
    ap.add_argument("--ssid", help="Wi-Fi name (asked for when omitted)")
    ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for scripts)")
    ap.add_argument("--scan", type=float, default=20, help="seconds to look for the board")
    a = ap.parse_args()

    if a.ap:
        cfg = {"mode": "ap"}
    else:
        ssid = a.ssid or input("Wi-Fi name (2.4 GHz): ").strip()
        password = (sys.stdin.readline().rstrip("\n") if a.password_stdin
                    else getpass.getpass("Wi-Fi password (empty = open network): "))
        if not ssid:
            print("no Wi-Fi name given")
            return 1
        cfg = {"mode": "sta", "ssid": ssid, "pass": password}

    print("looking for the board ...")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: (adv.local_name or d.name or "").startswith("ESP32 Agent"), timeout=a.scan)
    if not dev:
        print("board not found (is it powered and advertising?)")
        return 1
    async with BleakClient(dev, timeout=20) as client:
        if sys.platform.startswith("linux"):
            try:
                await client.pair()  # Just Works bonding; a no-op when already bonded
            except Exception as e:  # noqa: BLE001
                print("pair:", repr(e))
        await client.write_gatt_char(NET, json.dumps(cfg).encode(), response=True)
        print("sent; the board restarts its Wi-Fi ...")
        await asyncio.sleep(12)
        try:
            st = json.loads(await client.read_gatt_char(STATUS))
            print("board status: net=%s ip=%s stations=%s" % (st.get("net"), st.get("ip"), st.get("sta")))
            if cfg["mode"] == "sta" and st.get("ip", "0.0.0.0") in ("0.0.0.0", ""):
                print("no IP yet: wrong password, a 5 GHz-only network, or the router is slow. Run again to retry.")
        except Exception as e:  # noqa: BLE001
            print("could not read status afterwards:", repr(e))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
