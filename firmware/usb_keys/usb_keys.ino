// Monet USB keys: a Waveshare ESP32-S3-Matrix plugged into the TVIP box's USB port,
// where it is an ordinary USB keyboard + media-key device. The Pi sends it key presses
// over Bluetooth LE (both ends are ours: no closed Bluetooth stack to lose a pairing).
// Replaces the fake Bluetooth remote (capture/bt_remote.py stays as fallback).
//
// The board only presses raw HID codes; key names live on the Pi (capture/remote_keys.py,
// the same table the Bluetooth remote uses).
//
// BLE (name "MonetKeys"), service 6d6f6e65-7400-4b65-7973-000000000001:
//   ...0002 press  write:  kind ('k' keyboard / 'c' consumer), code u16 LE, hold_ms u16 LE, token
//   ...0003 status read:   JSON {usb, presses, rejected, uptime_s}
// Writes without the right token are ignored (token = the Pi's relay token, set over USB).
//
// USB serial setup (from the Pi): token <token> | status | restart
//
// Build (Arduino CLI, esp32 core 3.x); USB mode must be TinyUSB so it can be a keyboard:
//   arduino-cli compile --fqbn esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=4M firmware/usb_keys
// Update from the Pi (board plugged into the Pi): stream/../firmware/flash.sh

#include <NimBLEDevice.h>
#include <Preferences.h>
#include <nvs.h>
#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include <Adafruit_NeoPixel.h>

#define SVC_UUID    "6d6f6e65-7400-4b65-7973-000000000001"
#define PRESS_UUID  "6d6f6e65-7400-4b65-7973-000000000002"
#define STATUS_UUID "6d6f6e65-7400-4b65-7973-000000000003"

// Waveshare ESP32-S3-Matrix: 8x8 WS2812B on GPIO 14, RGB order. Waveshare warns the board
// gets hot at high brightness: only the middle 2x2 pixels, dim.
#define LED_PIN 14
#define LED_COUNT 64
static const uint8_t CENTER[] = {27, 28, 35, 36};

USBHIDKeyboard keyboard;
USBHIDConsumerControl consumer;
Preferences prefs;
Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_RGB + NEO_KHZ800);
NimBLECharacteristic* statusChr;

String token;
volatile bool usbUp = false, bleUp = false;
volatile uint32_t presses = 0, rejected = 0;
unsigned long flashUntil = 0;

struct Press { char kind; uint16_t code, hold; };
QueueHandle_t pressQueue;   // BLE callback -> loop(): keep the BLE task free of delays

// Only touch the LEDs when the colour changes (each 64-LED update blocks ~2 ms).
void show(uint8_t r, uint8_t g, uint8_t b) {
  static uint32_t last = 0xFFFFFFFF;
  uint32_t c = (uint32_t)r << 16 | (uint32_t)g << 8 | b;
  if (c == last) return;
  last = c;
  leds.clear();
  for (uint8_t i : CENTER) leds.setPixelColor(i, leds.Color(r, g, b));
  leds.show();
}

// red = no USB host (not in the box / box asleep), blue = USB but the Pi isn't connected,
// green = ready, white blink = key sent
void statusLed() {
  if (millis() < flashUntil) return;
  if (!usbUp) show(12, 0, 0);
  else if (!bleUp) show(0, 0, 14);
  else show(0, 10, 0);
}

String statusJson() {
  return String("{\"usb\":") + (usbUp ? "true" : "false") + ",\"ble\":" + (bleUp ? "true" : "false") +
         ",\"presses\":" + presses + ",\"rejected\":" + rejected + ",\"uptime_s\":" + millis() / 1000 + "}";
}

void usbEvent(void*, esp_event_base_t base, int32_t id, void*) {
  if (base != ARDUINO_USB_EVENTS) return;
  if (id == ARDUINO_USB_STARTED_EVENT || id == ARDUINO_USB_RESUME_EVENT) usbUp = true;
  if (id == ARDUINO_USB_STOPPED_EVENT || id == ARDUINO_USB_SUSPEND_EVENT) usbUp = false;
}

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo&) override { bleUp = true; }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    bleUp = NimBLEDevice::getServer()->getConnectedCount() > 0;
    NimBLEDevice::startAdvertising();
  }
};

class PressCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo&) override {
    std::string v = c->getValue();
    if (v.size() < 5 || !token.length() || v.substr(5) != std::string(token.c_str()) || (v[0] != 'k' && v[0] != 'c')) {
      rejected++;
      return;
    }
    Press p{v[0], (uint16_t)((uint8_t)v[1] | (uint8_t)v[2] << 8), (uint16_t)((uint8_t)v[3] | (uint8_t)v[4] << 8)};
    xQueueSend(pressQueue, &p, 0);
  }
};

class StatusCallbacks : public NimBLECharacteristicCallbacks {
  void onRead(NimBLECharacteristic* c, NimBLEConnInfo&) override { c->setValue(statusJson().c_str()); }
};

void serialCommand(String line) {
  line.trim();
  if (line.startsWith("token ")) {
    token = line.substring(6); prefs.putString("token", token);
    Serial.println("OK token");
  } else if (line == "status") {
    Serial.println(statusJson());
  } else if (line == "restart") {
    Serial.println("OK restart"); delay(100); ESP.restart();
  } else if (line.length()) {
    Serial.println("ERR unknown");
  }
}

void setup() {
  leds.begin();
  leds.setBrightness(255);   // colours above are already dim
  show(0, 0, 0);

  USB.onEvent(usbEvent);
  USB.productName("Monet Keys");
  USB.manufacturerName("Monet");
  keyboard.begin();
  consumer.begin();
  USB.begin();
  Serial.begin(115200);

  prefs.begin("keys", false);
  token = prefs.getString("token", "");
  // No Wi-Fi in this firmware. Wipe what earlier Wi-Fi firmware left in flash: our copy of
  // the network name/password and the Wi-Fi driver's own saved config.
  prefs.remove("ssid"); prefs.remove("pass"); prefs.remove("tx"); prefs.remove("host");
  nvs_handle_t h;
  if (nvs_open("nvs.net80211", NVS_READWRITE, &h) == ESP_OK) { nvs_erase_all(h); nvs_commit(h); nvs_close(h); }

  pressQueue = xQueueCreate(16, sizeof(Press));

  NimBLEDevice::init("MonetKeys");
  NimBLEDevice::setPower(3);   // dBm; the Pi sits near the box
  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  NimBLEService* svc = server->createService(SVC_UUID);
  svc->createCharacteristic(PRESS_UUID, NIMBLE_PROPERTY::WRITE)->setCallbacks(new PressCallbacks());
  statusChr = svc->createCharacteristic(STATUS_UUID, NIMBLE_PROPERTY::READ);
  statusChr->setCallbacks(new StatusCallbacks());
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_UUID);
  adv->setName("MonetKeys");
  adv->start();
}

void loop() {
  static String buf;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { serialCommand(buf); buf = ""; }
    else if (buf.length() < 200) buf += ch;
  }
  Press p;
  if (xQueueReceive(pressQueue, &p, 0) == pdTRUE && usbUp) {
    uint16_t hold = constrain(p.hold, 20, 2000);
    show(24, 24, 24);
    if (p.kind == 'k') { keyboard.pressRaw(p.code); delay(hold); keyboard.releaseRaw(p.code); }
    else { consumer.press(p.code); delay(hold); consumer.release(); }
    presses++;
    flashUntil = millis() + 80;
  }
  statusLed();
  delay(2);
}
