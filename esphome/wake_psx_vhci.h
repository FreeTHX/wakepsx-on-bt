/**
 * Project: esp-wakepsx-on-bt
 * Purpose: Low-level Bluetooth HCI injection for PlayStation Wake-on-BT.
 * Author: FreeTHX
 */

#pragma once

#include "esphome.h"
#include "esp_bt.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <atomic>
#include <cctype>
#include <cstring>
#include <string>

namespace wakepsx_on_bt {

static constexpr const char *TAG = "wakepsx_on_bt";

// Stack depth for the wake FreeRTOS task, in words (1 word = 4 bytes on ESP32 → 16 KB total).
// ESP-IDF recommends 4096 words minimum for tasks involving BT controller init + VHCI operations.
// Ref: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/bluetooth/controller_vhci.html
static constexpr uint32_t WAKE_TASK_STACK_WORDS = 4096;

// Settling time after esp_bt_controller_deinit() before calling esp_iface_mac_addr_set().
// The ESP-IDF does not specify a minimum — empirically determined to be sufficient.
static constexpr uint32_t BT_CONTROLLER_SETTLE_MS = 100;

// Warm-up delay after esp_vhci_host_register_callback() before sending the first HCI packet.
// VHCI mode exposes no "controller ready" event; this fixed delay covers internal BT controller
// initialisation. Based on ESP-IDF bt_hci_test example and empirical validation.
static constexpr uint32_t VHCI_WARMUP_MS = 200;

// Polling parameters for esp_vhci_host_check_send_available().
// Acts as a safety net if the VHCI_WARMUP_MS delay is not sufficient.
// Total maximum wait = VHCI_TX_POLL_INTERVAL_MS * VHCI_TX_POLL_MAX_CYCLES = 500 ms.
static constexpr uint32_t VHCI_TX_POLL_INTERVAL_MS = 10;
static constexpr int VHCI_TX_POLL_MAX_CYCLES = 50;

// Polling parameters for the HCI Connection Complete event after packet injection.
// Bluetooth Classic page timeout default = 5.12 s (640 × 8 ms slots, HCI spec Vol 4 §7.3.17).
// Total timeout = WAKE_POLL_INTERVAL_MS * WAKE_TIMEOUT_CYCLES = 6 s (adds ~1 s margin).
static constexpr uint32_t WAKE_POLL_INTERVAL_MS = 100;
static constexpr int WAKE_TIMEOUT_CYCLES = 60;

// How long the result status ("Success", "Page Timeout", etc.) is displayed before
// resetting to "Ready". Intentional UX delay — prevents rapid re-use.
static constexpr uint32_t WAKE_RESULT_DISPLAY_MS = 5000;

// HCI Create Connection packet type: all BR packet sizes enabled (DM1|DH1|DM3|DH3|DM5|DH5).
// EDR variants (2/3-DH*) are implicitly allowed (their "shall not use" bits are 0).
// SCO packets (HV1/HV2/HV3) are not set — not relevant for a wake connection.
// Ref: Bluetooth Core Spec Vol 4 Part E §7.1.5 — packet_type bitmask.
//      BlueZ hci.h: HCI_DM1=0x0008, HCI_DH1=0x0010, HCI_DM3=0x0400,
//                   HCI_DH3=0x0800, HCI_DM5=0x4000, HCI_DH5=0x8000.
static constexpr uint16_t HCI_BR_PACKET_TYPE = 0xCC18;

// Atomic: written from the BT VHCI callback (PRO_CPU) and read from wake_task (APP_CPU).
// volatile alone does not guarantee cross-core memory coherence on the dual-core ESP32.
// inline (C++17) gives a single definition across all translation units.
inline std::atomic<int> connection_status{-1};
// wake_mutex is created once in wake_psx() and never destroyed — intentional.
// Its lifetime matches the device lifetime: OTA updates trigger a full reboot,
// so vSemaphoreDelete() is never needed and would only add dead code.
inline SemaphoreHandle_t wake_mutex = nullptr;

// Target PSX MAC in little-endian byte order, set by wake_task before packet injection
// and read by notify_host_recv to filter Connection Complete events from other devices.
// Written once before the callback window; no concurrent writes possible (mutex-protected).
inline uint8_t g_psx_le[6] = {0};

// Callback triggered when the BT controller sends an HCI event packet back to the host.
// Handles two events:
//   0x03 — Connection Complete: terminal result of the page attempt.
//   0x0F — Command Status: controller rejected HCI_Create_Connection immediately (status ≠ 0).
//           Without this check, a rejection would silently spin for the full 6-second timeout.
static int notify_host_recv(uint8_t *data, uint16_t len) {
  if (data == nullptr || len < 2 || data[0] != 0x04)
    return 0;

  if (data[1] == 0x03 && len >= 13) {
    // Connection Complete event — data[3] = status, data[6..11] = BD_ADDR (little-endian).
    // Ignore events from devices other than the target console (e.g. an incoming connection
    // from a nearby device during the 6-second window).
    if (memcmp(&data[6], g_psx_le, 6) != 0)
      return 0;
    connection_status = data[3];
  } else if (data[1] == 0x0F && len >= 7) {
    // Command Status event — data[3] = status, data[5..6] = opcode (little-endian).
    const uint16_t opcode = static_cast<uint16_t>(data[5]) | (static_cast<uint16_t>(data[6]) << 8);
    if (opcode == 0x0405 && data[3] != 0x00) {
      connection_status = data[3];  // Command rejected — propagate immediately.
    }
  }
  return 0;
}

// Mandatory callback for VHCI interface.
static void notify_host_send_available(void) {}

// VHCI callback mapping structure.
static const esp_vhci_host_callback_t vhci_callbacks = {.notify_host_send_available = notify_host_send_available,
                                                        .notify_host_recv = notify_host_recv};

struct WakeTaskParams {
  std::string dsx_mac_;
  std::string psx_mac_;
  esphome::text_sensor::TextSensor *status_sensor_;
};

static void publish_status(esphome::text_sensor::TextSensor *sensor, const char *state) {
  if (sensor != nullptr) {
    sensor->publish_state(state);
  }
}

// Returns the integer value (0-15) of one hex digit; caller must ensure c is a valid hex character.
static uint8_t hex_nibble(char c) {
  if (c >= '0' && c <= '9')
    return static_cast<uint8_t>(c - '0');
  if (c >= 'a' && c <= 'f')
    return static_cast<uint8_t>(c - 'a' + 10);
  return static_cast<uint8_t>(c - 'A' + 10);
}

static bool parse_mac_string(const std::string &mac, uint8_t out[6]) {
  if (mac.size() != 17) {
    return false;
  }

  for (int i = 0; i < 6; i++) {
    const size_t pos = static_cast<size_t>(i) * 3;
    const unsigned char hi = static_cast<unsigned char>(mac[pos]);
    const unsigned char lo = static_cast<unsigned char>(mac[pos + 1]);

    if (!std::isxdigit(hi) || !std::isxdigit(lo)) {
      return false;
    }
    if (i < 5 && mac[pos + 2] != ':') {
      return false;
    }

    out[i] = static_cast<uint8_t>((hex_nibble(static_cast<char>(hi)) << 4) | hex_nibble(static_cast<char>(lo)));
  }

  // Reject addresses that are structurally invalid for a BT Classic unicast target:
  //
  //   all-zeros (00:00:00:00:00:00) — not a real device address
  //   broadcast (FF:FF:FF:FF:FF:FF) — reserved, not routable
  //   multicast  — IEEE 802: LSB of the first byte = 1 means group/multicast;
  //                BT Classic only uses unicast (individual) addresses
  //
  // These are caught here rather than in wake_task so the error is reported
  // before any BT hardware is touched.
  const bool is_all_zeros = (out[0] | out[1] | out[2] | out[3] | out[4] | out[5]) == 0x00;
  const bool is_broadcast = (out[0] & out[1] & out[2] & out[3] & out[4] & out[5]) == 0xFF;
  const bool is_multicast = (out[0] & 0x01) != 0;  // IEEE 802 Group bit

  if (is_all_zeros || is_broadcast || is_multicast) {
    return false;
  }

  return true;
}

static void cleanup_bt_controller() {
  esp_err_t err = esp_bt_controller_disable();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "BT controller disable failed during cleanup: %s", esp_err_to_name(err));
  }

  err = esp_bt_controller_deinit();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "BT controller deinit failed during cleanup: %s", esp_err_to_name(err));
  }
}

