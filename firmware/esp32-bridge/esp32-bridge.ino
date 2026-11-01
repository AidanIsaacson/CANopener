// CANopener ESP32 bridge (ESP32-WROOM-32UE). Arduino sketch, stage 6. NOT YET COMPILED.
//
// Receives readings from wireless satellite meters over ESP-NOW and prints them to the Pi on
// UART0 (GPIO1 TX0 / GPIO3 RX0 <-> Pi GPIO15 RXD / GPIO14 TXD, 115200 8N1) in the meter line
// protocol (canopener/core/meters.py). The Pi reads it as an ordinary serial meter: the "esp32"
// installed option.
//
// Target: Arduino-ESP32 core 3.x (ESP-IDF 5). The receive callback signature changed in 3.0;
// on core 2.x it is (const uint8_t *mac, const uint8_t *data, int len).
//
// Later: the Bluetooth gateway (Classic + BLE, DESIGN.md), and WiFi log offload.
//
// UART0 is also the flashing port, so the boot ROM prints a banner at reset. The Pi ignores
// lines that aren't JSON, so it does no harm.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const char *VERSION = "0.1-sketch";
static const uint8_t WIFI_CHANNEL = 1;     // satellites and bridge must agree

// Satellite packet, version 1 (firmware/esp32-bridge/README.md). Little-endian, packed.
struct __attribute__((packed)) SatPacket {
  uint8_t magic[2];       // 'C', 'O'
  uint8_t version;        // 1
  uint8_t count;          // readings in this packet, 1-8
  uint32_t seq;           // increments per packet: the bridge reports gaps
  uint32_t t_ms;          // satellite's own clock
  struct __attribute__((packed)) {
    char id[4];           // channel id, NUL-padded: "v1", "v2", "i"
    float value;          // NaN = no reading
    uint8_t flag;         // 0 ok, 1 over range, 2 low battery
  } r[8];
};

// Received in the WiFi task; printed from loop() so Serial is only used from one place.
static const int QUEUE_LEN = 16;
struct Item { uint8_t mac[6]; SatPacket p; int len; };
static Item queue_[QUEUE_LEN];
static volatile int head = 0, tail = 0;

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len < 12 || data[0] != 'C' || data[1] != 'O') return;
  int next = (head + 1) % QUEUE_LEN;
  if (next == tail) return;                  // full: drop (the seq gap shows it)
  memcpy(queue_[head].mac, info->src_addr, 6);
  memcpy(&queue_[head].p, data, min(len, (int)sizeof(SatPacket)));
  queue_[head].len = len;
  head = next;
}

static void hello() {
  Serial.printf("{\"hello\": \"canopener-meter\", \"name\": \"Wireless meters\", \"version\": \"%s\", "
                "\"options\": [\"espnow\"], \"channels\": []}\n", VERSION);
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("{\"status\": \"error\", \"text\": \"ESP-NOW failed to start\"}");
    return;
  }
  esp_now_register_recv_cb(on_recv);
  hello();
}

static String line;

void loop() {
  while (tail != head) {
    Item &it = queue_[tail];
    char sat[7];
    snprintf(sat, sizeof sat, "%02x%02x%02x", it.mac[3], it.mac[4], it.mac[5]);  // channel prefix
    int n = min((int)it.p.count, 8);
    for (int i = 0; i < n; i++) {
      char id[5] = {0};
      memcpy(id, it.p.r[i].id, 4);
      if (isnan(it.p.r[i].value)) {
        Serial.printf("{\"ch\": \"%s_%s\", \"v\": null, \"flag\": \"%s\"}\n", sat, id,
                      it.p.r[i].flag == 1 ? "OL" : "no reading");
      } else {
        Serial.printf("{\"ch\": \"%s_%s\", \"v\": %.4f%s}\n", sat, id, it.p.r[i].value,
                      it.p.r[i].flag == 2 ? ", \"flag\": \"low battery\"" : "");
      }
    }
    tail = (tail + 1) % QUEUE_LEN;
  }
  // Commands from the Pi: only {"cmd": "hello"} for now.
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (line.indexOf("\"hello\"") >= 0) hello();
      line = "";
    } else if (line.length() < 256) {
      line += c;
    }
  }
  delay(2);
}
