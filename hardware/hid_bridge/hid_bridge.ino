// HID bridge: an ESP32-S3 (XIAO, 8 MB PSRAM) plugged into the iPhone. It is the phone's
// USB mouse + keyboard, an AirPlay screen-mirroring receiver, and a BLE peripheral for the
// iOS app on the same phone. It holds no logic; the app decides. Protocol: docs/ble-protocol.md.
// Where the phone cannot host the board over USB (a Lightning iPhone without a powered camera adapter),
// the board is also a Bluetooth mouse + keyboard the phone pairs with in Settings > Bluetooth (ble_hid.cpp).
// Commands go over USB when the phone has mounted the board, otherwise over Bluetooth HID.
//
// HID commands (unchanged from the old TCP protocol, the board answers "ok" or "err <why>"):
//   ping | home | move <dx> <dy> | press | release | click
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//   homescreen (the consumer-control Home key, "AC Home": iOS goes to the Home Screen, like a keyboard's home key)

#include <esp_task_wdt.h>
#include <esp_mac.h>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "tusb.h"
#include "ble_link.h"
#include "ble_hid.h"
#include "netcfg.h"
#include "video_link.h"
#include "src/airplay/airplay.h"

const int STEP = 10;      // max pointer delta per report while moving
const int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable
const int HOME_REPORTS = 50;
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0  // 1: echo commands and the AirPlay handshake over USB serial
#endif

const int WATCHDOG_MS = 8000;  // a hung loop (e.g. blocked USB) reboots the board instead of staying offline

USBHIDMouse mouse;
USBHIDKeyboard keyboard;
USBHIDConsumerControl consumer;
const uint16_t AC_HOME = 0x0223;  // HID consumer page: AC Home
static String receiverName = "ESP32 Agent";
static bool fromSerial = false;  // the command being run arrived on USB serial, not over BLE

enum Route { ROUTE_NONE, ROUTE_USB, ROUTE_BLE };
static Route route = ROUTE_NONE;  // how the command being run reaches the phone; set by run()

void mouseMove(int dx, int dy, int wheel = 0, int pan = 0) {
  if (route == ROUTE_BLE) ble_hid_mouse_move(dx, dy, wheel, pan);
  else mouse.move(dx, dy, wheel, pan);
}
void mouseButton(bool down) {
  if (route == ROUTE_BLE) ble_hid_mouse_buttons(down ? HID_BTN_LEFT : 0);
  else if (down) mouse.press(MOUSE_LEFT);
  else mouse.release(MOUSE_LEFT);
}

void movePaced(long dx, long dy) {
  while (dx || dy) {
    int sx = constrain(dx, -STEP, STEP), sy = constrain(dy, -STEP, STEP);
    mouseMove(sx, sy);
    dx -= sx;
    dy -= sy;
    delay(STEP_MS);
  }
}

uint8_t keyCode(const String &k) {
  if (k == "enter") return KEY_RETURN;
  if (k == "esc") return KEY_ESC;
  if (k == "tab") return KEY_TAB;
  if (k == "backspace") return KEY_BACKSPACE;
  if (k == "space") return ' ';
  if (k == "up") return KEY_UP_ARROW;
  if (k == "down") return KEY_DOWN_ARROW;
  if (k == "left") return KEY_LEFT_ARROW;
  if (k == "right") return KEY_RIGHT_ARROW;
  if (k == "cmd") return KEY_LEFT_GUI;
  if (k == "ctrl") return KEY_LEFT_CTRL;
  if (k == "alt") return KEY_LEFT_ALT;
  if (k == "shift") return KEY_LEFT_SHIFT;
  return k.length() == 1 ? k[0] : 0;
}

void keysReleaseAll() {
  if (route == ROUTE_BLE) ble_hid_release_all();
  else keyboard.releaseAll();
}

bool pressCombo(String combo) {
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    String name = combo.substring(0, i);
    if (route == ROUTE_BLE) {
      uint8_t usage;
      bool shift;
      if (!ble_hid_key_usage(name, usage, shift)) { ble_hid_release_all(); return false; }
      if (shift) ble_hid_press(HID_LSHIFT);
      ble_hid_press(usage);
      continue;
    }
    uint8_t c = keyCode(name);
    if (!c) { keyboard.releaseAll(); return false; }
    keyboard.press(c);
  }
  delay(30);
  keysReleaseAll();
  return true;
}

// A locked or sleeping phone suspends USB; sending HID reports then can stall. Ask it to wake up instead,
// and report "asleep" right away if it does not, so the board never hangs.
bool usbReady() {
  if (tud_suspended()) {
    tud_remote_wakeup();
    for (int i = 0; i < 50 && tud_suspended(); i++) delay(10);
  }
  return tud_mounted() && !tud_suspended();
}

