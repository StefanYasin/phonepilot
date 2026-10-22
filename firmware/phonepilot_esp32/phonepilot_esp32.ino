/*
 * PhonePilot firmware for ESP32.
 *
 * The board is three things at once:
 *   1. a Bluetooth LE HID keyboard the iPhone pairs with
 *   2. a physical approve / panic button
 *   3. a small HTTP control API the fleet manager talks to
 *
 * The phone side is iOS Shortcuts. It polls /api/v1/next, performs the app
 * level action, and reports back to /api/v1/result. Nothing on this board
 * posts to a social network by itself.
 *
 * Libraries: ESP32-BLE-Keyboard, ArduinoJson 7, WebServer (bundled).
 * Board: esp32:esp32:esp32 (classic ESP32 with BLE).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <BleKeyboard.h>
#include <ESPmDNS.h>

#include "config.h"

#ifndef DEVICE_NAME
#define DEVICE_NAME "PhonePilot"
#endif
#ifndef BUTTON_PIN
#define BUTTON_PIN 0
#endif
#ifndef LED_PIN
#define LED_PIN 2
#endif
#ifndef AP_FALLBACK_PASSWORD
#define AP_FALLBACK_PASSWORD "phonepilot"
#endif

#define FIRMWARE_VERSION "0.1.0"
#define MAX_JOBS 8
#define TEXT_LIMIT 512

BleKeyboard bleKeyboard(DEVICE_NAME, "PhonePilot", 100);
WebServer server(80);

struct Job {
  uint32_t id = 0;
  String type;
  String name;
  String platform;
  String media;
  String caption;
  String target;
  bool delivered = false;
  unsigned long created_ms = 0;
};

Job jobs[MAX_JOBS];
uint32_t next_job_id = 1;
unsigned long approval_until = 0;
unsigned long button_press_count = 0;
unsigned long last_button_change = 0;
bool last_button_level = HIGH;
unsigned long button_down_at = 0;
bool button_held = false;
String last_result = "none";
unsigned long boot_ms = 0;

bool approvalArmed();

// ---------------------------------------------------------------- helpers

bool authorized() {
  if (strlen(API_TOKEN) == 0) {
    return true;  // token disabled, boot log warns loudly
  }
  return server.hasHeader("X-PhonePilot-Token") &&
         server.header("X-PhonePilot-Token") == API_TOKEN;
}

void sendJson(int code, JsonDocument &doc) {
  String body;
  serializeJson(doc, body);
  server.sendHeader("Cache-Control", "no-store");
  server.send(code, "application/json", body);
}

void sendError(int code, const char *message) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = message;
  sendJson(code, doc);
}

bool requireAuth() {
  if (!authorized()) {
    sendError(401, "bad or missing token");
    return false;
  }
  return true;
}

int freeSlot() {
  for (int i = 0; i < MAX_JOBS; i++) {
    if (jobs[i].id == 0) {
      return i;
    }
  }
  return -1;
}

int jobById(uint32_t id) {
  for (int i = 0; i < MAX_JOBS; i++) {
    if (jobs[i].id == id) {
      return i;
    }
  }
  return -1;
}

void clearJobs() {
  for (int i = 0; i < MAX_JOBS; i++) {
    jobs[i] = Job();
  }
}

uint32_t enqueue(const String &type, const String &name, const String &platform,
                 const String &media, const String &caption, const String &target) {
  int slot = freeSlot();
  if (slot < 0) {
    return 0;
  }
  Job &job = jobs[slot];
  job.id = next_job_id++;
  job.type = type;
  job.name = name;
  job.platform = platform;
  job.media = media;
  job.caption = caption;
  job.target = target;
  job.delivered = false;
  job.created_ms = millis();
  return job.id;
}

void jobToJson(const Job &job, JsonObject out, bool include_approval) {
  out["id"] = job.id;
  out["type"] = job.type;
  out["name"] = job.name;
  out["platform"] = job.platform;
  out["media"] = job.media;
  out["caption"] = job.caption;
  out["target"] = job.target;
  out["age_s"] = (millis() - job.created_ms) / 1000;
  if (include_approval) {
    out["approved"] = approvalArmed();
  }
}

void armApproval(unsigned long seconds) {
  approval_until = millis() + seconds * 1000UL;
  Serial.printf("approval armed for %lu s\n", seconds);
}

void panic() {
  approval_until = 0;
  clearJobs();
  if (bleKeyboard.isConnected()) {
    bleKeyboard.write(KEY_ESC);
  }
  Serial.println("panic: queue cleared");
}

// ---------------------------------------------------------------- handlers

void handleStatus() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  doc["ok"] = true;
  doc["device"] = DEVICE_NAME;
  doc["firmware"] = FIRMWARE_VERSION;
  doc["uptime_s"] = (millis() - boot_ms) / 1000;
  doc["ble_connected"] = bleKeyboard.isConnected();
  doc["wifi_rssi"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
  doc["ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
  doc["queue"] = 0;
  doc["approval_armed"] = approvalArmed();
  doc["button_presses"] = button_press_count;
  doc["last_result"] = last_result;

  int queued = 0;
  JsonArray items = doc["jobs"].to<JsonArray>();
  for (int i = 0; i < MAX_JOBS; i++) {
    if (jobs[i].id != 0) {
      queued++;
      JsonObject item = items.add<JsonObject>();
      item["id"] = jobs[i].id;
      item["type"] = jobs[i].type;
      item["delivered"] = jobs[i].delivered;
    }
  }
  doc["queue"] = queued;
  sendJson(200, doc);
}

void handleQueue() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  doc["ok"] = true;
  JsonArray items = doc["jobs"].to<JsonArray>();
  for (int i = 0; i < MAX_JOBS; i++) {
    if (jobs[i].id != 0) {
      JsonObject item = items.add<JsonObject>();
      jobToJson(jobs[i], item, true);
    }
  }
  sendJson(200, doc);
}

void handleNext() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  doc["ok"] = true;
  doc["approved"] = approvalArmed();
  for (int i = 0; i < MAX_JOBS; i++) {
    if (jobs[i].id != 0 && !jobs[i].delivered) {
      bool one_shot = jobs[i].type == "shortcut";
      jobs[i].delivered = true;
      JsonObject job = doc["job"].to<JsonObject>();
      jobToJson(jobs[i], job, true);
      if (one_shot) {
        jobs[i] = Job();  // shortcut jobs have no result to wait for
      }
      sendJson(200, doc);
      return;
    }
  }
  doc["job"] = nullptr;
  sendJson(200, doc);
}

void handleResult() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, server.arg("plain"));
  if (error) {
    sendError(400, "body must be JSON");
    return;
  }
  uint32_t id = doc["job_id"] | 0;
  const char *status = doc["status"] | "unknown";
  const char *detail = doc["detail"] | "";
  int slot = jobById(id);
  if (slot < 0) {
    sendError(404, "unknown job");
    return;
  }
  last_result = String(status) + ": " + String(detail);
  if (strcmp(status, "posted") == 0 || strcmp(status, "done") == 0) {
    jobs[slot] = Job();
    approval_until = 0;
  } else {
    jobs[slot].delivered = false;  // let the phone retry once the user is ready
  }
  JsonDocument reply;
  reply["ok"] = true;
  reply["job_id"] = id;
  sendJson(200, reply);
}

void handleKeys() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) {
    sendError(400, "body must be JSON");
    return;
  }
  const char *text = doc["text"] | "";
  bool enter = doc["enter"] | false;
  int delay_ms = doc["delay_ms"] | 5;
  if (strlen(text) == 0) {
    sendError(400, "text is required");
    return;
  }
  if (strlen(text) > TEXT_LIMIT) {
    sendError(413, "text too long");
    return;
  }
  if (!bleKeyboard.isConnected()) {
    sendError(409, "no BLE host connected");
    return;
  }
  delay_ms = constrain(delay_ms, 0, 50);
  for (size_t i = 0; i < strlen(text); i++) {
    bleKeyboard.print(text[i]);
    if (delay_ms > 0) {
      delay(delay_ms);
    }
  }
  if (enter) {
    bleKeyboard.write(KEY_RETURN);
  }
  JsonDocument reply;
  reply["ok"] = true;
  reply["sent"] = strlen(text);
  reply["enter"] = enter;
  sendJson(200, reply);
}

void handleShortcut() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) {
    sendError(400, "body must be JSON");
    return;
  }
  const char *name = doc["name"] | "";
  if (strlen(name) == 0) {
    sendError(400, "name is required");
    return;
  }
  uint32_t id = enqueue("shortcut", name, "", "", "", "");
  if (id == 0) {
    sendError(503, "job queue is full");
    return;
  }
  JsonDocument reply;
  reply["ok"] = true;
  reply["queued"] = true;
  reply["job_id"] = id;
  sendJson(200, reply);
}

void handlePost() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) {
    sendError(400, "body must be JSON");
    return;
  }
  const char *platform = doc["platform"] | "";
  const char *media = doc["media"] | "";
  const char *caption = doc["caption"] | "";
  const char *target = doc["target"] | "";
  if (strlen(platform) == 0 || strlen(media) == 0 || strlen(caption) == 0) {
    sendError(400, "platform, media and caption are required");
    return;
  }
  uint32_t id = enqueue("social_post", "", platform, media, caption, target);
  if (id == 0) {
    sendError(503, "job queue is full");
    return;
  }
  JsonDocument reply;
  reply["ok"] = true;
  reply["queued"] = true;
  reply["job_id"] = id;
  reply["requires_phone"] = true;
  sendJson(200, reply);
}

void handleApprove() {
  if (!requireAuth()) {
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) {
    sendError(400, "body must be JSON");
    return;
  }
  long seconds = doc["seconds"] | 90;
  seconds = constrain(seconds, 5L, 600L);
  armApproval((unsigned long)seconds);
  JsonDocument reply;
  reply["ok"] = true;
  reply["armed_for"] = seconds;
  sendJson(200, reply);
}

void handlePanic() {
  if (!requireAuth()) {
    return;
  }
  panic();
  JsonDocument reply;
  reply["ok"] = true;
  reply["cleared"] = true;
  sendJson(200, reply);
}

void handleRoot() {
  String page = "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>";
  page += "<title>" + String(DEVICE_NAME) + "</title>";
  page += "<body style='font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem'>";
  page += "<h1>" + String(DEVICE_NAME) + "</h1>";
  page += "<p>Firmware " FIRMWARE_VERSION ". BLE host: ";
  page += bleKeyboard.isConnected() ? "connected" : "not connected";
  page += ".</p><p>This board accepts commands from the PhonePilot fleet manager on your LAN.</p>";
  page += "<p><a href='/api/v1/status'>status JSON</a></p></body>";
  server.send(200, "text/html", page);
}

void handleNotFound() {
  sendError(404, "no such endpoint");
}

// ---------------------------------------------------------------- button

bool approvalArmed() {
  return approval_until != 0 && (long)(approval_until - millis()) > 0;
}

void serviceButton() {
  bool level = digitalRead(BUTTON_PIN);
  unsigned long now = millis();
  if (level != last_button_level) {
    last_button_change = now;
    last_button_level = level;
  }
  if (now - last_button_change < 40) {
    return;
  }
  if (level == LOW && !button_held) {
    button_held = true;
    button_down_at = now;
  }
  if (level == HIGH && button_held) {
    button_held = false;
    unsigned long held_for = now - button_down_at;
    if (held_for >= 1200) {
      panic();
    } else {
      button_press_count++;
      armApproval(90);
      digitalWrite(LED_PIN, HIGH);
    }
  }
}

// ---------------------------------------------------------------- setup

void connectWifi() {
  if (strlen(WIFI_SSID) == 0) {
    Serial.println("no WIFI_SSID configured, starting setup AP");
    WiFi.softAP(DEVICE_NAME, AP_FALLBACK_PASSWORD);
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("phonepilot");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("wifi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("wifi ready: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("wifi failed, starting setup AP");
    WiFi.mode(WIFI_AP);
    WiFi.softAP(DEVICE_NAME, AP_FALLBACK_PASSWORD);
    Serial.print("setup AP: ");
    Serial.println(WiFi.softAPIP());
  }
}

void setup() {
  boot_ms = millis();
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("PhonePilot firmware " FIRMWARE_VERSION);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  if (strlen(API_TOKEN) < 8) {
    Serial.println("WARNING: API_TOKEN is empty or short. Set it in config.h.");
  }

  bleKeyboard.begin();
  connectWifi();

  if (MDNS.begin("phonepilot")) {
    MDNS.addService("http", "tcp", 80);
  }

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/v1/status", HTTP_GET, handleStatus);
  server.on("/api/v1/queue", HTTP_GET, handleQueue);
  server.on("/api/v1/next", HTTP_GET, handleNext);
  server.on("/api/v1/result", HTTP_POST, handleResult);
  server.on("/api/v1/keys", HTTP_POST, handleKeys);
  server.on("/api/v1/shortcut", HTTP_POST, handleShortcut);
  server.on("/api/v1/post", HTTP_POST, handlePost);
  server.on("/api/v1/approve", HTTP_POST, handleApprove);
  server.on("/api/v1/panic", HTTP_POST, handlePanic);
  server.onNotFound(handleNotFound);
  server.begin();

  Serial.print("control API on http://");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.localIP() : WiFi.softAPIP());
}

void loop() {
  server.handleClient();
  serviceButton();

  if (WiFi.status() != WL_CONNECTED && strlen(WIFI_SSID) > 0) {
    static unsigned long last_retry = 0;
    if (millis() - last_retry > 15000) {
      last_retry = millis();
      WiFi.reconnect();
    }
  }

  if (!approvalArmed()) {
    digitalWrite(LED_PIN, LOW);
  } else {
    digitalWrite(LED_PIN, (millis() / 250) % 2);
  }

  if (!bleKeyboard.isConnected()) {
    static unsigned long last_note = 0;
    if (millis() - last_note > 30000) {
      last_note = millis();
      Serial.println("waiting for the iPhone to connect over BLE");
    }
  }

  delay(5);
}
