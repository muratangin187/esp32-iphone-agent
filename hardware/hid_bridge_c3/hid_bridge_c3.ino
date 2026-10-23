// HID bridge on an ESP32-C3: the phone's Bluetooth mouse + keyboard, driven by a computer over USB serial.
// No Wi-Fi, no AirPlay, no video: the computer receives the phone's screen itself (UxPlay) and this board is only
// the hands. Same `<id> <command>` lines and replies as the S3 firmware (hid_bridge) on USB serial:
//   ping | status | hid | unpair | home | move <dx> <dy> | press | release | click
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter> | homescreen
// The phone pairs once in Settings > Bluetooth ("ESP32 HID xxxx"); AssistiveTouch must be on for the pointer.
// ble_hid.cpp/.h and ble_link.h are shared with hid_bridge (symlinks).

#include <NimBLEDevice.h>
#include <esp_mac.h>
#include "ble_hid.h"

const int STEP = 10;     // max pointer delta per report while moving
const int STEP_MS = 8;   // report interval; constant pace keeps iOS pointer acceleration predictable
const int HOME_REPORTS = 50;
const uint16_t AC_HOME = 0x0223;  // HID consumer page: AC Home

// ble_hid.cpp asks this to keep app connections out of the HID target; here every connection is the phone's.
bool ble_is_app_conn(uint16_t) { return false; }

class ServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *s, NimBLEConnInfo &ci) override {
    s->updateConnParams(ci.getConnHandle(), 6, 12, 0, 200);  // 7.5-15 ms interval for a smooth pointer
  }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &ci, int) override {
    ble_hid_on_disconnect(ci.getConnHandle());
    NimBLEDevice::startAdvertising();
  }
};

static String boardName() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_BT);
  char s[24];
  snprintf(s, sizeof s, "ESP32 HID %02X%02X", mac[4], mac[5]);
  return s;
}

void movePaced(long dx, long dy) {
  while (dx || dy) {
    int sx = constrain(dx, -STEP, STEP), sy = constrain(dy, -STEP, STEP);
    ble_hid_mouse_move(sx, sy);
    dx -= sx;
    dy -= sy;
    delay(STEP_MS);
  }
}

bool pressCombo(String combo) {
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    uint8_t usage;
    bool shift;
    if (!ble_hid_key_usage(combo.substring(0, i), usage, shift)) { ble_hid_release_all(); return false; }
    if (shift) ble_hid_press(HID_LSHIFT);
    ble_hid_press(usage);
  }
  delay(30);
  ble_hid_release_all();
  return true;
}

String status() {
  char s[160];
  snprintf(s, sizeof s, "{\"up\":%lu,\"chip\":\"esp32c3\",\"usb\":\"ok\",\"hid\":\"%s\",\"net\":\"none\",\"ap\":\"idle\"}",
           millis(), ble_hid_ready() ? "ble" : "off");
  return s;
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  if (cmd == "status") return status();
  if (cmd == "hid") return "ok ble";  // the only path this board has
  if (cmd == "unpair") {
    NimBLEDevice::deleteAllBonds();
    return "ok";
  }
  if (!ble_hid_ready()) return "err phone hid not connected (pair the board in Settings > Bluetooth)";
  if (cmd == "home") {  // slam the pointer into the top-left corner
    for (int i = 0; i < HOME_REPORTS; i++) { ble_hid_mouse_move(-127, -127); delay(STEP_MS); }
    return "ok";
  }
  if (cmd == "move") { movePaced(a, b); return "ok"; }
  if (cmd == "press") { ble_hid_mouse_buttons(HID_BTN_LEFT); return "ok"; }
  if (cmd == "release") { ble_hid_mouse_buttons(0); return "ok"; }
  if (cmd == "click") { ble_hid_mouse_buttons(HID_BTN_LEFT); delay(60); ble_hid_mouse_buttons(0); return "ok"; }
  if (cmd == "scroll") { ble_hid_mouse_move(0, 0, a, b); return "ok"; }
  if (cmd == "type") { ble_hid_type(arg); return "ok"; }
  if (cmd == "key") return pressCombo(arg) ? "ok" : "err unknown key";
  if (cmd == "homescreen") { ble_hid_consumer(AC_HOME); delay(40); ble_hid_consumer(0); return "ok"; }
  return "err unknown command";
}

void setup() {
  Serial.begin(115200);
  String name = boardName();
  NimBLEDevice::init(name.c_str());
  NimBLEDevice::setSecurityAuth(true, false, true);  // bonding, Just Works, LE Secure Connections
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCb());
  ble_hid_begin(server);
  server->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->setName(name.c_str());
  adv->setAppearance(0x03C0);  // generic HID: the phone lists it in Settings > Bluetooth
  adv->addServiceUUID(NimBLEUUID((uint16_t)0x1812));
  adv->enableScanResponse(true);
  adv->start();
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c != '\n') {
      if (c != '\r' && line.length() < 600) line += c;
      continue;
    }
    line.trim();
    if (line.length()) {
      int sp = line.indexOf(' ');
      String id = sp < 0 ? line : line.substring(0, sp);
      String rest = sp < 0 ? "" : line.substring(sp + 1);
      rest.trim();
      Serial.println(id + " " + run(rest));
    }
    line = "";
  }
  delay(1);
}