// USB when the phone has the board mounted, else Bluetooth HID. A locked or sleeping phone suspends USB
// (usbReady() tries to wake it); a paired phone's Bluetooth link does not depend on it.
Route pickRoute() {
  String m = cfg_hid_mode();
  if (m == "ble") return ble_hid_ready() ? ROUTE_BLE : ROUTE_NONE;
  if (m == "usb") return usbReady() ? ROUTE_USB : ROUTE_NONE;
  if (tud_mounted() && !tud_suspended()) return ROUTE_USB;
  if (ble_hid_ready()) return ROUTE_BLE;
  return usbReady() ? ROUTE_USB : ROUTE_NONE;
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  route = pickRoute();
  if (route == ROUTE_NONE) return "err phone hid not connected (usb asleep, or pair the board in Settings > Bluetooth)";
  if (cmd == "home") {  // slam the pointer into the top-left corner
    for (int i = 0; i < HOME_REPORTS; i++) { mouseMove(-127, -127); delay(route == ROUTE_BLE ? STEP_MS : 2); }
    return "ok";
  }
  if (cmd == "move") { movePaced(a, b); return "ok"; }
  if (cmd == "press") { mouseButton(true); return "ok"; }
  if (cmd == "release") { mouseButton(false); return "ok"; }
  if (cmd == "click") { mouseButton(true); delay(60); mouseButton(false); return "ok"; }
  if (cmd == "scroll") { mouseMove(0, 0, a, b); return "ok"; }
  if (cmd == "type") {
    if (route == ROUTE_BLE) ble_hid_type(arg);
    else keyboard.print(arg);
    return "ok";
  }
  if (cmd == "key") return pressCombo(arg) ? "ok" : "err unknown key";
  if (cmd == "homescreen") {
    if (route == ROUTE_BLE) { ble_hid_consumer(AC_HOME); delay(40); ble_hid_consumer(0); }
    else { consumer.press(AC_HOME); delay(40); consumer.release(); }
    return "ok";
  }
  return "err unknown command";
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

const char *usbState() {
  if (!tud_mounted()) return "off";
  return tud_suspended() ? "asleep" : "ok";
}

// Which HID path the next command would take: usb | ble | off
const char *hidState() {
  if (!strcmp(cfg_hid_mode(), "ble")) return ble_hid_ready() ? "ble" : "off";
  if (tud_mounted() && !tud_suspended()) return "usb";
  return ble_hid_ready() ? "ble" : "off";
}

// Board-level commands from the app; everything else is an HID command for run().
String board(const String &line, const String &id) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  int n = sscanf(arg.c_str(), "%ld %ld", &a, &b);
  if (cmd == "stream") {
    if (arg != "on" && arg != "off") return "err usage: stream on|off";
    video_stream(arg == "on", fromSerial);
    return "ok";
  }
  if (cmd == "status") return statusJson(true);  // BLE gets this once a second; serial asks for it
  if (cmd == "hid") {  // which path HID commands take: auto | ble | usb (stored)
    if (arg.length() && !cfg_set_hid_mode(arg)) return "err usage: hid auto|ble|usb";
    return String("ok ") + cfg_hid_mode();
  }
  if (cmd == "app" || cmd == "log") {  // debug relay: Mac -> app ("app run ...") and app -> Mac ("log ...")
    ble_reply(cmd + " " + arg);         // every peer sees it; each side ignores lines it doesn't handle
    return "ok";
  }
  if (cmd == "snap") { video_snap(id, fromSerial); return ""; }  // replies when the GOP is sent
  if (cmd == "bench") {
    if (fromSerial) return "err bench measures BLE";
    if (n < 1 || a <= 0 || a > 4096) return "err usage: bench KB (1-4096)";
    video_bench(id, a);
    return "";
  }
  if (cmd == "res") {
    if (n < 2 || a < 160 || b < 160 || a > 1920 || b > 2560) return "err usage: res W H";
    cfg_set_res(a, b);
    airplay_set_display(a, b, cfg_fps());
    return "ok";
  }
  if (cmd == "fps") {
    if (n < 1 || a < 1 || a > 60) return "err usage: fps N (1-60)";
    cfg_set_fps(a);
    airplay_set_display(cfg_width(), cfg_height(), a);
    return "ok";
  }
  if (cmd == "coex") {  // A/B knob, not stored: every boot starts at balance
    if (arg.length() && !net_coex(arg)) return "err usage: coex bt|balance|wifi";
    return String("ok ") + net_coex_mode();
  }
  if (cmd == "unpair") {
    ble_reply(id + " ok");  // answer before the link goes down
    ble_unpair();
    return "";
  }
  return run(line);
}

