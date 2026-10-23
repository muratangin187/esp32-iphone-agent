// Mirrored H.264 from AirPlay to BLE, see video_link.h.
#include "video_link.h"
#include <esp_heap_caps.h>
#include "ble_link.h"
#include "src/video/gop.h"

// GOP since the latest IDR; overflow drops it until the next IDR. A still screen gets an IDR only every
// few minutes, so room for ~27 min of AUs at 5 fps (P-frames of a still screen are tiny).
const size_t GOP_CAP = 4 * 1024 * 1024;
const int MAX_AUS = 8192;
const size_t SCRATCH_CAP = 512 * 1024;   // largest access unit we forward
const size_t MAX_CHUNK = 512;

struct Req {
  char id[16];
  uint32_t kb;
  bool bench;
  bool serial;
};

static gop_buf gop;
static SemaphoreHandle_t lock;
static uint8_t *scratch;
static QueueHandle_t reqs;
static TaskHandle_t task;
static volatile bool streaming, streamSerial;
static SemaphoreHandle_t serialLock;
static uint16_t seq;
static uint8_t chunk[VCHUNK_HDR + MAX_CHUNK];
static uint32_t winStart, winBytes, winFrames, lastIdr;
static volatile uint32_t statKbps, statIdrMs, statAt;
static volatile float statFps;

void serial_line(const String &line) {
  xSemaphoreTake(serialLock, portMAX_DELAY);
  Serial.println(line);
  xSemaphoreGive(serialLock);
}

// One access unit on USB serial as a single frame (see video_link.h). Holds the serial lock for the whole frame
// so a reply line cannot land inside it; false if the host stopped reading.
static bool sendAuSerial(const uint8_t *p, size_t n, uint8_t flags, uint32_t ts) {
  if (!Serial) return false;
  xSemaphoreTake(serialLock, portMAX_DELAY);
  Serial.printf("video %u %lu %u\n", flags, (unsigned long)ts, (unsigned)n);
  bool ok = true;
  for (size_t off = 0; off < n && ok;) {
    size_t w = Serial.write(p + off, min((size_t)4096, n - off));
    if (!w) ok = false;
    off += w;
  }
  xSemaphoreGive(serialLock);
  return ok;
}

// One access unit as notifies. A failed chunk still consumes its seq, so the app sees a gap.
static bool sendAu(const uint8_t *p, size_t n, uint8_t flags, uint32_t ts, bool serial = false) {
  if (serial) return sendAuSerial(p, n, flags, ts);
  size_t room = min(ble_video_room(), MAX_CHUNK);
  if (room < 5) return false;
  vchunk c;
  vchunk_begin(&c, p, n, flags, ts);
  for (size_t len; (len = vchunk_next(&c, chunk, room, seq)) > 0;) {
    bool ok = ble_video_send(chunk, len);
    seq++;
    if (!ok) return false;
  }
  return true;
}

// Copies AU i of generation gen into scratch; false if the GOP moved on.
static bool copyAu(uint32_t gen, int i, gop_au *au) {
  xSemaphoreTake(lock, portMAX_DELAY);
  bool ok = gop.gen == gen && i < gop.n && gop.aus[i].len <= SCRATCH_CAP;
  if (ok) {
    *au = gop.aus[i];
    memcpy(scratch, gop.arena + au->off, au->len);
  }
  xSemaphoreGive(lock);
  return ok;
}

// Snapshot of the GOP state: generation, AU count, SPS/PPS.
static bool snapshot(uint32_t *gen, int *n, uint8_t *cfg, size_t *cfg_len, uint32_t *cfg_ts) {
  xSemaphoreTake(lock, portMAX_DELAY);
  *gen = gop.gen;
  *n = gop.wait_idr ? 0 : gop.n;
  *cfg_len = gop.cfg_len;
  *cfg_ts = gop.cfg_ts;
  memcpy(cfg, gop.cfg, gop.cfg_len);
  xSemaphoreGive(lock);
  return *n > 0 && *cfg_len > 0;
}

static String doSnap(bool serial) {
  static uint8_t cfg[GOP_CFG_MAX];
  if (serial ? !Serial : !ble_video_room()) return serial ? "err no serial host" : "err video not subscribed";
  for (int attempt = 0; attempt < 3; attempt++) {
    uint32_t gen, cfg_ts;
    size_t cfg_len;
    int n;
    if (!snapshot(&gen, &n, cfg, &cfg_len, &cfg_ts)) return "err no video (start screen mirroring)";
    if (!sendAu(cfg, cfg_len, VF_CONFIG | VF_SNAP, cfg_ts, serial)) return "err send failed";
    bool complete = true;
    for (int i = 0; i < n && complete; i++) {
      gop_au au;
      complete = copyAu(gen, i, &au);
      if (complete && !sendAu(scratch, au.len, VF_SNAP | (au.idr ? VF_IDR : 0), au.ts, serial)) return "err send failed";
    }
    if (complete) return "ok";  // else a new IDR replaced the GOP mid-send: start over with it
  }
  return "err video changing too fast";
}

