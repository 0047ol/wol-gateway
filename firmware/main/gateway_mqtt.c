#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "wol.h"
#include "esp_crt_bundle.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_tls_errors.h"
#include "esp_heap_caps.h"
#include "freertos/queue.h"
#include "psa/crypto.h"
#include "mqtt_client.h"

static const char *TAG = "gateway_mqtt";
extern const char enroll_ca_start[] asm("_binary_enroll_ca_pem_start");
static esp_mqtt_client_handle_t client;
static QueueHandle_t commands;
static SemaphoreHandle_t client_lock;
static volatile bool connected, subscribed, synced;
static int subscribe_id, result_subscribe_id, pair_subscribe_id;
static bool command_subscription, result_subscription, pair_subscription;
static int64_t last_register, last_heartbeat;
static char topic_cmd[80];
static char topic_result[80];
static char topic_pair[80];
static char *incoming;
static size_t incoming_length;
static size_t incoming_capacity;
// Bound aggregate raw payload memory, not just the number of queue entries.
#define COMMAND_BUFFER_BUDGET (32 * 1024)
static size_t command_buffer_bytes;
static portMUX_TYPE buffer_lock = portMUX_INITIALIZER_UNLOCKED;
static bool incoming_result;
static bool incoming_pair;
typedef struct { char *raw; size_t capacity; bool pairing; } queued_command_t;
static volatile int published_ids[16];
static unsigned published_index;

typedef struct {
    char id[96];
    char sign[65];
    char cmd_type[32];
    char msg[96];
    bool ok;
} command_result_t;
static command_result_t recent[16];
static unsigned recent_index;
static SemaphoreHandle_t recent_lock;
static volatile bool scan_running;
static volatile bool enrolling;
static volatile bool enrollment_pending;

static char scan_id[96];
static portMUX_TYPE binding_lock = portMUX_INITIALIZER_UNLOCKED;
static char binding_code[32] = "waiting_server";
static char registration_request[33];
static int64_t binding_wait_since;
static bool retry_requested;
static uint32_t binding_generation;

static bool binding_terminal_error(const char *code)
{
    static const char *errors[] = {
        "pairing_invalid", "pairing_expired", "pairing_used", "wrong_device",
        "gateway_not_found", "key_mismatch", "device_already_bound",
        "version_unsupported", "sync_unavailable", "config_failed"
    };
    if (!code) return false;
    for (size_t i = 0; i < sizeof(errors) / sizeof(errors[0]); i++)
        if (strcmp(code, errors[i]) == 0) return true;
    return false;
}

static char *command_buffer_alloc(size_t capacity)
{
    portENTER_CRITICAL(&buffer_lock);
    bool allowed = capacity <= COMMAND_BUFFER_BUDGET - command_buffer_bytes;
    if (allowed) command_buffer_bytes += capacity;
    portEXIT_CRITICAL(&buffer_lock);
    if (!allowed) return NULL;
    char *raw = malloc(capacity);
    if (!raw) {
        portENTER_CRITICAL(&buffer_lock);
        command_buffer_bytes -= capacity;
        portEXIT_CRITICAL(&buffer_lock);
    }
    return raw;
}

static void command_buffer_free(char *raw, size_t capacity)
{
    if (!raw) return;
    free(raw);
    portENTER_CRITICAL(&buffer_lock);
    command_buffer_bytes -= capacity;
    portEXIT_CRITICAL(&buffer_lock);
}

bool wol_mqtt_ready(void) { return connected && subscribed; }
bool wol_mqtt_bound(void) { return connected && subscribed && synced; }

void wol_binding_retry(void)
{
    portENTER_CRITICAL(&binding_lock);
    registration_request[0] = 0;
    strlcpy(binding_code, "waiting_server", sizeof(binding_code));
    binding_wait_since = 0;
    retry_requested = true;
    binding_generation++;
    synced = false;
    portEXIT_CRITICAL(&binding_lock);
}

static bool binding_retryable_error(const char *code)
{
    return code && (strcmp(code, "enrollment_unavailable") == 0 ||
                    strcmp(code, "enrollment_network_error") == 0);
}

