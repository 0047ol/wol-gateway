#include <ctype.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "wol.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

static wol_config_t current;
static SemaphoreHandle_t config_lock;

bool wol_pairing_code(const char *input, char out[17])
{
    size_t count = 0;
    for (; input && *input; input++) {
        if (*input == '-' || isspace((unsigned char)*input)) continue;
        if (!isxdigit((unsigned char)*input) || count >= 16) return false;
        out[count++] = toupper((unsigned char)*input);
    }
    out[count] = 0;
    return count == 16;
}

const char *wol_json_string(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}

bool wol_mac_parse(const char *str, uint8_t bytes[6])
{
    if (!str || strlen(str) != 12) return false;
    for (int i = 0; i < 6; i++) {
        char pair[3] = {str[i * 2], str[i * 2 + 1], 0};
        if (!isxdigit((unsigned char)pair[0]) || !isxdigit((unsigned char)pair[1])) return false;
        bytes[i] = (uint8_t)strtoul(pair, NULL, 16);
    }
    return true;
}

void wol_mac_format(const uint8_t b[6], char out[13])
{
    snprintf(out, 13, "%02x%02x%02x%02x%02x%02x", b[0], b[1], b[2], b[3], b[4], b[5]);
}

bool wol_ipv4(const char *ip)
{
    struct in_addr a;
    return ip && inet_pton(AF_INET, ip, &a) == 1;
}

bool wol_cidr(const char *cidr, uint32_t *network, uint32_t *mask)
{
    char address[20];
    if (!cidr || strlen(cidr) >= sizeof(address)) return false;
    strlcpy(address, cidr, sizeof(address));
    char *slash = strchr(address, '/');
    if (!slash) return false;
    *slash++ = 0;
    char *end;
    long prefix = strtol(slash, &end, 10);
    struct in_addr ip;
    if (!*slash || *end || prefix < 24 || prefix > 30 || inet_pton(AF_INET, address, &ip) != 1) return false;
    *mask = UINT32_MAX << (32 - prefix);
    *network = ntohl(ip.s_addr) & *mask;
    return true;
}

bool wol_wifi_valid(const char *ssid, const char *password)
{
    size_t n = strlen(password);
    if (!*ssid || strlen(ssid) > 32 || n > 64) return false;
    if (n == 64) {
        for (size_t i = 0; i < n; i++) if (!isxdigit((unsigned char)password[i])) return false;
        return true;
    }
    return n == 0 || n >= 8;
}

void wol_config_init(void)
{
    config_lock = xSemaphoreCreateMutex();
    configASSERT(config_lock);
    current.ping_interval = 30;
    current.heartbeat_interval = 15;
    current.scan_concurrency = 8;
    strlcpy(current.broadcast, "255.255.255.255", sizeof(current.broadcast));
    strlcpy(current.broker, WOL_DEFAULT_BROKER, sizeof(current.broker));
    nvs_handle_t h;
    if (nvs_open("wol", NVS_READONLY, &h) == ESP_OK) {
        uint32_t version = 0;
        size_t len = sizeof(current);
        wol_config_t *saved = calloc(1, sizeof(*saved));
        if (saved && nvs_get_u32(h, "version", &version) == ESP_OK &&
            nvs_get_blob(h, "config", saved, &len) == ESP_OK &&
            ((version == 4 && len == sizeof(*saved)) ||
             (version == 3 && len == ((offsetof(wol_config_t, gateway_name) + 3) & ~(size_t)3)) ||
             (version == 1 && len == offsetof(wol_config_t, pairing_code)))) {
            if (version < 4) memset(saved->gateway_name, 0, sizeof(saved->gateway_name));
            current = *saved;
        }
        free(saved);
        nvs_close(h);
    }
    if (!*current.broker) strlcpy(current.broker, WOL_DEFAULT_BROKER, sizeof(current.broker));
}

void wol_config_get(wol_config_t *out)
{
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *out = current;
    xSemaphoreGive(config_lock);
}

