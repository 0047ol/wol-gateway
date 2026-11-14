#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/socket.h>
#include "wol.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "captive_portal.h"
#include "network_recovery.h"
#include "wifi_scan_cache.h"

char wol_mac[13];
esp_netif_t *wol_sta;
static esp_netif_t *ap_netif;
static volatile bool has_ip;
static volatile uint32_t wifi_ip_acquired_ms;
static volatile uint16_t wifi_disconnect_reason;
static volatile uint8_t wifi_disconnect_count;
static volatile uint8_t setup_clients;
static volatile bool wifi_retry_pending;
static volatile uint32_t wifi_retry_at_ms;
/* In APSTA mode the setup AP follows the station channel.  Defer station
 * association while a provisioning client is connected so opening the portal
 * cannot move the hotspot underneath the browser. */
static volatile bool wifi_connect_pending;
static volatile uint32_t wifi_connect_not_before_ms;
static volatile bool wifi_connect_force;
static volatile uint32_t wifi_config_started_ms;
static volatile bool wifi_setup_active;
static volatile bool wifi_setup_failed;
static volatile bool wifi_setup_timed_out;
static volatile bool wifi_station_connecting;
static uint32_t wifi_scan_last_started_ms;
static bool ap_active;
static bool have_credentials;
static volatile bool wifi_scanning;
static SemaphoreHandle_t wifi_scan_lock;
static wol_wifi_scan_cache_t wifi_scan_cache;
static esp_err_t wifi_scan_error;
static int64_t lost_since;
static int64_t healthy_since;
enum { SETUP_AP_CLOSE_SECONDS = 10 };
static const char *TAG = "network";
static portMUX_TYPE recovery_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t transport_timeouts;
static int64_t first_transport_timeout, last_transport_recovery;
extern const char portal_html_start[] asm("_binary_portal_html_start");

bool wol_network_ready(void) { return has_ip; }

bool wol_network_clock_valid(void)
{
    return time(NULL) >= 1704067200;
}

uint32_t wol_network_time_sync_elapsed(void)
{
    if (!has_ip || wol_network_clock_valid() || !wifi_ip_acquired_ms) return 0;
    return ((uint32_t)(esp_timer_get_time() / 1000) - wifi_ip_acquired_ms) / 1000;
}

bool wol_network_time_sync_expired(void)
{
    return wol_network_time_sync_elapsed() >= 90;
}

static void time_sync_notification(struct timeval *tv)
{
    if (tv) ESP_LOGI(TAG, "SNTP synchronized, epoch=%lld", (long long)tv->tv_sec);
}

void wol_network_transport_timeout(void)
{
    portENTER_CRITICAL(&recovery_lock);
    if (!transport_timeouts) first_transport_timeout = esp_timer_get_time() / 1000;
    if (transport_timeouts < UINT32_MAX) transport_timeouts++;
    portEXIT_CRITICAL(&recovery_lock);
}

void wol_network_info(cJSON *obj)
{
    wol_ota_info(obj);
    wifi_ap_record_t access_point;
    if (esp_wifi_sta_get_ap_info(&access_point) == ESP_OK) {
        char ssid[33];
        memcpy(ssid, access_point.ssid, 32);
        ssid[32] = '\0';
        cJSON_AddStringToObject(obj, "wifi_ssid", ssid);
    }
    cJSON_AddNumberToObject(obj, "uptime_ms", esp_timer_get_time() / 1000);
    cJSON_AddNumberToObject(obj, "uptime_reported_at", time(NULL));
    cJSON_AddNumberToObject(obj, "reset_reason", esp_reset_reason());
    esp_netif_ip_info_t info;
    if (esp_netif_get_ip_info(wol_sta, &info) != ESP_OK) return;
    char text[20];
    snprintf(text, sizeof(text), IPSTR, IP2STR(&info.ip));
    cJSON_AddStringToObject(obj, "local_ip", text);
    snprintf(text, sizeof(text), IPSTR, IP2STR(&info.netmask));
    cJSON_AddStringToObject(obj, "netmask", text);
    snprintf(text, sizeof(text), IPSTR, IP2STR(&info.gw));
    cJSON_AddStringToObject(obj, "gateway_ip", text);
    uint32_t mask = ntohl(info.netmask.addr);
    int prefix = 0;
    for (uint32_t bit = 0x80000000; bit && (mask & bit); bit >>= 1) prefix++;
    esp_ip4_addr_t network = {.addr = info.ip.addr & info.netmask.addr};
    snprintf(text, sizeof(text), IPSTR "/%d", IP2STR(&network), prefix);
    cJSON_AddStringToObject(obj, "cidr", text);
}