static void release_wake_mutex() {
  if (wake_mutex != nullptr) {
    xSemaphoreGive(wake_mutex);
  }
}

// Main FreeRTOS task handling the hardware reset, spoofing, and packet injection.
static void wake_task(void *pvParameters) {
  WakeTaskParams *params = static_cast<WakeTaskParams *>(pvParameters);

  // Capture params by value (pointer copy): finish_task deletes the object and
  // calls vTaskDelete(nullptr), so wake_task never continues after finish_task.
  auto finish_task = [params](const char *status, bool set_ready_after_delay) {
    publish_status(params->status_sensor_, status);
    cleanup_bt_controller();

    if (set_ready_after_delay) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_RESULT_DISPLAY_MS));
      publish_status(params->status_sensor_, "Ready");
    }

    release_wake_mutex();
    delete params;
    vTaskDelete(nullptr);
  };

  publish_status(params->status_sensor_, "Connecting...");

  uint8_t dsx_raw[6];
  uint8_t psx_raw[6];

  // Parse MAC address strings into raw hex byte arrays.
  if (!parse_mac_string(params->dsx_mac_, dsx_raw) || !parse_mac_string(params->psx_mac_, psx_raw)) {
    ESP_LOGW(TAG, "Invalid MAC address format. dsx_mac=%s psx_mac=%s", params->dsx_mac_.c_str(),
             params->psx_mac_.c_str());
    finish_task("Invalid MAC", true);
    return;
  }

  // Step 1: Fully disable and de-initialize the BT controller.
  cleanup_bt_controller();
  vTaskDelay(pdMS_TO_TICKS(BT_CONTROLLER_SETTLE_MS));

  // Step 2: Spoof the MAC address.
  esp_err_t err = esp_iface_mac_addr_set(dsx_raw, ESP_MAC_BT);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to spoof BT MAC address: %s", esp_err_to_name(err));
    finish_task("Spoof Error", true);
    return;
  }

  // Step 3: Re-initialize the controller strictly in Classic BT mode.
  esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
  bt_cfg.mode = ESP_BT_MODE_CLASSIC_BT;

  err = esp_bt_controller_init(&bt_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to initialize BT controller: %s", esp_err_to_name(err));
    finish_task("Init Error", true);
    return;
  }

  err = esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to enable BT controller: %s", esp_err_to_name(err));
    finish_task("Enable Error", true);
    return;
  }

  // Step 4: Register VHCI callbacks and wait for controller readiness.
  err = esp_vhci_host_register_callback(&vhci_callbacks);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to register VHCI callback: %s", esp_err_to_name(err));
    finish_task("VHCI Error", true);
    return;
  }

  vTaskDelay(pdMS_TO_TICKS(VHCI_WARMUP_MS));

  // Step 5: Construct and inject the raw HCI packet.
  // HCI Create Connection packet (Bluetooth Core Spec Vol 4 Part E §7.1.5).
  // 0x01             -> HCI Command Packet indicator.
  // 0x05 0x04        -> Opcode 0x0405 (HCI_Create_Connection), little-endian.
  // 0x0D             -> Parameter length (13 bytes).
  // BD_ADDR (6 bytes)-> Target console MAC in little-endian order.
  // 0x18 0xCC        -> Packet type: DM1|DH1|DM3|DH3|DM5|DH5 (see HCI_BR_PACKET_TYPE).
  // 0x02             -> Page Scan Repetition Mode R2.
  // 0x00             -> Reserved.
  // 0x00 0x00        -> Clock Offset (valid flag not set, value ignored).
  // 0x01             -> Allow Role Switch.
  uint8_t hci_packet[17] = {
      0x01,
      0x05,
      0x04,
      0x0D,
      psx_raw[5],
      psx_raw[4],
      psx_raw[3],
      psx_raw[2],
      psx_raw[1],
      psx_raw[0],  // Target MAC (little-endian).
      static_cast<uint8_t>(HCI_BR_PACKET_TYPE & 0xFF),
      static_cast<uint8_t>(HCI_BR_PACKET_TYPE >> 8),  // Packet type (little-endian).
      0x02,                                           // Page Scan Repetition Mode.
      0x00,                                           // Reserved.
      0x00,
      0x00,  // Clock Offset (valid flag not set, value ignored).
      0x01   // Allow Role Switch.
  };

  int tx_wait_cycles = 0;
  while (!esp_vhci_host_check_send_available() && tx_wait_cycles < VHCI_TX_POLL_MAX_CYCLES) {
    vTaskDelay(pdMS_TO_TICKS(VHCI_TX_POLL_INTERVAL_MS));
    tx_wait_cycles++;
  }

  if (!esp_vhci_host_check_send_available()) {
    ESP_LOGE(TAG, "VHCI host not ready to send packet");
    finish_task("TX Unavailable", true);
    return;
  }

  // Store target MAC in little-endian for the notify_host_recv filter, then
  // reset connection_status just before sending so the warm-up window is clean.
  for (int i = 0; i < 6; i++)
    g_psx_le[i] = psx_raw[5 - i];
  connection_status = -1;
  esp_vhci_host_send_packet(hci_packet, sizeof(hci_packet));

  // Step 6: Monitor connection status.
  int timer = 0;
  while (connection_status == -1 && timer < WAKE_TIMEOUT_CYCLES) {
    vTaskDelay(pdMS_TO_TICKS(WAKE_POLL_INTERVAL_MS));
    timer++;
  }

  // Step 7: Evaluate the HCI response.
  // Snapshot the atomic once — avoids a second read between the while exit and the branches.
  const int final_status = connection_status.load();
  const char *result = "BT Error";
  if (final_status == 0x00) {
    result = "Success";
  } else if (final_status == 0x04) {
    result = "Page Timeout";
  } else if (final_status == -1) {
    result = "No Response";
  }

  finish_task(result, true);
}