void wol_settings_get(wol_settings_t *out)
{
    _Static_assert(offsetof(wol_settings_t, pairing_code) == offsetof(wol_config_t, devices), "NVS prefix layout changed");
    xSemaphoreTake(config_lock, portMAX_DELAY);
    memcpy(out, &current, offsetof(wol_config_t, devices));
    memcpy(out->pairing_code, current.pairing_code, sizeof(out->pairing_code));
    memcpy(out->gateway_name, current.gateway_name, sizeof(out->gateway_name));
    xSemaphoreGive(config_lock);
}

static esp_err_t save_locked(const wol_config_t *cfg)
{
    if (memcmp(&current, cfg, sizeof(current)) == 0) return ESP_OK;
    nvs_handle_t h;
    esp_err_t err = nvs_open("wol", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        err = nvs_set_blob(h, "config", cfg, sizeof(*cfg));
        if (err == ESP_OK) err = nvs_set_u32(h, "version", 4);
        if (err == ESP_OK) err = nvs_commit(h);
        nvs_close(h);
    }
    if (err == ESP_OK) current = *cfg;
    return err;
}

esp_err_t wol_config_save(const wol_config_t *cfg)
{
    xSemaphoreTake(config_lock, portMAX_DELAY);
    esp_err_t err = save_locked(cfg);
    xSemaphoreGive(config_lock);
    return err;
}

