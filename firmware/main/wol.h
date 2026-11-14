#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "esp_err.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define WOL_MAX_DEVICES 64
#define WOL_MAX_MESSAGE 24576
#define WOL_MAX_HOSTS 254

typedef struct {
    char name[65];
    char mac[13];
    char ip[16];
} wol_device_t;

// Keep this persisted prefix identical: existing NVS v3 configurations remain valid.
#define WOL_SETTINGS_FIELDS \
    char ssid[33]; \
    char password[65]; \
    char broker[192]; \
    char mqtt_username[65]; \
    char mqtt_password[129]; \
    char bind_key[65]; \
    int ping_interval; \
    int heartbeat_interval; \
    char broadcast[16]; \
    char scan_cidr[20]; \
    int scan_concurrency; \
    int device_count;

typedef struct {
    WOL_SETTINGS_FIELDS
    char pairing_code[17];
    char gateway_name[65];
} wol_settings_t;

typedef struct {
    WOL_SETTINGS_FIELDS
    wol_device_t devices[WOL_MAX_DEVICES];
    char pairing_code[17];
    char gateway_name[65];
} wol_config_t;
#undef WOL_SETTINGS_FIELDS

extern char wol_mac[13];
extern esp_netif_t *wol_sta;
extern SemaphoreHandle_t wol_lan_lock;

void wol_config_init(void);
void wol_config_get(wol_config_t *out);
void wol_settings_get(wol_settings_t *out);
esp_err_t wol_config_save(const wol_config_t *cfg);
esp_err_t wol_config_set_wifi(const char *ssid, const char *password);
esp_err_t wol_config_set_setup(const char *ssid, const char *password, const char *pairing, const char *key);
esp_err_t wol_config_set_enrollment(const char *pairing, const char *key, const char *username,
    const char *password, const char *new_key);
esp_err_t wol_config_set_bind_key(const char *key);
esp_err_t wol_config_set_pairing_code(const char *code);
esp_err_t wol_config_finish_pairing(const char *code, const char *key);
bool wol_pairing_code(const char *input, char out[17]);
esp_err_t wol_config_apply(const cJSON *payload, bool devices, bool config);
void wol_config_reset(void);
bool wol_mac_parse(const char *str, uint8_t bytes[6]);
void wol_mac_format(const uint8_t bytes[6], char out[13]);
const char *wol_json_string(const cJSON *obj, const char *key);
bool wol_ipv4(const char *ip);
bool wol_cidr(const char *cidr, uint32_t *network, uint32_t *mask);
bool wol_wifi_valid(const char *ssid, const char *password);

void wol_network_start(void);
bool wol_network_ready(void);
bool wol_network_clock_valid(void);
uint32_t wol_network_time_sync_elapsed(void);
bool wol_network_time_sync_expired(void);
void wol_network_transport_timeout(void);
void wol_network_info(cJSON *obj);
void wol_reboot_later(void);

void wol_mqtt_start(void);
bool wol_mqtt_ready(void);
bool wol_enroll(void);
bool wol_mqtt_bound(void);
void wol_binding_info(cJSON *obj);
void wol_binding_retry(void);
void wol_binding_error(const char *code);
bool wol_binding_blocked(void);
bool wol_publish(const char *suffix, const cJSON *body, int qos);
bool wol_publish_live(const char *suffix, const cJSON *body);
esp_err_t wol_wake(const cJSON *payload);
esp_err_t wol_scan(cJSON **result, bool background, const char *cmd_id);
void wol_scan_cancel(void);
void wol_scan_cancel_reset(void);
void wol_monitor_task(void *arg);
void wol_monitor_request(void);
void wol_ota_init(void);
bool wol_ota_active(void);
esp_err_t wol_ota_start(const cJSON *payload);
void wol_ota_info(cJSON *obj);
