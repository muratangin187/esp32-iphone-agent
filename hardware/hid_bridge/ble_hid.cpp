// BLE HID peripheral, see ble_hid.h. Host: h2zero NimBLE-Arduino 2.5.x.
#include "ble_hid.h"
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include "ble_link.h"

enum { RID_KEYBOARD = 1, RID_MOUSE = 2, RID_CONSUMER = 3 };

static uint8_t REPORT_MAP[] = {
  // Keyboard, report id 1: modifiers, reserved, 5 LEDs (out), 6 keys
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, RID_KEYBOARD,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
  0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
  0xC0,
  // Mouse, report id 2: buttons(5), x, y, wheel, pan (relative, signed byte)
  0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, RID_MOUSE, 0x09, 0x01, 0xA1, 0x00,
  0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x81, 0x03,
  0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
  0x05, 0x0C, 0x0A, 0x38, 0x02, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
  0xC0, 0xC0,
  // Consumer control, report id 3: one 16-bit usage (AC Home for the iOS home screen)
  0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, RID_CONSUMER,
  0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
  0xC0,
};

// ASCII 0x20..0x7E -> HID usage, bit 7 = needs shift
static const uint8_t ASCII_HID[95] = {
  0x2C, 0x9E, 0xB4, 0xA0, 0xA1, 0xA2, 0xA4, 0x34, 0xA6, 0xA7, 0xA5, 0xAE, 0x36, 0x2D, 0x37, 0x38,  //  !"#$%&'()*+,-./
  0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0xB3, 0x33, 0xB6, 0x2E, 0xB7, 0xB8,  // 0-9 :;<=>?
  0x9F, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x92,  // @A-O
  0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x2F, 0x31, 0x30, 0xA3, 0xAD,  // P-Z [\]^_
  0x35, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12,  // `a-o
  0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0xAF, 0xB1, 0xB0, 0xB5,        // p-z {|}~
};

static NimBLECharacteristic *kbChar, *msChar, *consumerChar;
// Every central that discovers this service may subscribe to its reports: the phone (the one we want), but also
// a Linux host, whose BlueZ binds any HID service it finds. A connection that talks to the app side (cmd writes,
// reply/status/video subscriptions) is an app peer and never gets HID reports: the target is a subscriber that
// is not one. Connection handles are small integers (NimBLE: at most 3 connections).
const int MAX_CONN = 8;
static volatile bool hidSub[MAX_CONN];    // subscribed to any HID input report
static volatile bool mouseSub[MAX_CONN];  // subscribed to the mouse report: it can receive pointer moves
static uint8_t buttons, mods, keys[6];

class HidCb : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &ci, uint16_t sub) override {
    uint16_t h = ci.getConnHandle();
    if (h >= MAX_CONN) return;
    if (sub & 1) hidSub[h] = true;
    if (c == msChar) mouseSub[h] = sub & 1;
  }
};
static HidCb hidCb;

void ble_hid_begin(NimBLEServer *server) {
  auto *hid = new NimBLEHIDDevice(server);
  kbChar = hid->getInputReport(RID_KEYBOARD);
  msChar = hid->getInputReport(RID_MOUSE);
  consumerChar = hid->getInputReport(RID_CONSUMER);
  for (auto *c : {kbChar, msChar, consumerChar}) c->setCallbacks(&hidCb);
  hid->setManufacturer("ESP32 Agent");
  hid->setPnp(0x02, 0xe502, 0xa111, 0x0210);
  hid->setHidInfo(0x00, 0x01);
  hid->setReportMap(REPORT_MAP, sizeof REPORT_MAP);
  hid->setBatteryLevel(100);
}

static int target() {
  for (int h = 0; h < MAX_CONN; h++)
    if (mouseSub[h] && !ble_is_app_conn(h)) return h;
  return -1;
}