static void event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START && have_credentials) {
        wifi_config_started_ms = (uint32_t)(esp_timer_get_time() / 1000);
        wifi_connect_pending = true;
        ESP_LOGI(TAG, "STA start deferred until setup client leaves");
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        if (setup_clients < UINT8_MAX) setup_clients++;
        ESP_LOGI(TAG, "Setup client connected; clients=%u", setup_clients);
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        if (setup_clients) setup_clients--;
        ESP_LOGI(TAG, "Setup client disconnected; clients=%u", setup_clients);
        if (setup_clients == 0 && have_credentials && !has_ip && !wifi_station_connecting && !wifi_setup_active) {
            wifi_connect_pending = true;
            wifi_connect_not_before_ms = (uint32_t)(esp_timer_get_time() / 1000) + 500;
        }
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        has_ip = false;
        wifi_ip_acquired_ms = 0;
        wifi_station_connecting = false;
        wifi_event_sta_disconnected_t *event = data;
        wifi_disconnect_reason = event->reason;
        if (wifi_disconnect_count < UINT8_MAX) wifi_disconnect_count++;
        ESP_LOGW(TAG, "Wi-Fi disconnected, reason=%u", event->reason);
        if (wifi_setup_active && (event->reason == WIFI_REASON_AUTH_FAIL ||
                (wifi_disconnect_count >= 2 && (event->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
                                               event->reason == WIFI_REASON_HANDSHAKE_TIMEOUT)))) {
            wifi_setup_active = false;
            wifi_setup_failed = true;
            wifi_connect_pending = false;
            wifi_connect_force = false;
            ESP_LOGW(TAG, "Setup Wi-Fi authentication failed; reason=%u", event->reason);
        }
        if (have_credentials) {
            // Do not continuously scan off-channel while someone repairs provisioning.
            uint32_t delay_ms = wifi_setup_active ? 1500 : wifi_disconnect_count >= 3 ? 30000 : 5000;
            wifi_retry_at_ms = (uint32_t)(esp_timer_get_time() / 1000) + delay_ms;
            wifi_retry_pending = true;
        }
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        has_ip = true;
        wifi_ip_acquired_ms = (uint32_t)(esp_timer_get_time() / 1000);
        if (!wifi_ip_acquired_ms) wifi_ip_acquired_ms = 1;
        if (esp_sntp_restart()) ESP_LOGI(TAG, "Restarted SNTP after Wi-Fi obtained an IP address");
        wifi_station_connecting = false;
        wifi_setup_active = false;
        wifi_setup_failed = false;
        wifi_setup_timed_out = false;
        wifi_config_started_ms = 0;
        wifi_disconnect_reason = 0;
        wifi_disconnect_count = 0;
        wifi_retry_pending = false;
        wifi_connect_pending = false;
        wifi_connect_force = false;
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "Wi-Fi address: " IPSTR, IP2STR(&event->ip_info.ip));
    }
}

static void reboot_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(5000));
    esp_restart();
}

void wol_reboot_later(void)
{
    configASSERT(xTaskCreate(reboot_task, "reboot", 2048, NULL, 2, NULL) == pdPASS);
}