void wol_binding_error(const char *code)
{
    if (!binding_terminal_error(code) && !binding_retryable_error(code)) return;
    portENTER_CRITICAL(&binding_lock);
    strlcpy(binding_code, code, sizeof(binding_code));
    binding_wait_since = 0;
    synced = false;
    portEXIT_CRITICAL(&binding_lock);
}

bool wol_binding_blocked(void)
{
    char code[32];
    portENTER_CRITICAL(&binding_lock);
    strlcpy(code, binding_code, sizeof(code));
    portEXIT_CRITICAL(&binding_lock);
    return binding_terminal_error(code);
}

void wol_binding_info(cJSON *obj)
{
    char code[32];
    int64_t waiting;
    portENTER_CRITICAL(&binding_lock);
    strlcpy(code, binding_code, sizeof(code));
    waiting = binding_wait_since;
    portEXIT_CRITICAL(&binding_lock);
    // Preserve a server-reported terminal error until the user explicitly
    // submits a new pairing value. Otherwise the next status poll would turn
    // pairing_invalid back into connecting_mqtt and leave the portal loading.
    bool terminal_error = binding_terminal_error(code) || binding_retryable_error(code);
    if (!terminal_error && !wol_network_ready()) strlcpy(code, "connecting_wifi", sizeof(code));
    else if (!terminal_error && !wol_network_clock_valid())
        strlcpy(code, wol_network_time_sync_expired() ? "time_sync_failed" : "syncing_time", sizeof(code));
    else if (!terminal_error && enrollment_pending) strlcpy(code, "enrolling_credentials", sizeof(code));
    else if (!terminal_error && !wol_mqtt_ready()) strlcpy(code, "connecting_mqtt", sizeof(code));
    else if (!terminal_error && synced) strlcpy(code, "bound", sizeof(code));
    else if (!terminal_error && waiting && esp_timer_get_time() / 1000000 - waiting > 30) {
        if (strcmp(code, "waiting_server") == 0) strlcpy(code, "server_timeout", sizeof(code));
        else if (strcmp(code, "waiting_config") == 0) strlcpy(code, "config_timeout", sizeof(code));
    }
    cJSON_AddStringToObject(obj, "binding_status", code);
    cJSON_AddBoolToObject(obj, "bound", wol_mqtt_bound());
}

static void registration_result(const char *raw)
{
    cJSON *obj = cJSON_Parse(raw);
    const char *id = wol_json_string(obj, "request_id");
    const char *code = wol_json_string(obj, "code");
    bool accepted = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(obj, "ok"));
    portENTER_CRITICAL(&binding_lock);
    if (*registration_request && strcmp(id, registration_request) == 0 && !synced) {
        if (accepted && strcmp(code, "accepted") == 0 && strcmp(binding_code, "config_failed") != 0)
            strlcpy(binding_code, "waiting_config", sizeof(binding_code));
        else if (!accepted && (strcmp(code, "key_mismatch") == 0 || strcmp(code, "gateway_not_found") == 0 ||
                               strcmp(code, "sync_unavailable") == 0 || strcmp(code, "version_unsupported") == 0 || strcmp(code, "pairing_invalid") == 0 ||
                               strcmp(code, "pairing_expired") == 0 || strcmp(code, "pairing_used") == 0 ||
                               strcmp(code, "device_already_bound") == 0 || strcmp(code, "wrong_device") == 0))
            strlcpy(binding_code, code, sizeof(binding_code));
    }
    portEXIT_CRITICAL(&binding_lock);
    cJSON_Delete(obj);
}

static int publish_json(const char *suffix, const cJSON *body, int qos)
{
    if (!wol_mqtt_ready()) return -1;
    char topic[96];
    snprintf(topic, sizeof(topic), "wol/gateway/%s/%s", wol_mac, suffix);
    cJSON *versioned = cJSON_Duplicate(body, true);
    if (!versioned) return -1;
    cJSON_DeleteItemFromObjectCaseSensitive(versioned, "firmware_version");
    cJSON_AddStringToObject(versioned, "firmware_version", esp_app_get_description()->version);
    char *raw = cJSON_PrintUnformatted(versioned);
    cJSON_Delete(versioned);
    if (!raw) return -1;
    xSemaphoreTake(client_lock, portMAX_DELAY);
    int id = client && wol_mqtt_ready() ? esp_mqtt_client_enqueue(client, topic, raw, 0, qos, 0, true) : -1;
    xSemaphoreGive(client_lock);
    free(raw);
    return id;
}

