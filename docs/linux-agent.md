# Control an iPhone from a Linux computer with an AI agent (MCP)

This fork adds a path with **no Mac, no app on the phone and no USB host adapter**: the computer is the brain and the
eyes, a tiny ESP32 is the hands. It was built and tested with an iPhone 14 Pro Max (Lightning), a Linux computer and
an ESP32-C3 (also works with the ESP32-S3 board of the main project).

```
iPhone ──AirPlay Screen Mirroring (home Wi-Fi)──▶ Linux computer: UxPlay ─▶ JPEG frames ─▶ MCP server ─▶ agent
iPhone ◀── Bluetooth mouse + keyboard ── ESP32 ◀── USB serial ──────────────────────────┘
```

- **Eyes:** [UxPlay](https://github.com/FDH2/UxPlay) receives the phone's screen mirroring; `phone_screen.py` keeps
  the newest JPEG in `/tmp/iphone/live.jpg` (498×1080 for a 14 Pro Max). It runs as a user service, so the phone's
  mirroring session survives restarts of the MCP server.
- **Hands:** the ESP32 pairs with the phone as a Bluetooth mouse and keyboard (Settings > Bluetooth) and takes
  `<id> <command>` lines on USB serial. AssistiveTouch must be on for the pointer.
- **Brain:** `hardware/tools/iphone_mcp.py` is an MCP server with `iphone_status`, `iphone_screenshot`, `iphone_tap`,
  `iphone_swipe`, `iphone_scroll`, `iphone_type`, `iphone_key`, `iphone_home`, `iphone_calibrate`. Coordinates are
  fractions (0..1) of the screenshot.

## Why a relative mouse can tap a point

The Bluetooth mouse only moves by deltas. A tap slams the pointer into the top-left corner (`home`), moves it by the
calibrated number of units (vertical, then horizontal: iOS accelerates the pointer with speed, so equal, paced,
axis-aligned moves are predictable), finds the pointer circle by diffing against the frame taken right after `home`,
nudges it if it is off by more than ~1% and clicks. Measured on the 14 Pro Max: after `home` the pointer centre sits at
about (22, 21) px of 498×1080, then moves 0.78 px per unit, residual error about 3 px, about 3 s per tap.
`iphone_calibrate` re-measures after you change AssistiveTouch's tracking speed.

## Setup

1. **Hands, ESP32-C3** (`hardware/hid_bridge_c3`, shares `ble_hid.cpp` with the S3 firmware):

   ```bash
   arduino-cli compile -b "esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashMode=dio,FlashFreq=40" --output-dir build_c3 hardware/hid_bridge_c3
   esptool --chip esp32c3 --port /dev/ttyACM0 write-flash 0x0 build_c3/hid_bridge_c3.ino.merged.bin
   ```

   **ESP32-S3 instead:** flash `hardware/flash.sh` as usual, then once `hid ble` over serial (a board plugged into a
   computer is also a USB mouse to it; `hid ble` keeps commands off that path). It also works as the eyes, but
   at 480×1040 and a few seconds per screenshot; UxPlay is better.
2. **Serial access on Linux:** `sudo usermod -aG uucp $USER` (group name varies: `dialout` on Debian).
3. **UxPlay:** build it (needs libplist, openssl, GStreamer with `jpegenc` and `avdec_h264`, avahi), e.g.
   `cmake -S UxPlay -B build -DCMAKE_INSTALL_PREFIX=$HOME/.local && cmake --build build && cmake --install build`.
4. **Firewall:** the phone must reach UxPlay's fixed ports (`-p`). With ufw:

   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 7000,7001,7100 proto tcp
   sudo ufw allow from 192.168.1.0/24 to any port 6000,6001,7011 proto udp
   sudo ufw allow from 192.168.1.0/24 to any port 5353 proto udp
   ```
5. **Screen service:** `systemctl --user enable --now iphone-screen.service` (unit: `docs/iphone-screen.service`;
   adjust the `uv` path).
6. **Phone:** AssistiveTouch on (Settings > Accessibility > Touch); Settings > Bluetooth > the board; Control Center >
   Screen Mirroring > *iPhone Agent*. Keep Auto-Lock off while the agent works: mirroring stops when the phone locks.
7. **MCP:** `claude mcp add iphone -- uv run /path/to/hardware/tools/iphone_mcp.py` (on the same machine), or over
   SSH from another machine: `claude mcp add iphone -- ssh host 'cd ~ && uv run ~/path/hardware/tools/iphone_mcp.py'`.
   Check without MCP: `uv run hardware/tools/iphone_mcp.py --check`.

## Limits

- Keyboard input is ASCII only (no ç ş ğ ı ö ü, no emoji).
- Passcode entry, Apple Pay and DRM video are not mirrored; mirroring stops while the phone is locked.
- Tap precision is ~3 px at 498 px width: tiny targets near the screen edge (the pointer centre cannot reach the
  outer ~3% of the screen) or in the status bar (pointer detection ignores the top 6%) may miss.
- The relative scale was measured on one phone and tracking speed; run `iphone_calibrate` on a new one.
- Only the phone's Bluetooth link uses the radio: the ESP32 takes no Wi-Fi, so there is nothing to join or configure
  on the board.

## Web UI and one MCP for every phone

`hardware/tools/phone_web/server.py` serves a page that shows the iPhone and every Android phone adb can see, and lets
you use them from Chrome: click, drag, mouse-wheel scroll, keyboard, Home/Back/Recents. It runs as a user service
(`docs/phone-web.service`, port 8765) on the computer the phones are connected to, and answers on localhost, the LAN
and Tailscale. There is no password: it refuses a Host header that is not an IP, `localhost`, the machine's name or a
`*.ts.net` name (DNS rebinding) and any cross-site request (`Origin` must equal `Host`).

- **Android** uses scrcpy (`android.py`, protocol 5.0): H.264 over an adb forward, decoded to JPEG with PyAV, touch,
  key, text and scroll injected through the control socket. Non-ASCII text (Turkish letters, emoji) is pasted through
  the clipboard. 15-30 frames/s; scrcpy starts when someone watches and stops 30 s after the last viewer leaves.
- **iPhone** shows UxPlay's frames (~5/s) and uses the Bluetooth mouse. Click mode: about 2-3 s for the first tap and
  0.6-1.3 s for the next ones (the pointer position is tracked instead of homing into the corner each time; a
  measurement that disagrees with the target by more than 10% is discarded and the next tap homes again). **Live
  mouse** (button in the header) moves the iPhone's pointer by the browser mouse's motion, like a remote desktop.
- The page's `/api/<id>/screenshot.jpg` and `POST /api/<id>/action` (the same JSON as the WebSocket) are what
  `phone_mcp.py` uses: `claude mcp add phones -- uv run hardware/tools/phone_mcp.py` gives an agent
  `phone_devices`, `phone_screenshot`, `phone_tap`, `phone_swipe`, `phone_scroll`, `phone_type`, `phone_key` and
  `phone_home` for both phones. Only one process may own the ESP32's serial port, so with the web service running,
  use `phone_mcp.py` instead of `iphone_mcp.py`.
- Reach it from another machine with `ssh -L 8765:localhost:8765 host` (then http://localhost:8765), or open the port to
  the LAN/Tailscale: `sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp` and
  `sudo ufw allow in on tailscale0 to any port 8765 proto tcp`.

Known limits: the iPhone's *hardware keyboard layout* decides the letters (a Turkish Q layout makes the `i` key type
`ı`; pick English (US) for the board's keyboard in Settings > General > Keyboard > Hardware Keyboard), and a long tap
or a drag on the iPhone moves the pointer in two axis-aligned legs, so the path is not a straight line.