void ble_hid_on_disconnect(uint16_t conn) {
  if (conn >= MAX_CONN) return;
  hidSub[conn] = mouseSub[conn] = false;
  if (target() < 0) {
    buttons = mods = 0;
    memset(keys, 0, sizeof keys);
  }
}

bool ble_hid_peer(uint16_t conn) { return conn < MAX_CONN && hidSub[conn]; }
bool ble_hid_ready() { return target() >= 0; }

// A report notify() refused (the BLE queue was full) is retried: a lost pointer report is a tap off target.
static bool send(NimBLECharacteristic *c, const uint8_t *r, size_t n) {
  int h = target();
  if (h < 0 || !c) return false;
  for (int tries = 0; tries < 10; tries++) {
    if (c->notify(r, n, h)) return true;
    delay(2);
  }
  return false;
}

void ble_hid_mouse_move(int dx, int dy, int wheel, int pan) {
  uint8_t r[5] = {buttons, (uint8_t)(int8_t)constrain(dx, -127, 127), (uint8_t)(int8_t)constrain(dy, -127, 127),
                  (uint8_t)(int8_t)constrain(wheel, -127, 127), (uint8_t)(int8_t)constrain(pan, -127, 127)};
  if (ble_hid_ready()) send(msChar, r, sizeof r);
}

void ble_hid_mouse_buttons(uint8_t b) {
  buttons = b;
  ble_hid_mouse_move(0, 0);
}

static void sendKeys() {
  uint8_t r[8] = {mods, 0, keys[0], keys[1], keys[2], keys[3], keys[4], keys[5]};
  if (ble_hid_ready()) send(kbChar, r, sizeof r);
}

void ble_hid_press(uint8_t usage) {
  if (usage >= 0xE0) {
    mods |= 1 << (usage - 0xE0);
  } else {
    for (int i = 0; i < 6; i++) {
      if (keys[i] == usage) break;
      if (!keys[i]) { keys[i] = usage; break; }
    }
  }
  sendKeys();
}

void ble_hid_release_all() {
  mods = 0;
  memset(keys, 0, sizeof keys);
  sendKeys();
}

void ble_hid_type(const String &text) {
  for (size_t i = 0; i < text.length(); i++) {
    char c = text[i];
    if (c < 0x20 || c > 0x7E) continue;
    uint8_t v = ASCII_HID[c - 0x20];
    if (v & 0x80) ble_hid_press(HID_LSHIFT);
    ble_hid_press(v & 0x7F);
    delay(8);
    ble_hid_release_all();
    delay(8);
  }
}

void ble_hid_consumer(uint16_t usage) {
  uint8_t r[2] = {(uint8_t)(usage & 0xFF), (uint8_t)(usage >> 8)};
  if (ble_hid_ready()) send(consumerChar, r, sizeof r);
}

void ble_hid_release_everything() {
  buttons = 0;
  ble_hid_mouse_move(0, 0);
  ble_hid_release_all();
}

bool ble_hid_key_usage(const String &k, uint8_t &usage, bool &shift) {
  shift = false;
  usage = 0;
  if (k == "enter") usage = 0x28;
  else if (k == "esc") usage = 0x29;
  else if (k == "tab") usage = 0x2B;
  else if (k == "backspace") usage = 0x2A;
  else if (k == "space") usage = 0x2C;
  else if (k == "up") usage = 0x52;
  else if (k == "down") usage = 0x51;
  else if (k == "left") usage = 0x50;
  else if (k == "right") usage = 0x4F;
  else if (k == "cmd") usage = HID_LGUI;
  else if (k == "ctrl") usage = HID_LCTRL;
  else if (k == "alt") usage = HID_LALT;
  else if (k == "shift") usage = HID_LSHIFT;
  else if (k.length() == 1 && k[0] >= 0x20 && k[0] <= 0x7E) {
    uint8_t v = ASCII_HID[k[0] - 0x20];
    shift = v & 0x80;
    usage = v & 0x7F;
  }
  return usage != 0;
}