bool wol_publish(const char *suffix, const cJSON *body, int qos)
{
    return publish_json(suffix, body, qos) >= 0;
}

bool wol_publish_live(const char *suffix, const cJSON *body)
{
    // QoS0 direct publish: a stale presence snapshot must never enter the outbox.
    if (!wol_mqtt_ready() || !body) return false;
    char topic[96];
    snprintf(topic, sizeof(topic), "wol/gateway/%s/%s", wol_mac, suffix);
    cJSON *copy = cJSON_Duplicate(body, true);
    if (!copy) return false;
    cJSON_AddStringToObject(copy, "firmware_version", esp_app_get_description()->version);
    char *raw = cJSON_PrintUnformatted(copy);
    cJSON_Delete(copy);
    if (!raw) return false;
    int result = -1;
    if (xSemaphoreTake(client_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (client && wol_mqtt_ready() && esp_mqtt_client_get_outbox_size(client) < 8192)
            result = esp_mqtt_client_publish(client, topic, raw, 0, 0, 0);
        xSemaphoreGive(client_lock);
    }
    free(raw);
    return result >= 0;
}

static void ack(const command_result_t *result, bool wait)
{
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "cmd_id", result->id);
    cJSON_AddStringToObject(body, "cmd_type", result->cmd_type);
    cJSON_AddBoolToObject(body, "ok", result->ok);
    cJSON_AddStringToObject(body, "msg", result->msg);
    int id = publish_json("cmd/ack", body, 1);
    cJSON_Delete(body);
    // Give QoS 1 ACK a chance to reach the broker before changing Wi-Fi or resetting.
    int64_t deadline = esp_timer_get_time() + 5000000;
    while (wait && id >= 0 && connected && esp_timer_get_time() < deadline) {
        for (int i = 0; i < 16; i++) if (published_ids[i] == id) return;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static void remember(const command_result_t *result)
{
    xSemaphoreTake(recent_lock, portMAX_DELAY);
    recent[recent_index++ % 16] = *result;
    xSemaphoreGive(recent_lock);
}

static const char *result_message(esp_err_t err, const char *type)
{
    if (err == ESP_ERR_INVALID_ARG) return "参数无效或超出设备支持范围";
    if (err == ESP_ERR_INVALID_STATE) return "Wi-Fi 已断开或尚未获取 IP 地址，请检查网关网络连接";
    if (err == ESP_ERR_NOT_FINISHED) return "已有扫描任务正在执行，请等待当前扫描完成后重试";
    if (err == ESP_ERR_TIMEOUT) return "局域网扫描等待超时，任务已停止并释放资源，请稍后重试";
    if (err == ESP_ERR_NO_MEM) return "设备内存不足，请稍后重试";
    if (err == ESP_ERR_NOT_SUPPORTED) return "设备不支持此指令";
    if (err != ESP_OK) return "操作失败，请检查网络或设备日志";
    if (strcmp(type, "scan") == 0) return "扫描完成";
    if (strcmp(type, "wake") == 0) return "唤醒包已发送，等待电脑上线";
    if (strcmp(type, "reset") == 0) return "即将清除配置并重启";
    if (strcmp(type, "update_wifi") == 0) return "Wi-Fi 已保存，即将重启连接";
    return "配置已保存";
}

static void scan_task(void *arg)
{
    command_result_t *result = arg;
    int64_t scan_started = esp_timer_get_time();
    ESP_LOGI(TAG, "Starting LAN scan command");
    cJSON *start = cJSON_CreateObject();
    if (start) {
        cJSON_AddStringToObject(start, "cmd_id", result->id);
        cJSON_AddBoolToObject(start, "partial", true);
        cJSON_AddBoolToObject(start, "reset", true);
        cJSON_AddArrayToObject(start, "hosts");
        wol_publish("scan/result", start, 1);
        cJSON_Delete(start);
    }
    cJSON *hosts = NULL;
    esp_err_t err = wol_scan(&hosts, false, result->id);
    if (err == ESP_OK) {
        cJSON *done = cJSON_CreateObject();
        if (done) {
            cJSON_AddStringToObject(done, "cmd_id", result->id);
            cJSON_AddBoolToObject(done, "partial", true);
            cJSON_AddBoolToObject(done, "done", true);
            cJSON_AddArrayToObject(done, "hosts");
            if (!wol_publish("scan/result", done, 1)) err = ESP_FAIL;
            cJSON_Delete(done);
        } else {
            err = ESP_ERR_NO_MEM;
        }
    }
    cJSON_Delete(hosts);
    result->ok = err == ESP_OK;
    strlcpy(result->msg, result_message(err, "scan"), sizeof(result->msg));
    ESP_LOGI(TAG, "LAN scan command finished: ok=%d elapsed_ms=%lld",
             result->ok, (long long)((esp_timer_get_time() - scan_started) / 1000));
    remember(result);
    ack(result, false);
    free(result);
    scan_running = false;
    vTaskDelete(NULL);
}

static cJSON *verify(const cJSON *envelope, const wol_settings_t *cfg)
{
    const char *raw = wol_json_string(envelope, "signed_body");
    const char *sign = wol_json_string(envelope, "sign");
    if (!*raw || strlen(sign) != 64) return NULL;
    cJSON *body = cJSON_Parse(raw);
    if (!cJSON_IsObject(body)) { cJSON_Delete(body); return NULL; }
    const char *timestamp = wol_json_string(body, "timestamp");
    char *end;
    int64_t ts = strtoll(timestamp, &end, 10), now = time(NULL);
    if (!*timestamp || *end || now < 1704067200 || ts < now - 60 || ts > now + 60) goto invalid;
    unsigned char digest[32];
    psa_hash_operation_t ctx = PSA_HASH_OPERATION_INIT;
    size_t digest_length = 0;
    psa_status_t err = psa_hash_setup(&ctx, PSA_ALG_SHA_256);
    if (err == PSA_SUCCESS) err = psa_hash_update(&ctx, (const unsigned char *)cfg->bind_key, strlen(cfg->bind_key));
    if (err == PSA_SUCCESS) err = psa_hash_update(&ctx, (const unsigned char *)timestamp, strlen(timestamp));
    if (err == PSA_SUCCESS) err = psa_hash_update(&ctx, (const unsigned char *)raw, strlen(raw));
    if (err == PSA_SUCCESS) err = psa_hash_finish(&ctx, digest, sizeof(digest), &digest_length);
    psa_hash_abort(&ctx);
    if (err != PSA_SUCCESS || digest_length != sizeof(digest)) goto invalid;
    char expected[65];
    for (int i = 0; i < 32; i++) snprintf(expected + i * 2, 3, "%02x", digest[i]);
    unsigned diff = 0;
    for (int i = 0; i < 64; i++) diff |= expected[i] ^ sign[i];
    if (diff) goto invalid;
    const char *id = wol_json_string(body, "cmd_id"), *type = wol_json_string(body, "cmd_type");
    if (!*id || strlen(id) >= sizeof(recent[0].id) || !*type || strlen(type) >= sizeof(recent[0].cmd_type) ||
        !cJSON_IsObject(cJSON_GetObjectItemCaseSensitive(body, "payload"))) goto invalid;
    return body;
invalid:
    cJSON_Delete(body);
    return NULL;
}

static void command_task(void *arg)
{
    wol_settings_t *cfg = malloc(sizeof(*cfg));
    configASSERT(cfg);
    queued_command_t message;
    for (;;) {
        if (xQueueReceive(commands, &message, portMAX_DELAY) != pdTRUE) continue;
        cJSON *envelope = cJSON_Parse(message.raw);
        command_buffer_free(message.raw, message.capacity);
        wol_settings_get(cfg);
        bool pairing_attempt = *cfg->pairing_code != 0;
        if (message.pairing != pairing_attempt) { cJSON_Delete(envelope); continue; }
        char pairing_secret[17];
        strlcpy(pairing_secret, cfg->pairing_code, sizeof(pairing_secret));
        if (pairing_attempt) strlcpy(cfg->bind_key, cfg->pairing_code, sizeof(cfg->bind_key));
        cJSON *body = verify(envelope, cfg);
        if (!body) {
            ESP_LOGW(TAG, "Rejected command: invalid signature, timestamp or format (check NTP)");
            cJSON_Delete(envelope);
            continue;
        }
        const char *id = wol_json_string(body, "cmd_id"), *type = wol_json_string(body, "cmd_type");
        if (pairing_attempt) {
            const cJSON *payload = cJSON_GetObjectItemCaseSensitive(body, "payload");
            const char *request = wol_json_string(payload, "pair_request_id");
            portENTER_CRITICAL(&binding_lock);
            bool matches = *registration_request && strcmp(request, registration_request) == 0;
            portEXIT_CRITICAL(&binding_lock);
            if (matches && strcmp(type, "pairing_complete") == 0 &&
                wol_config_finish_pairing(pairing_secret, wol_json_string(payload, "bind_key")) == ESP_OK) {
                wol_binding_retry();
                ESP_LOGI(TAG, "Pairing complete; device credential saved, registering");
            }
            cJSON_Delete(body);
            cJSON_Delete(envelope);
            continue;
        }
        const char *sign = wol_json_string(envelope, "sign");
        bool duplicate = false;
        command_result_t saved = {0};
        xSemaphoreTake(recent_lock, portMAX_DELAY);
        for (int i = 0; i < 16; i++) {
            if (strcmp(recent[i].id, id) == 0) {
                saved = recent[i];
                duplicate = true;
                break;
            }
        }
        xSemaphoreGive(recent_lock);
        if (duplicate && strcmp(saved.sign, sign) == 0) ack(&saved, false);
        if (scan_running && strcmp(scan_id, id) == 0) duplicate = true;
        if (duplicate) { cJSON_Delete(body); cJSON_Delete(envelope); continue; }
        command_result_t result = {0};
        strlcpy(result.id, id, sizeof(result.id));
        strlcpy(result.sign, sign, sizeof(result.sign));
        strlcpy(result.cmd_type, type, sizeof(result.cmd_type));
        const cJSON *payload = cJSON_GetObjectItemCaseSensitive(body, "payload");
        esp_err_t err = ESP_ERR_NOT_SUPPORTED;
        bool reboot = false, reset = false;
        if (wol_ota_active() && strcmp(type, "sync_all") != 0) {
            err = ESP_ERR_INVALID_STATE;
        } else if (strcmp(type, "ota_start") == 0) {
            err = scan_running ? ESP_ERR_INVALID_STATE : wol_ota_start(payload);
        } else if (strcmp(type, "sync_all") == 0 || strcmp(type, "sync_sub_devices") == 0 || strcmp(type, "update_config") == 0) {
            uint32_t generation;
            const char *request = wol_json_string(payload, "registration_request_id");
            portENTER_CRITICAL(&binding_lock);
            generation = binding_generation;
            // Older servers omit the request ID; their signed configuration still
            // proves possession of the current key. New servers must echo our ID.
            bool current_request = *registration_request && (!*request || strcmp(request, registration_request) == 0);
            portEXIT_CRITICAL(&binding_lock);
            if (strcmp(type, "sync_all") == 0 && !current_request) {
                // Ignore a signed response to an attempt superseded by a key change.
                cJSON_Delete(body);
                cJSON_Delete(envelope);
                continue;
            }
            err = wol_config_apply(payload, strcmp(type, "update_config") != 0, strcmp(type, "sync_sub_devices") != 0);
            if (strcmp(type, "sync_all") == 0) {
                portENTER_CRITICAL(&binding_lock);
                if (binding_generation == generation) {
                    synced = err == ESP_OK;
                    strlcpy(binding_code, synced ? "bound" : "config_failed", sizeof(binding_code));
                }
                portEXIT_CRITICAL(&binding_lock);
            }
        } else if (strcmp(type, "presence_check") == 0) {
            wol_monitor_request();
        } else if (strcmp(type, "wake") == 0) {
            err = wol_wake(payload);
        } else if (strcmp(type, "scan") == 0) {
            err = ESP_ERR_NOT_FINISHED;
            if (!scan_running) {
                command_result_t *task_result = malloc(sizeof(*task_result));
                err = ESP_ERR_NO_MEM;
                if (task_result) {
                    *task_result = result;
                    wol_scan_cancel_reset();
                    strlcpy(scan_id, id, sizeof(scan_id));
                    scan_running = true;
                    ESP_LOGI(TAG, "Accepted LAN scan command");
                    if (xTaskCreate(scan_task, "arp_scan", 6144, task_result, 3, NULL) == pdPASS) {
                        cJSON_Delete(body);
                        cJSON_Delete(envelope);
                        continue;
                    }
                    scan_running = false;
                    free(task_result);
                }
            } else {
                ESP_LOGW(TAG, "Rejected LAN scan command: another scan is active");
            }
        } else if (strcmp(type, "scan_cancel") == 0) {
            const char *target = wol_json_string(payload, "scan_cmd_id");
            if (scan_running && *target && strcmp(scan_id, target) == 0) {
                wol_scan_cancel();
                err = ESP_OK;
            } else err = ESP_ERR_INVALID_STATE;
        } else if (strcmp(type, "update_wifi") == 0) {
            const char *ssid = wol_json_string(payload, "ssid"), *password = wol_json_string(payload, "password");
            err = wol_config_set_wifi(ssid, password);
            reboot = err == ESP_OK;
        } else if (strcmp(type, "reset") == 0) {
            err = ESP_OK;
            reset = reboot = true;
        }
        result.ok = err == ESP_OK;
        strlcpy(result.msg, result_message(err, type), sizeof(result.msg));
        remember(&result);
        ack(&result, reboot);
        ESP_LOGI(TAG, "%s -> %s", type, esp_err_to_name(err));
        cJSON_Delete(body);
        cJSON_Delete(envelope);
        if (reset) wol_config_reset();
        if (reboot) {
            wol_reboot_later();
            vTaskDelay(portMAX_DELAY);
        }
    }
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    switch (event_id) {
    case MQTT_EVENT_CONNECTED:
        connected = true;
        subscribed = synced = false;
        command_subscription = result_subscription = pair_subscription = false;
        last_register = last_heartbeat = 0;
        subscribe_id = esp_mqtt_client_subscribe(client, topic_cmd, 1);
        result_subscribe_id = esp_mqtt_client_subscribe(client, topic_result, 1);
        pair_subscribe_id = esp_mqtt_client_subscribe(client, topic_pair, 1);
        ESP_LOGI(TAG, "MQTT connected");
        break;
    case MQTT_EVENT_SUBSCRIBED:
        if (event->msg_id == subscribe_id) command_subscription = true;
        if (event->msg_id == result_subscribe_id) result_subscription = true;
        if (event->msg_id == pair_subscribe_id) pair_subscription = true;
        subscribed = command_subscription && result_subscription && pair_subscription;
        break;
    case MQTT_EVENT_DISCONNECTED:
        connected = subscribed = synced = false;
        portENTER_CRITICAL(&binding_lock);
        registration_request[0] = 0;
        if (!binding_terminal_error(binding_code) && !binding_retryable_error(binding_code))
            strlcpy(binding_code, "waiting_server", sizeof(binding_code));
        binding_wait_since = 0;
        binding_generation++;
        portEXIT_CRITICAL(&binding_lock);
        command_buffer_free(incoming, incoming_capacity);
        incoming = NULL;
        incoming_length = incoming_capacity = 0;
        ESP_LOGW(TAG, "MQTT disconnected; will reconnect");
        break;
    case MQTT_EVENT_PUBLISHED:
        published_ids[published_index++ % 16] = event->msg_id;
        break;
    case MQTT_EVENT_DATA:
        if (event->current_data_offset == 0) {
            command_buffer_free(incoming, incoming_capacity);
            incoming = NULL;
            incoming_length = incoming_capacity = 0;
            bool is_command = event->topic_len == strlen(topic_cmd) && memcmp(event->topic, topic_cmd, event->topic_len) == 0;
            incoming_result = event->topic_len == strlen(topic_result) && memcmp(event->topic, topic_result, event->topic_len) == 0;
            incoming_pair = event->topic_len == strlen(topic_pair) && memcmp(event->topic, topic_pair, event->topic_len) == 0;
            if ((!is_command && !incoming_result && !incoming_pair) || event->total_data_len <= 0 ||
                event->total_data_len > (incoming_result ? 1024 : WOL_MAX_MESSAGE) || event->retain) break;
            incoming_capacity = (size_t)event->total_data_len + 1;
            incoming = command_buffer_alloc(incoming_capacity);
            if (!incoming) ESP_LOGW(TAG, "MQTT receive budget exhausted; dropping payload");
        }
        if (!incoming) break;
        if (event->current_data_offset != incoming_length || event->data_len < 0 ||
            (size_t)event->total_data_len + 1 != incoming_capacity ||
            incoming_length + event->data_len >= incoming_capacity) {
            command_buffer_free(incoming, incoming_capacity);
            incoming = NULL; incoming_length = incoming_capacity = 0; break;
        }
        memcpy(incoming + incoming_length, event->data, event->data_len);
        incoming_length += event->data_len;
        incoming[incoming_length] = 0;
        if (incoming_length == event->total_data_len) {
            bool signed_pairing = false;
            if (incoming_pair) {
                cJSON *envelope = cJSON_Parse(incoming);
                signed_pairing = cJSON_IsString(cJSON_GetObjectItemCaseSensitive(envelope, "signed_body"));
                cJSON_Delete(envelope);
            }
            queued_command_t message = {.raw = incoming, .capacity = incoming_capacity, .pairing = incoming_pair};
            if (incoming_result || (incoming_pair && !signed_pairing)) {
                registration_result(incoming);
                command_buffer_free(incoming, incoming_capacity);
            } else if (xQueueSend(commands, &message, 0) != pdPASS) {
                ESP_LOGW(TAG, "Command queue full");
                command_buffer_free(incoming, incoming_capacity);
            }
            incoming = NULL;
            incoming_length = incoming_capacity = 0;
        }
        break;
    case MQTT_EVENT_ERROR:
        if (event->error_handle && event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGW(TAG, "MQTT transport error: esp=0x%x tls=0x%x errno=%d heap=%u largest=%u",
                     event->error_handle->esp_tls_last_esp_err,
                     event->error_handle->esp_tls_stack_err,
                     event->error_handle->esp_transport_sock_errno,
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
            if (event->error_handle->esp_tls_last_esp_err == ESP_ERR_ESP_TLS_CONNECTION_TIMEOUT)
                wol_network_transport_timeout();
        } else ESP_LOGW(TAG, "MQTT error; verify broker account and network");
        break;
    default:
        break;
    }
}

static void service_task(void *arg)
{
    wol_settings_t *cfg = malloc(sizeof(*cfg));
    configASSERT(cfg);
    bool started = false;
    char active_broker[192] = {0}, active_user[65] = {0}, active_password[129] = {0};
    int warning_counter = 0;
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000000;
        uint32_t generation;
        portENTER_CRITICAL(&binding_lock);
        generation = binding_generation;
        if (retry_requested) {
            last_register = 0;
            retry_requested = false;
        }
        portEXIT_CRITICAL(&binding_lock);
        wol_settings_get(cfg);
        // The setup page applies settings without rebooting. Recreate the transport
        // when its endpoint/account changes, instead of using stale credentials.
        // Enrollment must run before MQTT can receive its first command.
        enrollment_pending = *cfg->pairing_code || !*cfg->mqtt_username || !*cfg->mqtt_password ||
            strcmp(cfg->mqtt_username,wol_mac) != 0;
        if (wol_network_ready() && enrollment_pending) {
            enrolling = true;
            if (wol_enroll()) { wol_settings_get(cfg); enrollment_pending = false; wol_binding_retry(); }
            enrolling = false;
        }
        if (started && (strcmp(active_broker, cfg->broker) != 0 ||
                        strcmp(active_user, cfg->mqtt_username) != 0 ||
                        strcmp(active_password, cfg->mqtt_password) != 0)) {
            xSemaphoreTake(client_lock, portMAX_DELAY);
            connected = subscribed = synced = false;
            esp_mqtt_client_stop(client);
            esp_mqtt_client_destroy(client);
            client = NULL;
            started = false;
            xSemaphoreGive(client_lock);
            wol_binding_retry();
            ESP_LOGI(TAG, "MQTT endpoint/account changed; restarting transport");
        }
        bool clock_ready = wol_network_clock_valid();
        if (!started && wol_network_ready() && *cfg->broker && *cfg->mqtt_username && *cfg->mqtt_password && clock_ready) {
            char client_id[40];
            snprintf(client_id, sizeof(client_id), "gateway_%s", wol_mac);
            esp_mqtt_client_config_t config = {
                .broker.address.uri = cfg->broker,
                .broker.verification.certificate = enroll_ca_start,
                .credentials.client_id = client_id,
                .credentials.username = *cfg->mqtt_username ? cfg->mqtt_username : NULL,
                .credentials.authentication.password = *cfg->mqtt_username && *cfg->mqtt_password ? cfg->mqtt_password : NULL,
                .session.keepalive = 30,
                .network.reconnect_timeout_ms = 5000,
                .buffer.size = 2048,
                .buffer.out_size = 2048,
                .outbox.limit = 65536,
            };
            xSemaphoreTake(client_lock, portMAX_DELAY);
            client = esp_mqtt_client_init(&config);
            if (client) {
                ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event, NULL));
                ESP_ERROR_CHECK(esp_mqtt_client_start(client));
                started = true;
                strlcpy(active_broker, cfg->broker, sizeof(active_broker));
                strlcpy(active_user, cfg->mqtt_username, sizeof(active_user));
                strlcpy(active_password, cfg->mqtt_password, sizeof(active_password));
            }
            xSemaphoreGive(client_lock);
        }
        if (wol_network_ready() && !clock_ready && ++warning_counter % 30 == 0)
            ESP_LOGW(TAG, "Waiting for NTP; MQTT and signed commands require correct time");
        if (wol_mqtt_ready() && clock_ready && (*cfg->bind_key || *cfg->pairing_code)) {
            // Retry binding, including when the server starts after the device.
            if (!last_register || now - last_register >= (synced ? 60 : 15)) {
                uint8_t random_id[16];
                char request_id[33];
                esp_fill_random(random_id, sizeof(random_id));
                for (int i = 0; i < 16; i++) snprintf(request_id + i * 2, 3, "%02x", random_id[i]);
                portENTER_CRITICAL(&binding_lock);
                bool current_generation = generation == binding_generation;
                if (current_generation) {
                    strlcpy(registration_request, request_id, sizeof(registration_request));
                    if (!binding_wait_since) binding_wait_since = now;
                }
                portEXIT_CRITICAL(&binding_lock);
                if (!current_generation) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
                cJSON *reg = cJSON_CreateObject();
                cJSON_AddStringToObject(reg, "request_id", request_id);
                cJSON_AddStringToObject(reg, "gateway_mac", wol_mac);
                bool pairing = *cfg->pairing_code != 0;
                cJSON_AddStringToObject(reg, pairing ? "pairing_code" : "bind_key", pairing ? cfg->pairing_code : cfg->bind_key);
                cJSON_AddStringToObject(reg, "firmware_version", esp_app_get_description()->version);
                wol_network_info(reg);
                if (wol_publish(pairing ? "pair" : "register", reg, 1)) last_register = now;
                cJSON_Delete(reg);
            }
            if (synced && (!last_heartbeat || now - last_heartbeat >= cfg->heartbeat_interval)) {
                cJSON *hb = cJSON_CreateObject();
                wol_network_info(hb);
                if (wol_publish("heartbeat", hb, 0)) last_heartbeat = now;
                cJSON_Delete(hb);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void wol_mqtt_start(void)
{
    client_lock = xSemaphoreCreateMutex();
    configASSERT(client_lock);
    snprintf(topic_cmd, sizeof(topic_cmd), "wol/gateway/%s/cmd", wol_mac);
    snprintf(topic_result, sizeof(topic_result), "wol/gateway/%s/register/result", wol_mac);
    snprintf(topic_pair, sizeof(topic_pair), "wol/gateway/%s/pair/result", wol_mac);
    commands = xQueueCreate(4, sizeof(queued_command_t));
    configASSERT(commands);
    recent_lock = xSemaphoreCreateMutex();
    configASSERT(recent_lock);
    configASSERT(xTaskCreate(command_task, "commands", 8192, NULL, 4, NULL) == pdPASS);
    configASSERT(xTaskCreate(service_task, "mqtt_service", 10240, NULL, 3, NULL) == pdPASS);
}