/**
 * @brief Wake a PlayStation console via a raw HCI Create Connection packet.
 *
 * Spoofs the ESP32 Bluetooth MAC address to impersonate a paired DualShock or
 * DualSense controller, then injects an HCI_Create_Connection packet targeting
 * the console. The result is published to @p sensor as a human-readable string
 * ("Success", "Page Timeout", "No Response", "Busy", or an error label).
 *
 * Re-entrant-safe: a lazily-initialised mutex prevents concurrent executions.
 * A second call while a wake sequence is in progress immediately publishes
 * "Busy" and returns without touching the hardware.
 *
 * The actual work runs in a dedicated FreeRTOS task (@ref wake_task). This
 * function returns immediately after spawning the task (or on early-exit paths).
 *
 * @param dsx_mac  MAC address of the paired controller to impersonate,
 *                 colon-separated uppercase hex (e.g. "AA:BB:CC:DD:EE:FF").
 * @param psx_mac  MAC address of the target PlayStation console (same format).
 * @param sensor   ESPHome TextSensor receiving status updates; may be @c nullptr
 *                 (updates are silently skipped).
 */
static void wake_psx(const std::string &dsx_mac, const std::string &psx_mac, esphome::text_sensor::TextSensor *sensor) {
  if (wake_mutex == nullptr) {
    wake_mutex = xSemaphoreCreateMutex();
    if (wake_mutex == nullptr) {
      ESP_LOGE(TAG, "Failed to create wake mutex");
      publish_status(sensor, "Task Error");
      return;
    }
  }

  if (xSemaphoreTake(wake_mutex, 0) != pdTRUE) {
    publish_status(sensor, "Busy");
    return;
  }

  // Heap allocation is intentional and unavoidable here: xTaskCreate() allocates 4096 bytes of task
  // stack on the heap anyway, making the struct allocation negligible by comparison. A static buffer
  // would be unsafe without additional synchronization beyond the mutex, and this function is called
  // at most a few times per day, so fragmentation risk is nil.
  WakeTaskParams *p = new WakeTaskParams{dsx_mac, psx_mac, sensor};

  // Priority 5: deliberately low. BT controller internal tasks run at 23 (Controller)
  // and 19 (Host stack). At priority 5 the wake task yields immediately to any BT
  // driver work, avoiding priority inversion while still preempting idle (priority 0).
  if (xTaskCreate(wake_task, "wakepsx_on_bt_job", WAKE_TASK_STACK_WORDS, p, 5, nullptr) != pdPASS) {
    ESP_LOGE(TAG, "Failed to create wake task");
    publish_status(sensor, "Task Error");
    delete p;
    release_wake_mutex();
  }
}
}  // namespace wakepsx_on_bt