esp_err_t wol_config_set_wifi(const char *ssid, const char *password)
{
    if (!wol_wifi_valid(ssid, password)) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    strlcpy(cfg->ssid, ssid, sizeof(cfg->ssid));
    strlcpy(cfg->password, password, sizeof(cfg->password));
    esp_err_t err = save_locked(cfg);
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

esp_err_t wol_config_set_setup(const char *ssid, const char *password, const char *broker,
    const char *pairing, const char *key)
{
    if (!wol_wifi_valid(ssid, password) || !broker || strlen(broker) >= sizeof(current.broker))
        return ESP_ERR_INVALID_ARG;
    char normalized[17] = {0};
    if ((*pairing && !wol_pairing_code(pairing, normalized)) ||
        (*key && (strlen(key) < 16 || strlen(key) > 64))) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    strlcpy(cfg->ssid, ssid, sizeof(cfg->ssid));
    strlcpy(cfg->password, password, sizeof(cfg->password));
    strlcpy(cfg->broker, *broker ? broker : WOL_DEFAULT_BROKER, sizeof(cfg->broker));
    if (*pairing || *key) {
        strlcpy(cfg->pairing_code, normalized, sizeof(cfg->pairing_code));
        strlcpy(cfg->bind_key, *pairing ? "" : key, sizeof(cfg->bind_key));
    }
    esp_err_t err = save_locked(cfg);
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

esp_err_t wol_config_set_enrollment(const char *pairing, const char *key, const char *username,
    const char *password, const char *new_key)
{
    if (!*username || strlen(username) >= sizeof(current.mqtt_username) ||
        strlen(password) < 32 || strlen(password) >= sizeof(current.mqtt_password) ||
        strlen(new_key) != 32) return ESP_ERR_INVALID_ARG;
    for (int i = 0; i < 32; i++) if (!isxdigit((unsigned char)new_key[i])) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    esp_err_t err = ESP_ERR_INVALID_STATE;
    // A delayed enrollment must never replace a newer pairing or restore an old
    // Wi-Fi/device snapshot. Merge credentials into the current configuration.
    if (strcmp(current.pairing_code, pairing) == 0 && strcmp(current.bind_key, key) == 0) {
        *cfg = current;
        strlcpy(cfg->mqtt_username, username, sizeof(cfg->mqtt_username));
        strlcpy(cfg->mqtt_password, password, sizeof(cfg->mqtt_password));
        strlcpy(cfg->bind_key, new_key, sizeof(cfg->bind_key));
        cfg->pairing_code[0] = 0;
        err = save_locked(cfg);
    }
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

esp_err_t wol_config_set_bind_key(const char *key)
{
    if (!key || strlen(key) < 16 || strlen(key) > 64) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (*cfg->ssid && *cfg->broker) {
        strlcpy(cfg->bind_key, key, sizeof(cfg->bind_key));
        cfg->pairing_code[0] = 0;
        err = save_locked(cfg);
    }
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

esp_err_t wol_config_set_pairing_code(const char *code)
{
    char normalized[17];
    if (!wol_pairing_code(code, normalized)) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (*cfg->ssid && *cfg->broker) {
        strlcpy(cfg->pairing_code, normalized, sizeof(cfg->pairing_code));
        cfg->bind_key[0] = 0;
        err = save_locked(cfg);
    }
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

esp_err_t wol_config_finish_pairing(const char *code, const char *key)
{
    if (!key || strlen(key) != 32) return ESP_ERR_INVALID_ARG;
    for (int i = 0; i < 32; i++) if (!isxdigit((unsigned char)key[i])) return ESP_ERR_INVALID_ARG;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (*code && strcmp(current.pairing_code, code) == 0) {
        strlcpy(cfg->bind_key, key, sizeof(cfg->bind_key));
        cfg->pairing_code[0] = 0;
        err = save_locked(cfg);
    }
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

static bool number(const cJSON *p, const char *key, int *dest, int min, int max)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(p, key);
    if (!v) return true;
    if (!cJSON_IsNumber(v) || v->valuedouble != v->valueint || v->valueint < min || v->valueint > max) return false;
    *dest = v->valueint;
    return true;
}

esp_err_t wol_config_apply(const cJSON *payload, bool devices, bool config)
{
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    xSemaphoreTake(config_lock, portMAX_DELAY);
    *cfg = current;
    esp_err_t err = ESP_ERR_INVALID_ARG;
    if (!cJSON_IsObject(payload)) goto done;
    if (config) {
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(payload, "name");
        if (name) {
            if (!cJSON_IsString(name) || strlen(name->valuestring) > 64) goto done;
            strlcpy(cfg->gateway_name, name->valuestring, sizeof(cfg->gateway_name));
        }
        if (!number(payload, "ping_interval", &cfg->ping_interval, 5, 3600) ||
            !number(payload, "heartbeat_send_interval", &cfg->heartbeat_interval, 5, 300) ||
            !number(payload, "scan_concurrency", &cfg->scan_concurrency, 1, 8)) goto done;
        const cJSON *b = cJSON_GetObjectItemCaseSensitive(payload, "wol_broadcast_addr");
        if (b) {
            if (!cJSON_IsString(b) || !wol_ipv4(b->valuestring)) goto done;
            strlcpy(cfg->broadcast, b->valuestring, sizeof(cfg->broadcast));
        }
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(payload, "scan_cidr");
        if (c) {
            uint32_t net, mask;
            if (!cJSON_IsString(c) || (*c->valuestring && !wol_cidr(c->valuestring, &net, &mask))) goto done;
            strlcpy(cfg->scan_cidr, c->valuestring, sizeof(cfg->scan_cidr));
        }
    }
    if (devices) {
        const cJSON *list = cJSON_GetObjectItemCaseSensitive(payload, "sub_devices");
        if (!cJSON_IsArray(list) || cJSON_GetArraySize(list) > WOL_MAX_DEVICES) goto done;
        cfg->device_count = 0;
        const cJSON *d;
        cJSON_ArrayForEach(d, list) {
            uint8_t mac[6];
            const char *name = wol_json_string(d, "name"), *ip = wol_json_string(d, "ip");
            if (!wol_mac_parse(wol_json_string(d, "mac"), mac) || strlen(name) > 64 || (*ip && !wol_ipv4(ip))) goto done;
            wol_device_t *target = &cfg->devices[cfg->device_count++];
            wol_mac_format(mac, target->mac);
            strlcpy(target->name, name, sizeof(target->name));
            strlcpy(target->ip, ip, sizeof(target->ip));
        }
    }
    err = save_locked(cfg);
done:
    xSemaphoreGive(config_lock);
    free(cfg);
    return err;
}

void wol_config_reset(void)
{
    nvs_handle_t h;
    ESP_ERROR_CHECK(nvs_open("wol", NVS_READWRITE, &h));
    ESP_ERROR_CHECK(nvs_erase_all(h));
    ESP_ERROR_CHECK(nvs_commit(h));
    nvs_close(h);
}
