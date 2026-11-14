#include "wol.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "freertos/task.h"

SemaphoreHandle_t wol_lan_lock;

#define RESET_GESTURE_NAMESPACE "power_guard"
#define RESET_GESTURE_KEY "starts"
#define RESET_GESTURE_STARTS 5
#define RESET_GESTURE_STABLE_SECONDS 20

static const char *TAG = "boot_guard";

static bool is_power_cycle_reset(esp_reset_reason_t reason)
{
    // Supply dips are faults, not an intentional factory-reset gesture.
    return reason == ESP_RST_POWERON;
}

static void clear_reset_gesture_after_stable_uptime(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(RESET_GESTURE_STABLE_SECONDS * 1000));

    nvs_handle_t handle;
    esp_err_t err = nvs_open(RESET_GESTURE_NAMESPACE, NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        uint8_t starts = 0;
        err = nvs_get_u8(handle, RESET_GESTURE_KEY, &starts);
        if (err == ESP_OK && starts != 0) {
            err = nvs_set_u8(handle, RESET_GESTURE_KEY, 0);
            if (err == ESP_OK) err = nvs_commit(handle);
        }
        nvs_close(handle);
    }
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND)
        ESP_LOGW(TAG, "Could not clear power-cycle counter: %s", esp_err_to_name(err));
    else
        ESP_LOGI(TAG, "Stable uptime reached; power-cycle counter cleared");
    vTaskDelete(NULL);
}

static void reset_gesture_init(esp_reset_reason_t reason)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(RESET_GESTURE_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not open power-cycle counter: %s", esp_err_to_name(err));
        return;
    }

    uint8_t starts = 0;
    err = nvs_get_u8(handle, RESET_GESTURE_KEY, &starts);
    if (!is_power_cycle_reset(reason)) {
        if (err == ESP_OK && starts != 0) {
            err = nvs_set_u8(handle, RESET_GESTURE_KEY, 0);
            if (err == ESP_OK) err = nvs_commit(handle);
        }
        nvs_close(handle);
        if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND)
            ESP_LOGW(TAG, "Could not clear power-cycle counter: %s", esp_err_to_name(err));
        return;
    }

    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        ESP_LOGW(TAG, "Could not read power-cycle counter: %s", esp_err_to_name(err));
        return;
    }
    starts = starts < RESET_GESTURE_STARTS ? starts + 1 : RESET_GESTURE_STARTS;
    err = nvs_set_u8(handle, RESET_GESTURE_KEY, starts);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not save power-cycle counter: %s", esp_err_to_name(err));
        return;
    }

    if (starts >= RESET_GESTURE_STARTS) {
        ESP_LOGW(TAG, "Power-cycle reset gesture detected; clearing gateway settings");
        err = nvs_open(RESET_GESTURE_NAMESPACE, NVS_READWRITE, &handle);
        if (err == ESP_OK) {
            err = nvs_set_u8(handle, RESET_GESTURE_KEY, 0);
            if (err == ESP_OK) err = nvs_commit(handle);
            nvs_close(handle);
        }
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Could not re-arm power-cycle guard; factory reset skipped: %s", esp_err_to_name(err));
            return;
        }
        wol_config_reset();
        ESP_LOGW(TAG, "Gateway settings cleared; starting setup hotspot");
        return;
    }

    ESP_LOGI(TAG, "Power-on start %u/%u; stay powered for %u seconds to cancel the sequence",
             (unsigned)starts, (unsigned)RESET_GESTURE_STARTS,
             (unsigned)RESET_GESTURE_STABLE_SECONDS);
    if (xTaskCreate(clear_reset_gesture_after_stable_uptime, "power_cycle_guard", 2048, NULL, 1, NULL) != pdPASS)
        ESP_LOGW(TAG, "Could not start power-cycle counter timer");
}

void app_main(void)
{
    ESP_LOGI("wol", "Boot reset reason: %d", esp_reset_reason());
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    reset_gesture_init(esp_reset_reason());
    wol_config_init();
    wol_ota_init();
    wol_lan_lock = xSemaphoreCreateMutex();
    configASSERT(wol_lan_lock);
    wol_network_start();
    wol_mqtt_start();
    configASSERT(xTaskCreate(wol_monitor_task, "device_monitor", 6144, NULL, 3, NULL) == pdPASS);
    ESP_LOGI("wol", "Gateway ready; station MAC: %s", wol_mac);
    ESP_LOGI("wol", "Config snapshot bytes: full=%u settings=%u; heap=%u min_heap=%u",
             (unsigned)sizeof(wol_config_t), (unsigned)sizeof(wol_settings_t),
             (unsigned)esp_get_free_heap_size(), (unsigned)esp_get_minimum_free_heap_size());
}
