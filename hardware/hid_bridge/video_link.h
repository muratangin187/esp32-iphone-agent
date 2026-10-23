// Mirrored H.264 from AirPlay to BLE: keeps the current GOP in PSRAM and sends it on `snap`,
// or every access unit while `stream on`. Also runs `bench` and computes the video stats.
#pragma once
#include <Arduino.h>

bool video_begin();
void video_on_config(const uint8_t *sps_pps, size_t len);  // AirPlay callbacks (mirror task)
void video_on_au(const uint8_t *au, size_t len, bool idr);
// `serial` = the request came over USB serial: video and the reply go there instead of BLE. On serial a video
// frame is one access unit: a header line "video <flags> <ts_ms> <len>\n" followed by <len> raw bytes (Annex-B).
// flags are the BLE ones (bit2 IDR, bit3 CONFIG, bit5 SNAP).
void video_stream(bool on, bool serial = false);
void video_snap(const String &id, bool serial = false);  // replies "<id> ok" when the GOP is sent
void serial_line(const String &line);            // one text line on USB serial, never inside a video frame
void video_bench(const String &id, uint32_t kb);  // replies "<id> ok <ms>"

struct VideoStats {
  uint32_t kbps, idr_ms, gop_bytes;
  float fps;
};
VideoStats video_stats();
