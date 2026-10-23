// BLE HID mouse + keyboard + consumer control, served from the board's own GATT server (see ble_link.cpp).
// For phones that cannot host the board over USB (Lightning iPhones without a powered camera adapter):
// the phone pairs with the board in Settings > Bluetooth like any Bluetooth mouse. USB HID stays the
// default whenever the phone has the board mounted. Report map and key table: adapted from the legacy
// hid_bridge_ble_c3 sketch (same project, MIT).
#pragma once
#include <Arduino.h>

class NimBLEServer;

#define HID_LCTRL 0xE0  // modifier pseudo-usages (>= 0xE0 are HID modifier bits)
#define HID_LSHIFT 0xE1
#define HID_LALT 0xE2
#define HID_LGUI 0xE3
#define HID_BTN_LEFT 1

void ble_hid_begin(NimBLEServer *server);  // create the HID services; call before server->start()
void ble_hid_on_disconnect(uint16_t conn);
bool ble_hid_peer(uint16_t conn);          // this connection subscribed to HID reports (the phone, or a Linux host's BlueZ)
bool ble_hid_ready();                      // the phone is subscribed to the mouse report

void ble_hid_mouse_move(int dx, int dy, int wheel = 0, int pan = 0);  // each -127..127
void ble_hid_mouse_buttons(uint8_t buttons);                          // absolute button state
void ble_hid_press(uint8_t usage);                                    // key (< 0xE0) or modifier (>= 0xE0)
void ble_hid_release_all();                                           // keys and modifiers
void ble_hid_type(const String &text);                                // printable ASCII
void ble_hid_consumer(uint16_t usage);                                // press (usage) or release (0)
bool ble_hid_key_usage(const String &name, uint8_t &usage, bool &shift);  // "enter", "cmd", "a", "A", ...
void ble_hid_release_everything();