String statusJson(bool full) {
  static const char *apStates[] = {"idle", "connected", "streaming"};
  VideoStats v = video_stats();
  char s[416];
  snprintf(s, sizeof s,
           "{\"up\":%lu,\"rst\":\"%s\",\"heap\":%u,\"psram\":%u,\"usb\":\"%s\",\"hid\":\"%s\",\"net\":\"%s\",\"ip\":\"%s\",\"sta\":%d,"
           "\"ap\":\"%s\",\"w\":%u,\"h\":%u,\"kbps\":%lu,\"fps\":%.1f,\"idr\":%lu,\"gop\":%lu,"
           "\"ble\":{\"mtu\":%u,\"phy\":%u,\"kbps\":%lu,\"drop\":%lu,\"rdrop\":%lu,\"n\":%d}}",
           millis(), resetReason(), (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), usbState(), hidState(), net_mode(), net_ip().c_str(), net_peers(),
           apStates[airplay_state()], cfg_width(), cfg_height(), (unsigned long)v.kbps, v.fps, (unsigned long)v.idr_ms,
           (unsigned long)v.gop_bytes, ble_mtu(), ble_phy(), (unsigned long)ble_kbps(), (unsigned long)ble_drops(),
           (unsigned long)ble_reply_drops(), ble_peers());
  if (full) return s;
  // MTU below ~250 (before the MTU exchange): keep only the fields the app needs to stay alive
  snprintf(s, sizeof s, "{\"up\":%lu,\"rst\":\"%s\",\"usb\":\"%s\",\"net\":\"%s\",\"ap\":\"%s\",\"ble\":{\"mtu\":%u}}",
           millis(), resetReason(), usbState(), net_mode(), apStates[airplay_state()], ble_mtu());
  return s;
}

void statusTask(void *) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    if (!ble_connected()) continue;
    String s = statusJson(true);
    if (s.length() > ble_mtu() - 3u) s = statusJson(false);
    ble_status(s);
  }
}

void logLine(const char *msg) {
#if DEBUG_SERIAL
  Serial.printf("[%lu] %s\n", millis(), msg);
#endif
}

void startAirplay() {
  static airplay_cfg cfg;
  cfg.name = receiverName.c_str();
  esp_read_mac(cfg.hwaddr, ESP_MAC_WIFI_STA);
  cfg_ed25519_seed(cfg.ed25519_seed);
  cfg.width = cfg_width();
  cfg.height = cfg_height();
  cfg.max_fps = cfg_fps();
  cfg.on_config = video_on_config;
  cfg.on_au = video_on_au;
  cfg.log = logLine;
  if (!airplay_start(&cfg)) Serial.println("airplay: start failed");
}

void setup() {
  Serial.begin(115200);
#if DEBUG_SERIAL
  for (int i = 0; i < 30 && !Serial; i++) delay(100);  // give a serial monitor 3 s to catch the boot lines
#endif
  mouse.begin();
  keyboard.begin();
  consumer.begin();
  USB.productName("HID Bridge");
  USB.begin();
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);

  net_begin();
  if (!video_begin()) Serial.println("video: no PSRAM for the GOP buffer");
  ble_begin("ESP32 Agent " + board_suffix());
  startAirplay();
  xTaskCreatePinnedToCore(statusTask, "status", 4096, NULL, 1, NULL, 1);
  Serial.printf("boot %s: free internal heap %u (largest %u), PSRAM %u, reset %s\n", receiverName.c_str(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), resetReason());
}

void loop() {
  static bool linked = false;
  esp_task_wdt_reset();
  net_poll();
  if (linked && !ble_connected()) {
    mouse.release(MOUSE_LEFT);  // a gesture cut off mid-way must not leave the button held (long press)
    keyboard.releaseAll();
    ble_hid_release_everything();
    video_stream(false);
  }
  linked = ble_connected();

  String line;
  if (Serial.available()) {  // USB serial takes the same `<id> <command>` lines, for debugging from a Mac
    line = Serial.readStringUntil('\n');
    line.trim();
    if (line.startsWith("0 net ")) { net_request(line.substring(6)); Serial.println("0 ok"); return; }
    int sp = line.indexOf(' ');
    String id = sp < 0 ? line : line.substring(0, sp);
    fromSerial = true;
    String reply = board(sp < 0 ? "" : line.substring(sp + 1), id);
    fromSerial = false;
    if (reply.length()) serial_line(id + " " + reply);  // snap replies later, from the video task
    return;
  }
  if (!ble_pop_cmd(line, 50)) return;
  int sp = line.indexOf(' ');
  String id = sp < 0 ? line : line.substring(0, sp);
  String rest = sp < 0 ? "" : line.substring(sp + 1);
  rest.trim();
#if DEBUG_SERIAL
  unsigned long started = millis();
#endif
  String reply = id.toInt() || id == "0" ? board(rest, id) : "err missing request id";
  if (reply.length()) ble_reply(id + " " + reply);
#if DEBUG_SERIAL
  Serial.printf("%s -> %s (%lu ms)\n", line.c_str(), reply.c_str(), millis() - started);
#endif
}