// Only accept provisioning requests received through the device's own AP.
static bool from_ap(httpd_req_t *req)
{
    struct sockaddr_storage local = {0};
    socklen_t len = sizeof(local);
    esp_netif_ip_info_t info;
    if (!ap_active || getsockname(httpd_req_to_sockfd(req), (struct sockaddr *)&local, &len) != 0 ||
        esp_netif_get_ip_info(ap_netif, &info) != ESP_OK) return false;
    uint32_t address;
    if (local.ss_family == AF_INET) {
        address = ((const struct sockaddr_in *)&local)->sin_addr.s_addr;
    } else if (local.ss_family == AF_INET6) {
        // The dual-stack HTTP listener reports IPv4 clients as ::ffff:a.b.c.d.
        const struct sockaddr_in6 *ipv6 = (const struct sockaddr_in6 *)&local;
        const uint8_t mapped_prefix[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
        if (memcmp(ipv6->sin6_addr.s6_addr, mapped_prefix, sizeof(mapped_prefix)) != 0) return false;
        memcpy(&address, &ipv6->sin6_addr.s6_addr[12], sizeof(address));
    } else {
        return false;
    }
    return address == info.ip.addr;
}

static esp_err_t portal_redirect(httpd_req_t *req);

static esp_err_t portal_get(httpd_req_t *req)
{
    if (!from_ap(req)) {
        httpd_resp_set_status(req, "404 Not Found");
        return httpd_resp_send(req, "", 0);
    }
    // Captive assistants open an Internet probe hostname. Move the document to
    // the AP address before its relative API requests can follow cellular DNS.
    char host[32];
    if (httpd_req_get_hdr_value_str(req, "Host", host, sizeof(host)) != ESP_OK ||
        (strcmp(host, "192.168.4.1") != 0 && strcmp(host, "192.168.4.1:80") != 0))
        return portal_redirect(req);
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, portal_html_start, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t info_get(httpd_req_t *req)
{
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    cJSON *obj = cJSON_CreateObject();
    bool retry_paused = wol_wifi_retry_paused(has_ip, wifi_setup_active,
        wifi_station_connecting, wifi_disconnect_count, setup_clients);
    bool connection_failed = !has_ip && (wifi_setup_failed || retry_paused ||
        (!wifi_setup_active && wifi_disconnect_count && wifi_config_started_ms &&
         (uint32_t)(esp_timer_get_time() / 1000) - wifi_config_started_ms >= 20000));
    cJSON_AddStringToObject(obj, "mac", wol_mac);
    cJSON_AddBoolToObject(obj, "wifi_connected", has_ip);
    cJSON_AddStringToObject(obj, "wifi_connection_state", has_ip ? "connected" :
        connection_failed ? "failed" :
        (wifi_setup_active || wifi_station_connecting) ? "connecting" : "idle");
    cJSON_AddNumberToObject(obj, "wifi_disconnect_reason", wifi_disconnect_reason);
    cJSON_AddBoolToObject(obj, "wifi_connection_timed_out", wifi_setup_timed_out && !has_ip);
    cJSON_AddNumberToObject(obj, "wifi_disconnect_count", wifi_disconnect_count);
    cJSON_AddBoolToObject(obj, "wifi_retry_paused", retry_paused);
    cJSON_AddNumberToObject(obj, "uptime_ms", esp_timer_get_time() / 1000);
    cJSON_AddNumberToObject(obj, "reset_reason", esp_reset_reason());
    cJSON_AddNumberToObject(obj, "free_heap", esp_get_free_heap_size());
    cJSON_AddNumberToObject(obj, "minimum_free_heap", esp_get_minimum_free_heap_size());
    cJSON_AddBoolToObject(obj, "clock_valid", wol_network_clock_valid());
    cJSON_AddNumberToObject(obj, "time_sync_elapsed_seconds", wol_network_time_sync_elapsed());
    int8_t tx_power = 0;
    if (esp_wifi_get_max_tx_power(&tx_power) == ESP_OK)
        cJSON_AddNumberToObject(obj, "wifi_tx_power_dbm", tx_power / 4.0);
    cJSON_AddBoolToObject(obj, "mqtt_connected", wol_mqtt_ready());
    wol_settings_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) {
        cJSON_Delete(obj);
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    }
    wol_settings_get(cfg);
    bool configured = *cfg->ssid && *cfg->broker;
    cJSON_AddBoolToObject(obj, "has_mqtt_credentials", *cfg->mqtt_username && *cfg->mqtt_password);
    cJSON_AddBoolToObject(obj, "pairing_pending", *cfg->pairing_code != 0);
    cJSON_AddBoolToObject(obj, "has_wifi_name", *cfg->ssid != 0);
    cJSON_AddBoolToObject(obj, "has_server_address", *cfg->broker != 0);
    cJSON_AddBoolToObject(obj, "has_pairing_credential", *cfg->bind_key != 0 || *cfg->pairing_code != 0);
    cJSON_AddBoolToObject(obj, "configured", configured);
    cJSON_AddStringToObject(obj, "ssid", cfg->ssid);
    cJSON_AddStringToObject(obj, "broker", cfg->broker);
    cJSON_AddStringToObject(obj, "mqtt_username", cfg->mqtt_username);
    cJSON_AddStringToObject(obj, "name", cfg->gateway_name);
    cJSON_AddNumberToObject(obj, "ap_close_seconds", ap_active && has_ip && wol_mqtt_bound()
        ? (healthy_since ? MAX(0, SETUP_AP_CLOSE_SECONDS - (int)(esp_timer_get_time() / 1000000 - healthy_since)) : SETUP_AP_CLOSE_SECONDS) : -1);
    wol_binding_info(obj);
    if (!configured) cJSON_ReplaceItemInObject(obj, "binding_status", cJSON_CreateString("not_configured"));
    else if (connection_failed)
        cJSON_ReplaceItemInObject(obj, "binding_status", cJSON_CreateString("wifi_failed"));
    else if (!has_ip && wifi_setup_active)
        cJSON_ReplaceItemInObject(obj, "binding_status", cJSON_CreateString("connecting_wifi"));
    free(cfg);
    wol_network_info(obj);
    char *json = cJSON_PrintUnformatted(obj);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_sendstr(req, json ? json : "{}");
    free(json);
    cJSON_Delete(obj);
    return err;
}

static esp_err_t portal_redirect(httpd_req_t *req)
{
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    // A non-empty body is required by some captive network assistants.
    return httpd_resp_sendstr(req, "<!doctype html><html><body><a href=\"http://192.168.4.1/\">打开 WOL 网关配网页面</a></body></html>");
}

static esp_err_t portal_not_found(httpd_req_t *req, httpd_err_code_t error)
{
    // Preserve real API errors, rather than returning an HTML redirect to fetch().
    if (strncmp(req->uri, "/api/", 5) == 0) {
        httpd_resp_set_status(req, "404 Not Found");
        return httpd_resp_sendstr(req, "API not found");
    }
    return portal_redirect(req);
}

static esp_err_t api_reply(httpd_req_t *req, const char *status, bool ok, const char *message)
{
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddBoolToObject(obj, "ok", ok);
    cJSON_AddStringToObject(obj, "msg", message);
    char *raw = cJSON_PrintUnformatted(obj);
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_sendstr(req, raw ? raw : "{}");
    free(raw);
    cJSON_Delete(obj);
    return err;
}

static bool personal_wifi(wifi_auth_mode_t mode)
{
    return mode == WIFI_AUTH_OPEN || mode == WIFI_AUTH_WPA_PSK || mode == WIFI_AUTH_WPA2_PSK ||
           mode == WIFI_AUTH_WPA_WPA2_PSK || mode == WIFI_AUTH_WPA3_PSK || mode == WIFI_AUTH_WPA2_WPA3_PSK;
}

static esp_err_t wifi_scan_collect(const wifi_scan_config_t *scan, wifi_ap_record_t *records)
{
    esp_wifi_clear_ap_list();
    esp_err_t err = esp_wifi_scan_start(scan, true);
    if (err == ESP_OK) {
        uint16_t count = WOL_WIFI_SCAN_LIMIT;
        err = esp_wifi_scan_get_ap_records(&count, records);
        if (err == ESP_OK) {
            xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
            uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            for (uint16_t i = 0; i < count; i++)
                wol_wifi_scan_merge(&wifi_scan_cache, (const char *)records[i].ssid,
                    records[i].authmode, records[i].rssi, now);
            xSemaphoreGive(wifi_scan_lock);
        }
    }
    if (err != ESP_OK) esp_wifi_clear_ap_list();
    return err;
}

static esp_err_t wifi_scan_once(void)
{
    wifi_scan_config_t scan = {.show_hidden = false, .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time.active = {.min = 60, .max = 120}, .home_chan_dwell_time = 150};
    ESP_LOGI(TAG, "Wi-Fi scan starting; clients=%u heap=%u", setup_clients, (unsigned)esp_get_free_heap_size());
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    wol_wifi_scan_begin(&wifi_scan_cache, (uint32_t)(esp_timer_get_time() / 1000));
    xSemaphoreGive(wifi_scan_lock);
    wifi_ap_record_t *records = malloc(WOL_WIFI_SCAN_LIMIT * sizeof(*records));
    esp_err_t err = records ? wifi_scan_collect(&scan, records) : ESP_ERR_NO_MEM;
    if (err == ESP_OK) {
        wol_settings_t *cfg = malloc(sizeof(*cfg));
        if (cfg) {
            wol_settings_get(cfg);
            xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
            bool missing = *cfg->ssid && !wol_wifi_scan_seen(&wifi_scan_cache, cfg->ssid);
            xSemaphoreGive(wifi_scan_lock);
            if (missing) {
                // One bounded directed retry helps discover the saved network
                // without adding another full scan for every visible network.
                scan.ssid = (uint8_t *)cfg->ssid;
                scan.scan_time.active.min = 80;
                scan.scan_time.active.max = 160;
                esp_err_t retry = wifi_scan_collect(&scan, records);
                ESP_LOGI(TAG, "Saved Wi-Fi directed scan finished; error=%s", esp_err_to_name(retry));
            }
            free(cfg);
        }
    }
    free(records);
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    wifi_scan_error = err;
    ESP_LOGI(TAG, "Wi-Fi scan finished; error=%s count=%u heap=%u stack=%u", esp_err_to_name(err), wifi_scan_cache.count,
             (unsigned)esp_get_free_heap_size(), (unsigned)uxTaskGetStackHighWaterMark(NULL));
    xSemaphoreGive(wifi_scan_lock);
    return err;
}

static void wifi_scan_task(void *arg)
{
    wifi_scan_once();
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    wifi_scanning = false;
    xSemaphoreGive(wifi_scan_lock);
    vTaskDelete(NULL);
}

static esp_err_t wifi_scan_start(httpd_req_t *req)
{
    if (wol_ota_active()) return api_reply(req, "409 Conflict", false, "正在升级固件，请等待升级结束");
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (wifi_setup_active || wifi_station_connecting) {
        xSemaphoreGive(wifi_scan_lock);
        return api_reply(req, "409 Conflict", false, "正在连接 Wi-Fi，请等待连接结果后再刷新");
    }
    if (!wifi_scanning && wifi_scan_last_started_ms && (uint32_t)(now_ms - wifi_scan_last_started_ms) < 10000) {
        xSemaphoreGive(wifi_scan_lock);
        return api_reply(req, "429 Too Many Requests", false, "请间隔 10 秒刷新，避免连续扫描影响配网热点");
    }
    if (!wifi_scanning) {
        wifi_scanning = true;
        wifi_scan_last_started_ms = now_ms;
        wifi_scan_error = ESP_OK;
        if (xTaskCreate(wifi_scan_task, "wifi_scan", 4096, NULL, 3, NULL) != pdPASS) {
            wifi_scanning = false;
            xSemaphoreGive(wifi_scan_lock);
            return api_reply(req, "500 Internal Server Error", false, "无法启动 Wi-Fi 扫描，请稍后重试");
        }
    }
    xSemaphoreGive(wifi_scan_lock);
    return api_reply(req, "200 OK", true, "正在扫描附近的 2.4 GHz 网络");
}

static esp_err_t wifi_scan_get(httpd_req_t *req)
{
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    cJSON *result = cJSON_CreateObject(), *networks = cJSON_CreateArray();
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    wol_wifi_scan_prune(&wifi_scan_cache, now_ms);
    cJSON_AddBoolToObject(result, "scanning", wifi_scanning);
    cJSON_AddBoolToObject(result, "ok", wifi_scan_error == ESP_OK);
    for (uint16_t i = 0; i < wifi_scan_cache.count; i++) {
        wol_wifi_scan_item_t *ap = &wifi_scan_cache.items[i];
        cJSON *item = cJSON_CreateObject();
        cJSON_AddStringToObject(item, "ssid", (char *)ap->ssid);
        cJSON_AddNumberToObject(item, "rssi", ap->rssi);
        cJSON_AddNumberToObject(item, "auth", ap->authmode);
        cJSON_AddBoolToObject(item, "secured", ap->authmode != WIFI_AUTH_OPEN);
        cJSON_AddBoolToObject(item, "supported", personal_wifi(ap->authmode));
        cJSON_AddBoolToObject(item, "seen_in_scan", ap->generation == wifi_scan_cache.generation);
        cJSON_AddNumberToObject(item, "age_seconds", (uint32_t)(now_ms - ap->seen_ms) / 1000);
        cJSON_AddItemToArray(networks, item);
    }
    xSemaphoreGive(wifi_scan_lock);
    cJSON_AddItemToObject(result, "networks", networks);
    char *raw = cJSON_PrintUnformatted(result);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_sendstr(req, raw ? raw : "{}");
    free(raw); cJSON_Delete(result);
    return err;
}

static cJSON *request_json(httpd_req_t *req)
{
    if (req->content_len <= 0 || req->content_len > 2048) {
        api_reply(req, "400 Bad Request", false, "提交内容过大或为空，请检查后重试");
        return NULL;
    }
    char *raw = calloc(1, req->content_len + 1);
    if (!raw) {
        api_reply(req, "500 Internal Server Error", false, "设备内存不足，请稍后重试");
        return NULL;
    }
    int offset = 0;
    while (offset < req->content_len) {
        int got = httpd_req_recv(req, raw + offset, req->content_len - offset);
        if (got <= 0) {
            free(raw);
            api_reply(req, "400 Bad Request", false, "接收配置失败，请重试");
            return NULL;
        }
        offset += got;
    }
    cJSON *obj = cJSON_Parse(raw);
    free(raw);
    if (!cJSON_IsObject(obj)) {
        cJSON_Delete(obj);
        api_reply(req, "400 Bad Request", false, "配置格式不正确，请刷新页面后重试");
        return NULL;
    }
    return obj;
}

static esp_err_t binding_post(httpd_req_t *req)
{
    if (wol_ota_active()) return api_reply(req, "409 Conflict", false, "正在升级固件，请等待升级结束");
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    cJSON *obj = request_json(req);
    if (!obj) return ESP_OK;
    const char *key = wol_json_string(obj, "bind_key");
    const char *pairing = wol_json_string(obj, "pairing_code");
    char normalized[17];
    if ((*pairing && !wol_pairing_code(pairing, normalized)) || (!*pairing && (strlen(key) < 16 || strlen(key) > 64))) {
        cJSON_Delete(obj);
        return api_reply(req, "400 Bad Request", false, "请复制后台生成的完整配对码");
    }
    esp_err_t err = *pairing ? wol_config_set_pairing_code(pairing) : wol_config_set_bind_key(key);
    cJSON_Delete(obj);
    if (err == ESP_ERR_INVALID_STATE) return api_reply(req, "400 Bad Request", false, "请先填写 Wi-Fi 和服务器地址完成配网");
    if (err != ESP_OK) return api_reply(req, "500 Internal Server Error", false, "配对信息保存失败，请重试");
    wol_binding_retry();
    return api_reply(req, "200 OK", true, "已保存，正在重新配对；无需重启或重填 Wi-Fi");
}

static esp_err_t setup_post(httpd_req_t *req)
{
    if (wol_ota_active()) return api_reply(req, "409 Conflict", false, "正在升级固件，请等待升级结束");
    if (!from_ap(req)) return httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "Setup Wi-Fi required");
    if (wifi_setup_active) return api_reply(req, "409 Conflict", false, "正在连接 Wi-Fi，请等待本次连接结果");
    cJSON *obj = request_json(req);
    if (!obj) return ESP_OK;
    const char *ssid = wol_json_string(obj, "ssid"), *pass = wol_json_string(obj, "password");
    const char *broker = wol_json_string(obj, "broker"), *key = wol_json_string(obj, "bind_key");
    const char *pairing = wol_json_string(obj, "pairing_code");
    char normalized[17] = {0};
    bool pairing_valid = *pairing && wol_pairing_code(pairing, normalized);
    const char *user = wol_json_string(obj, "mqtt_username"), *mpass = wol_json_string(obj, "mqtt_password");
    const char *error = NULL;
    bool found = false, secured = true;
    const cJSON *auth_value = cJSON_GetObjectItemCaseSensitive(obj, "wifi_auth");
    int auth = cJSON_IsNumber(auth_value) ? auth_value->valueint : -1;
    xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
    wol_wifi_scan_prune(&wifi_scan_cache, (uint32_t)(esp_timer_get_time() / 1000));
    for (uint16_t i = 0; i < wifi_scan_cache.count; i++) {
        wol_wifi_scan_item_t *ap = &wifi_scan_cache.items[i];
        if (strcmp((char *)ap->ssid, ssid) == 0 && ap->authmode == auth && personal_wifi(ap->authmode)) {
            found = true; secured = ap->authmode != WIFI_AUTH_OPEN; break;
        }
    }
    xSemaphoreGive(wifi_scan_lock);
    size_t ssid_len = strlen(ssid), password_len = strlen(pass);
    if (wifi_scanning) error = "请等待 Wi-Fi 扫描完成";
    else if (!found) error = "请扫描并选择一个受支持的 Wi-Fi 网络";
    else if (!ssid_len) error = "Wi-Fi 名称不能为空";
    else if (ssid_len > 32) error = "Wi-Fi 名称超过 32 字节";
    else if (password_len > 64 || (secured && password_len < 8)) error = "Wi-Fi 密码需至少 8 位，或使用 64 位十六进制 PSK";
    else if (password_len == 64) {
        for (size_t i = 0; i < password_len; i++) {
            if (!isxdigit((unsigned char)pass[i])) { error = "64 位 Wi-Fi PSK 必须全部为十六进制字符"; break; }
        }
    }
    if (!error && !wol_wifi_valid(ssid, pass)) error = "Wi-Fi 名称或密码格式无效";
    if (!error && (!*broker || strlen(broker) >= 192 ||
        (strncmp(broker, "mqtt://", 7) != 0 && strncmp(broker, "mqtts://", 8) != 0) ||
        !strstr(broker, "://")[3] || strchr(broker, '@') || strchr(broker, ' ') || strchr(broker, '\n')))
        error = "MQTT 地址无效，请填写 mqtt://主机:端口 或 mqtts://主机:端口";
    if (!error && *pairing && !pairing_valid) error = "配对码格式无效，请复制后台最新的 16 位十六进制配对码";
    if (!error && *key && (strlen(key) < 16 || strlen(key) > 64))
        error = "请填写后台生成的完整配对码";
    if (!error && strlen(user) > 64) error = "MQTT 用户名不能超过 64 字节";
    if (!error && strlen(mpass) > 128) error = "MQTT 密码不能超过 128 字节";
    // Credentials are obtained automatically over TLS after pairing, never typed here.
    if (error) {
        cJSON_Delete(obj);
        return api_reply(req, "400 Bad Request", false, error);
    }
    // Do not erase automatically provisioned credentials when changing Wi-Fi.
    esp_err_t err = wol_config_set_setup(ssid, pass, pairing, key);
    if (err != ESP_OK) {
        cJSON_Delete(obj);
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Could not save configuration");
    }

    wifi_config_t sta = {0};
    memcpy(sta.sta.ssid, ssid, strlen(ssid));
    memcpy(sta.sta.password, pass, strlen(pass));
    sta.sta.pmf_cfg.capable = true;
    cJSON_Delete(obj);

    // Apply credentials in-place so provisioning never reboots the USB device.
    wifi_config_started_ms = (uint32_t)(esp_timer_get_time() / 1000);
    wifi_setup_active = true;
    wifi_setup_failed = false;
    wifi_setup_timed_out = false;
    wifi_connect_pending = false;
    wifi_connect_force = false;
    wifi_retry_pending = false;
    has_ip = false;
    esp_wifi_disconnect();
    err = esp_wifi_set_config(WIFI_IF_STA, &sta);
    if (err != ESP_OK) {
        wifi_setup_active = false;
        wifi_setup_failed = true;
        return api_reply(req, "500 Internal Server Error", false, "配置已保存，但 Wi-Fi 设置未能应用；请保持热点连接后重试");
    }
    have_credentials = true;
    wifi_disconnect_count = 0;
    wifi_disconnect_reason = 0;
    wifi_config_started_ms = (uint32_t)(esp_timer_get_time() / 1000);
    wifi_retry_pending = false;
    wifi_connect_pending = true;
    wifi_connect_not_before_ms = (uint32_t)(esp_timer_get_time() / 1000) + 1000;
    wifi_connect_force = true;
    ESP_LOGI(TAG, "Wi-Fi association scheduled after setup response");
    ESP_LOGI(TAG, "Applied new Wi-Fi configuration without restarting the device");
    return api_reply(req, "200 OK", true, "配置已保存，正在连接 Wi-Fi；配网热点和 USB 连接会保持开启");
}