static String doBench(uint32_t kb) {
  size_t room = min(ble_video_room(), MAX_CHUNK);
  if (!room) return "err video not subscribed";
  uint32_t start = millis();
  memset(chunk + VCHUNK_HDR, 0xA5, room);
  for (size_t left = kb * 1024; left;) {
    size_t n = min(left, room);
    chunk[0] = (uint8_t)seq;
    chunk[1] = (uint8_t)(seq >> 8);
    chunk[2] = VF_BENCH;
    bool ok = ble_video_send(chunk, VCHUNK_HDR + n);
    seq++;
    if (!ok) return "err ble send failed";
    left -= n;
  }
  return "ok " + String(millis() - start);
}

// Continuous mode: send every AU once, catching up from the GOP's IDR when streaming starts (P-frames
// can't be skipped: each needs the one before, and the next IDR may be minutes away). After a failed
// send, start over from the GOP's IDR.
static void streamStep() {
  static uint32_t txGen = UINT32_MAX;
  static int next;
  static uint8_t cfg[GOP_CFG_MAX];
  bool serial = streamSerial;
  if (!streaming || (serial ? !Serial : !ble_video_room())) {
    txGen = UINT32_MAX;  // resend config + whole GOP when streaming resumes
    return;
  }
  uint32_t gen, cfg_ts;
  size_t cfg_len;
  int n;
  if (!snapshot(&gen, &n, cfg, &cfg_len, &cfg_ts)) return;
  if (gen != txGen) {
    txGen = gen;
    next = 0;
    if (!sendAu(cfg, cfg_len, VF_CONFIG, cfg_ts, serial)) { txGen = UINT32_MAX; return; }
  }
  for (gop_au au; next < n && copyAu(gen, next, &au); next++)
    if (!sendAu(scratch, au.len, au.idr ? VF_IDR : 0, au.ts, serial)) { txGen = UINT32_MAX; return; }
}

static void videoTask(void *) {
  for (;;) {
    Req r;
    if (xQueueReceive(reqs, &r, 0) == pdTRUE) {
      String reply = String(r.id) + " " + (r.bench ? doBench(r.kb) : doSnap(r.serial));
#if DEBUG_SERIAL
      Serial.println("video: " + reply);  // the reply only goes over BLE; mirror it for serial-driven tests
#endif
      if (r.serial) serial_line(reply);
      else ble_reply(reply);
    }
    streamStep();
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
  }
}

bool video_begin() {
  uint8_t *arena = (uint8_t *)heap_caps_malloc(GOP_CAP, MALLOC_CAP_SPIRAM);
  gop_au *aus = (gop_au *)heap_caps_malloc(MAX_AUS * sizeof(gop_au), MALLOC_CAP_SPIRAM);
  scratch = (uint8_t *)heap_caps_malloc(SCRATCH_CAP, MALLOC_CAP_SPIRAM);
  if (!arena || !aus || !scratch) return false;
  gop_init(&gop, arena, GOP_CAP, aus, MAX_AUS);
  lock = xSemaphoreCreateMutex();
  serialLock = xSemaphoreCreateMutex();
  reqs = xQueueCreate(4, sizeof(Req));
  return xTaskCreatePinnedToCore(videoTask, "video", 6 * 1024, NULL, 2, &task, 1) == pdPASS;
}

void video_on_config(const uint8_t *sps_pps, size_t len) {
  xSemaphoreTake(lock, portMAX_DELAY);
  gop_set_config(&gop, sps_pps, len, millis());
  xSemaphoreGive(lock);
}

void video_on_au(const uint8_t *au, size_t len, bool idr) {
  uint32_t now = millis();
  winBytes += len;
  winFrames++;
  if (idr) {
    if (lastIdr) statIdrMs = now - lastIdr;
    lastIdr = now;
  }
  if (now - winStart >= 1000) {
    statKbps = winBytes * 8 / (now - winStart);
    statFps = winFrames * 1000.0f / (now - winStart);
    statAt = now;
    winBytes = winFrames = 0;
    winStart = now;
  }
  xSemaphoreTake(lock, portMAX_DELAY);
  gop_push(&gop, au, len, idr, now);
  xSemaphoreGive(lock);
  xTaskNotifyGive(task);
}

void video_stream(bool on, bool serial) {
  streamSerial = serial;
  streaming = on;
}

static void request(const String &id, bool bench, uint32_t kb, bool serial) {
  Req r = {{0}, kb, bench, serial};
  strncpy(r.id, id.c_str(), sizeof r.id - 1);
  if (xQueueSend(reqs, &r, 0) != pdTRUE) {
    if (serial) serial_line(id + " err busy");
    else ble_reply(id + " err busy");
  }
}

void video_snap(const String &id, bool serial) { request(id, false, 0, serial); }
void video_bench(const String &id, uint32_t kb) { request(id, true, kb, false); }

VideoStats video_stats() {
  bool fresh = millis() - statAt < 3000;  // mirroring stopped: report zeros, not stale numbers
  xSemaphoreTake(lock, portMAX_DELAY);
  uint32_t gopBytes = gop.used;
  xSemaphoreGive(lock);
  return {fresh ? statKbps : 0, statIdrMs, gopBytes, fresh ? statFps : 0};
}