static void start_ap(void)
{
    if (ap_active) return;
    wifi_config_t ap = {0};
    snprintf((char *)ap.ap.ssid, sizeof(ap.ap.ssid), "WOL-Setup-%.6s", wol_mac + 6);
    ap.ap.ssid_len = strlen((char *)ap.ap.ssid);
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ap.ap.channel = 1;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    // Keep RF power bounded while the USB-powered board runs provisioning AP + STA.
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(60));
    ap_active = true;
    ESP_LOGI(TAG, "Setup AP: %s (no password); open http://192.168.4.1", ap.ap.ssid);
}

static void recovery_task(void *arg)
{
    // USB Serial/JTAG may miss early boot output while it re-enumerates.
    vTaskDelay(pdMS_TO_TICKS(2000));
    ESP_LOGI(TAG, "Boot diagnostics: reset_reason=%d", esp_reset_reason());
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000000;
        uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
        if (wifi_setup_active && !has_ip && (uint32_t)(now_ms - wifi_config_started_ms) >= 20000) {
            wifi_setup_active = false;
            wifi_setup_failed = true;
            wifi_setup_timed_out = true;
            wifi_connect_pending = false;
            wifi_connect_force = false;
            wifi_retry_pending = false;
            wifi_station_connecting = false;
            esp_wifi_disconnect();
            ESP_LOGW(TAG, "Setup Wi-Fi attempt timed out after 20 seconds; reason=%u", wifi_disconnect_reason);
        }
        bool reconnect_transport = false;
        portENTER_CRITICAL(&recovery_lock);
        if (wol_mqtt_ready() || !has_ip) transport_timeouts = 0;
        if (wol_transport_recovery_due(transport_timeouts, first_transport_timeout,
                last_transport_recovery, now * 1000, has_ip, setup_clients > 0 || wifi_scanning)) {
            reconnect_transport = true;
            transport_timeouts = 0;
            last_transport_recovery = now * 1000;
        }
        portEXIT_CRITICAL(&recovery_lock);
        if (reconnect_transport) {
            ESP_LOGW(TAG, "Repeated MQTT transport timeouts; reassociating Wi-Fi, heap=%u largest=%u",
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
            // Use the normal disconnect/retry path; preserve NVS and the setup AP.
            esp_err_t err = esp_wifi_disconnect();
            if (err != ESP_OK) ESP_LOGW(TAG, "Wi-Fi reassociation failed: %s", esp_err_to_name(err));
        }
        // Serialize starting STA association against accepting a portal scan.
        xSemaphoreTake(wifi_scan_lock, portMAX_DELAY);
        bool explicit_due = wifi_connect_pending && (!wifi_retry_pending || wifi_connect_force) &&
            (setup_clients == 0 || wifi_connect_force) && (int32_t)(now_ms - wifi_connect_not_before_ms) >= 0;
        bool retry_due = wifi_retry_pending && (setup_clients == 0 || wifi_setup_active) &&
            (int32_t)(now_ms - wifi_retry_at_ms) >= 0;
        // Both pending paths can become due when a phone leaves the setup AP.
        // Start at most one association and preserve busy until its IP/disconnect event.
        if (!wifi_scanning && !has_ip && !wifi_station_connecting && (explicit_due || retry_due)) {
            wifi_connect_pending = false;
            wifi_connect_force = false;
            wifi_retry_pending = false;
            wifi_station_connecting = true;
            esp_err_t err = esp_wifi_connect();
            if (err != ESP_OK) {
                wifi_station_connecting = false;
                ESP_LOGW(TAG, "Wi-Fi association could not start: %s", esp_err_to_name(err));
                wifi_retry_at_ms = now_ms + (wifi_setup_active ? 1500 : 30000);
                wifi_retry_pending = true;
            } else {
                ESP_LOGI(TAG, "Starting Wi-Fi association; explicit=%u setup_clients=%u", wifi_setup_active, setup_clients);
            }
        }
        xSemaphoreGive(wifi_scan_lock);
        if (has_ip && wol_mqtt_bound()) {
            lost_since = now;
            if (!healthy_since) healthy_since = now;
            // The portal reports this same grace period before disconnecting
            // provisioning clients. Station Wi-Fi and MQTT remain connected.
            if (ap_active && have_credentials && now - healthy_since >= SETUP_AP_CLOSE_SECONDS) {
                ap_active = false;
                ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
                ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(80));
                setup_clients = 0;
                ESP_LOGI(TAG, "Setup AP closed after stable connection");
            }
        } else {
            healthy_since = 0;
            if (now - lost_since >= 60) start_ap();
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void wol_network_start(void)
{
    wifi_scan_lock = xSemaphoreCreateMutex();
    configASSERT(wifi_scan_lock);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wol_sta = esp_netif_create_default_wifi_sta();
    ap_netif = esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, event_handler, NULL));
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    wol_mac_format(mac, wol_mac);
    wol_config_t *cfg = malloc(sizeof(*cfg));
    configASSERT(cfg);
    wol_config_get(cfg);
    have_credentials = *cfg->ssid && *cfg->broker;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (have_credentials) {
        wifi_config_t sta = {0};
        // esp_wifi permits exactly 32/64 bytes without a trailing NUL.
        memcpy(sta.sta.ssid, cfg->ssid, strlen(cfg->ssid));
        memcpy(sta.sta.password, cfg->password, strlen(cfg->password));
        sta.sta.pmf_cfg.capable = true;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    }
    free(cfg);
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(60));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    wifi_connect_pending = have_credentials;
    wifi_config_started_ms = have_credentials ? (uint32_t)(esp_timer_get_time() / 1000) : 0;
    wifi_connect_not_before_ms = (uint32_t)(esp_timer_get_time() / 1000) + 1500;
    /* A first scan is safe before the setup AP has any clients.  Cache it so
     * the portal can render nearby networks without scanning underneath a
     * phone that is already connected to the hotspot. */
    ESP_LOGI(TAG, "Scanning nearby Wi-Fi before starting setup AP");
    wifi_scan_once();
    // Keep the setup page reachable while the saved configuration is verified.
    // A completed signed sync_all starts the portal's hotspot close countdown.
    start_ap();
    httpd_config_t http = HTTPD_DEFAULT_CONFIG();
    http.stack_size = 8192;
    http.max_open_sockets = 3;
    http.max_uri_handlers = 16;
    http.lru_purge_enable = true;
    httpd_handle_t server;
    ESP_ERROR_CHECK(httpd_start(&server, &http));
    const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = portal_get},
        {.uri = "/api/info", .method = HTTP_GET, .handler = info_get},
        {.uri = "/api/setup", .method = HTTP_POST, .handler = setup_post},
        {.uri = "/api/wifi/scan", .method = HTTP_POST, .handler = wifi_scan_start},
        {.uri = "/api/wifi/scan", .method = HTTP_GET, .handler = wifi_scan_get},
        {.uri = "/api/binding", .method = HTTP_POST, .handler = binding_post},
        {.uri = "/generate_204", .method = HTTP_GET, .handler = portal_redirect},
        {.uri = "/gen_204", .method = HTTP_GET, .handler = portal_redirect},
        /* Apple CNA opens this URL in a restricted web view.  Return the
         * portal itself instead of a redirect so local-only setup networks
         * remain reachable when iOS reports there is no Internet. */
        {.uri = "/hotspot-detect.html", .method = HTTP_GET, .handler = portal_get},
        {.uri = "/library/test/success.html", .method = HTTP_GET, .handler = portal_redirect},
        {.uri = "/connecttest.txt", .method = HTTP_GET, .handler = portal_redirect},
        {.uri = "/ncsi.txt", .method = HTTP_GET, .handler = portal_redirect},
        {.uri = "/redirect", .method = HTTP_GET, .handler = portal_redirect},
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++)
        ESP_ERROR_CHECK(httpd_register_uri_handler(server, &routes[i]));
    ESP_ERROR_CHECK(httpd_register_err_handler(server, HTTPD_404_NOT_FOUND, portal_not_found));
    wol_captive_dns_start(ap_netif);
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, CONFIG_WOL_NTP_SERVER);
    esp_sntp_setservername(1, "pool.ntp.org");
    esp_sntp_set_time_sync_notification_cb(time_sync_notification);
    esp_sntp_init();
    configASSERT(xTaskCreate(recovery_task, "wifi_recovery", 3072, NULL, 3, NULL) == pdPASS);
}
