#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdatomic.h>
#include "wol.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#define OTA_LAYOUT "c3-4m-ab-v1"
#define OTA_SLOT_SIZE 0x1f0000
#define OTA_URL "https://YOUR_SERVER_HOST:8443/api/firmware/"
#define OTA_TIMEOUT_US (600LL * 1000000)

extern const char enroll_ca_start[] asm("_binary_enroll_ca_pem_start");
extern const char ota_public_start[] asm("_binary_ota_public_key_pem_start");
static const char *TAG = "ota";
static atomic_bool active;
static portMUX_TYPE status_lock = portMUX_INITIALIZER_UNLOCKED;

typedef struct {
    unsigned format;
    uint32_t target_address;
    char task_id[33];
    char target_version[32];
    char sha256[65];
    char state[16];
    char error[49];
    int image_size;
    int progress;
} ota_status_t;

typedef struct {
    char token[65];
    ota_status_t status;
} ota_job_t;

static ota_status_t status;

static bool hex_bytes(const char *text, unsigned char *out, size_t count)
{
    if (strlen(text) != count * 2) return false;
    for (size_t i = 0; i < count; i++) {
        unsigned value = 0;
        for (size_t j = 0; j < 2; j++) {
            char c = text[i * 2 + j];
            if (c >= '0' && c <= '9') value = value * 16 + c - '0';
            else if (c >= 'a' && c <= 'f') value = value * 16 + c - 'a' + 10;
            else return false;
        }
        out[i] = value;
    }
    return true;
}

static bool persist_status(void)
{
    ota_status_t copy;
    portENTER_CRITICAL(&status_lock);
    copy = status;
    portEXIT_CRITICAL(&status_lock);
    nvs_handle_t handle;
    if (nvs_open("ota", NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(handle, "result", &copy, sizeof(copy));
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    return err == ESP_OK;
}

static void set_status(const char *state, int progress, const char *error)
{
    portENTER_CRITICAL(&status_lock);
    strlcpy(status.state, state, sizeof(status.state));
    strlcpy(status.error, error ? error : "", sizeof(status.error));
    status.progress = progress;
    portEXIT_CRITICAL(&status_lock);
}

bool wol_ota_active(void)
{
    return atomic_load(&active);
}

void wol_ota_info(cJSON *obj)
{
    ota_status_t copy;
    portENTER_CRITICAL(&status_lock);
    copy = status;
    portEXIT_CRITICAL(&status_lock);
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    const esp_partition_t *run = esp_ota_get_running_partition();
    cJSON *ota = cJSON_AddObjectToObject(obj, "ota");
    cJSON_AddBoolToObject(ota, "supported", next && run && next->address != run->address && next->size >= OTA_SLOT_SIZE);
    cJSON_AddStringToObject(ota, "layout", OTA_LAYOUT);
    cJSON_AddStringToObject(ota, "running_version", esp_app_get_description()->version);
    cJSON_AddStringToObject(ota, "task_id", copy.task_id);
    cJSON_AddStringToObject(ota, "state", *copy.state ? copy.state : "idle");
    cJSON_AddStringToObject(ota, "error", copy.error);
    cJSON_AddStringToObject(ota, "sha256", copy.sha256);
    cJSON_AddNumberToObject(ota, "progress", copy.progress);
}

static void publish_status(void)
{
    cJSON *body = cJSON_CreateObject();
    if (!body) return;
    wol_network_info(body);
    wol_publish("heartbeat", body, 1);
    cJSON_Delete(body);
}

static bool partition_matches(const esp_partition_t *partition, int image_size, const char *expected)
{
    if (!partition || image_size < 256 || image_size > partition->size) return false;
    unsigned char wanted[32], actual[32];
    if (!hex_bytes(expected, wanted, 32)) return false;
    unsigned char *buffer = malloc(2048);
    if (!buffer) return false;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    bool ok = mbedtls_sha256_starts(&sha, 0) == 0;
    for (int offset = 0; ok && offset < image_size; offset += 2048) {
        size_t count = image_size - offset < 2048 ? image_size - offset : 2048;
        ok = esp_partition_read(partition, offset, buffer, count) == ESP_OK && mbedtls_sha256_update(&sha, buffer, count) == 0;
        vTaskDelay(1);
    }
    if (ok) ok = mbedtls_sha256_finish(&sha, actual) == 0 && memcmp(actual, wanted, 32) == 0;
    mbedtls_sha256_free(&sha);
    free(buffer);
    return ok;
}

static void boot_verify(void *arg)
{
    (void)arg;
    ota_status_t copy = status;
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (!running || running->address != copy.target_address || !partition_matches(running, copy.image_size, copy.sha256)) {
        set_status("rolled_back", 0, "");
        persist_status();
        atomic_store(&active, false);
        vTaskDelete(NULL);
        return;
    }
    set_status("validating", 99, "");
    bool healthy = false;
    for (int seconds = 0; seconds < 120; seconds++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        // Broker outages alone must not invalidate a working application.
        if (seconds >= 10 && wol_network_ready() && (wol_mqtt_bound() || seconds >= 90)) {
            healthy = true;
            break;
        }
    }
    if (healthy && esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        set_status("success", 100, "");
        persist_status();
        atomic_store(&active, false);
        publish_status();
        ESP_LOGI(TAG, "New application verified; heap=%u minimum=%u stack=%u",
                 (unsigned)esp_get_free_heap_size(), (unsigned)esp_get_minimum_free_heap_size(), (unsigned)uxTaskGetStackHighWaterMark(NULL));
    } else {
        ESP_LOGE(TAG, "New application did not pass startup checks; rolling back");
        // Keep the reboot marker for the previous slot to report the rollback.
        esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
        set_status("failed", 0, "rollback_unavailable");
        persist_status();
        atomic_store(&active, false);
        ESP_LOGE(TAG, "Rollback unavailable: %s", esp_err_to_name(err));
    }
    vTaskDelete(NULL);
}

void wol_ota_init(void)
{
    nvs_handle_t handle;
    size_t length = sizeof(status);
    if (nvs_open("ota", NVS_READONLY, &handle) == ESP_OK) {
        if (nvs_get_blob(handle, "result", &status, &length) != ESP_OK || length != sizeof(status) || status.format != 2)
            memset(&status, 0, sizeof(status));
        nvs_close(handle);
    }
    if (!strcmp(status.state, "rebooting") || !strcmp(status.state, "validating")) {
        atomic_store(&active, true);
        if (xTaskCreate(boot_verify, "ota_verify", 6144, NULL, 2, NULL) != pdPASS) {
            ESP_LOGE(TAG, "Unable to start OTA self-test; rebooting to previous slot");
            esp_ota_mark_app_invalid_rollback_and_reboot();
            esp_restart();
        }
    } else if (!strcmp(status.state, "downloading") || !strcmp(status.state, "verifying")) {
        set_status("failed", 0, "interrupted");
        persist_status();
    }
}

typedef struct { char range[80]; } ota_http_headers_t;

static esp_err_t response_header(esp_http_client_event_t *event)
{
    ota_http_headers_t *headers = event->user_data;
    if (event->event_id == HTTP_EVENT_ON_HEADER && !strcasecmp(event->header_key, "Content-Range"))
        strlcpy(headers->range, event->header_value, sizeof(headers->range));
    return ESP_OK;
}

static void upgrade_task(void *arg)
{
    ota_job_t *job = arg;
    esp_http_client_handle_t client = NULL;
    esp_ota_handle_t handle = 0;
    bool writing = false;
    unsigned char *buffer = NULL;
    const char *failure = "download_failed";
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    char url[sizeof(OTA_URL) + 33], authorization[73];
    ota_http_headers_t response = {0};
    snprintf(url, sizeof(url), OTA_URL "%s", job->status.task_id);
    snprintf(authorization, sizeof(authorization), "Bearer %s", job->token);
    esp_http_client_config_t http = {
        .url = url, .cert_pem = enroll_ca_start, .timeout_ms = 15000,
        .buffer_size = 4096, .buffer_size_tx = 1024,
        .disable_auto_redirect = true,
        .keep_alive_enable = true,
        .event_handler = response_header, .user_data = &response,
    };
    vTaskDelay(pdMS_TO_TICKS(800));
    if (xSemaphoreTake(wol_lan_lock, pdMS_TO_TICKS(5000)) != pdTRUE) { failure = "scan_busy"; goto done; }
    xSemaphoreGive(wol_lan_lock);
    set_status("downloading", 0, "");
    if (!persist_status()) { failure = "storage_failed"; goto done; }
    publish_status();
    ESP_LOGI(TAG, "Starting HTTPS OTA; heap=%u largest=%u", (unsigned)esp_get_free_heap_size(), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    buffer = malloc(4096);
    if (!buffer) { failure = "memory_low"; goto done; }
    if (esp_ota_begin(next, job->status.image_size, &handle) != ESP_OK) { failure = "flash_failed"; goto done; }
    writing = true;
    int64_t started = esp_timer_get_time(), last_report = started;
    int received = 0, last_percent = -1;
    while (received < job->status.image_size) {
        int end = received + 32768 - 1;
        if (end >= job->status.image_size) end = job->status.image_size - 1;
        for (int attempt = 0; received <= end && attempt < 4; attempt++) {
            if (esp_timer_get_time() - started > OTA_TIMEOUT_US) { failure = "download_timeout"; goto done; }
            char range[64], expected_range[80];
            snprintf(range, sizeof(range), "bytes=%d-%d", received, end);
            snprintf(expected_range, sizeof(expected_range), "bytes %d-%d/%d", received, end, job->status.image_size);
            response.range[0] = 0;
            client = esp_http_client_init(&http);
            if (!client) { failure = "memory_low"; goto done; }
            bool connected = esp_http_client_set_header(client, "Authorization", authorization) == ESP_OK &&
                esp_http_client_set_header(client, "Range", range) == ESP_OK && esp_http_client_open(client, 0) == ESP_OK;
            if (connected) {
                int64_t length = esp_http_client_fetch_headers(client);
                // No redirect is followed; require this exact authenticated range.
                if (length != end - received + 1 || esp_http_client_get_status_code(client) != 206 || strcmp(response.range, expected_range) || esp_http_client_is_chunked_response(client)) {
                    ESP_LOGW(TAG, "Invalid OTA HTTP response: status=%d length=%lld offset=%d", esp_http_client_get_status_code(client), (long long)length, received);
                    failure = "http_response_invalid"; goto done;
                }
                while (received <= end) {
                    int remaining = end - received + 1;
                    int count = esp_http_client_read(client, (char *)buffer, remaining < 4096 ? remaining : 4096);
                    if (count <= 0) {
                        ESP_LOGW(TAG, "OTA read interrupted: result=%d errno=%d offset=%d retry=%d", count, esp_http_client_get_errno(client), received, attempt);
                        break;
                    }
                    if (esp_ota_write(handle, buffer, count) != ESP_OK) { failure = "flash_failed"; goto done; }
                    received += count;
                    int percent = (int)((int64_t)received * 95 / job->status.image_size);
                    set_status("downloading", percent, "");
                    if (percent != last_percent && esp_timer_get_time() - last_report > 1500000) {
                        publish_status(); last_report = esp_timer_get_time(); last_percent = percent;
                    }
                    if (esp_timer_get_time() - started > OTA_TIMEOUT_US) { failure = "download_timeout"; goto done; }
                    vTaskDelay(1);
                }
            }
            esp_http_client_cleanup(client); client = NULL;
            if (received <= end) vTaskDelay(pdMS_TO_TICKS(500));
        }
        if (received <= end) goto done;
    }
    free(buffer); buffer = NULL;
    set_status("verifying", 96, ""); publish_status();
    esp_err_t err = esp_ota_end(handle);
    writing = false;
    if (err != ESP_OK) { failure = "image_invalid"; goto done; }
    esp_app_desc_t description;
    if (esp_ota_get_partition_description(next, &description) != ESP_OK || strcmp(description.project_name, "wol_gateway") || strcmp(description.version, job->status.target_version)) {
        failure = "image_mismatch"; goto done;
    }
    if (!partition_matches(next, job->status.image_size, job->status.sha256)) { failure = "digest_mismatch"; goto done; }
    set_status("rebooting", 98, "");
    if (!persist_status()) { failure = "storage_failed"; goto done; }
    if (esp_ota_set_boot_partition(next) != ESP_OK) { failure = "image_invalid"; goto done; }
    publish_status();
    ESP_LOGI(TAG, "Application ready; rebooting; minimum_heap=%u stack=%u", (unsigned)esp_get_minimum_free_heap_size(), (unsigned)uxTaskGetStackHighWaterMark(NULL));
    memset(job->token, 0, sizeof(job->token));
    free(job);
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
done:
    if (client) esp_http_client_cleanup(client);
    if (writing) esp_ota_abort(handle);
    free(buffer);
    set_status("failed", 0, failure);
    persist_status();
    atomic_store(&active, false);
    publish_status();
    ESP_LOGW(TAG, "OTA failed: %s; heap=%u minimum=%u", failure, (unsigned)esp_get_free_heap_size(), (unsigned)esp_get_minimum_free_heap_size());
    memset(job->token, 0, sizeof(job->token));
    free(job);
    vTaskDelete(NULL);
}

static bool version_allowed(const char *target)
{
    unsigned a, b, c, x, y, z;
    int length = 0;
    if (sscanf(target, "%u.%u.%u%n", &a, &b, &c, &length) != 3 || target[length] || a > 999 || b > 999 || c > 999 ||
        sscanf(esp_app_get_description()->version, "%u.%u.%u", &x, &y, &z) != 3) return false;
    return a > x || (a == x && (b > y || (b == y && c >= z)));
}

esp_err_t wol_ota_start(const cJSON *payload)
{
    if (!wol_mqtt_bound() || !wol_network_ready() || atomic_exchange(&active, true)) return ESP_ERR_INVALID_STATE;
    esp_err_t result = ESP_ERR_INVALID_ARG;
    ota_job_t *job = calloc(1, sizeof(*job));
    cJSON *manifest = NULL;
    mbedtls_pk_context key;
    mbedtls_pk_init(&key);
    if (!job) { result = ESP_ERR_NO_MEM; goto fail; }
    const char *raw = wol_json_string(payload, "manifest"), *signature = wol_json_string(payload, "signature");
    const char *task = wol_json_string(payload, "task_id"), *token = wol_json_string(payload, "token");
    unsigned char signature_bytes[80], hash[32], identifier[16], bearer[32];
    size_t sig_length = strlen(signature) / 2;
    if (!*raw || strlen(raw) > 1024 || sig_length < 8 || sig_length > sizeof(signature_bytes) || !hex_bytes(signature, signature_bytes, sig_length) || !hex_bytes(task, identifier, 16) || !hex_bytes(token, bearer, 32)) goto fail;
    if (mbedtls_sha256((const unsigned char *)raw, strlen(raw), hash, 0) != 0 ||
        mbedtls_pk_parse_public_key(&key, (const unsigned char *)ota_public_start, strlen(ota_public_start) + 1) != 0 ||
        mbedtls_pk_verify(&key, MBEDTLS_MD_SHA256, hash, sizeof(hash), signature_bytes, sig_length) != 0) goto fail;
    manifest = cJSON_Parse(raw);
    const cJSON *size = cJSON_GetObjectItemCaseSensitive(manifest, "size");
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(manifest, "schema");
    const char *version = wol_json_string(manifest, "version"), *sha = wol_json_string(manifest, "sha256");
    if (!cJSON_IsNumber(schema) || schema->valueint != 1 || strcmp(wol_json_string(manifest, "chip"), "esp32c3") || strcmp(wol_json_string(manifest, "layout"), OTA_LAYOUT) || !cJSON_IsNumber(size) || size->valuedouble != size->valueint || size->valueint < 256 || size->valueint > OTA_SLOT_SIZE || !*version || strlen(version) >= sizeof(job->status.target_version) || !hex_bytes(sha, hash, 32)) goto fail;
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL), *running = esp_ota_get_running_partition();
    if (!next || !running || next->address == running->address || next->size < size->valueint) goto fail;
    // Reserve enough memory for the second TLS session and OTA stack. Refuse, never reboot, on pressure.
    if (esp_get_free_heap_size() < 85000 || heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) < 32768) { result = ESP_ERR_NO_MEM; goto fail; }
    if (!version_allowed(version)) goto fail;
    job->status.format = 2;
    job->status.target_address = next->address;
    job->status.image_size = size->valueint;
    strlcpy(job->status.task_id, task, sizeof(job->status.task_id));
    strlcpy(job->status.target_version, version, sizeof(job->status.target_version));
    strlcpy(job->status.sha256, sha, sizeof(job->status.sha256));
    strlcpy(job->status.state, "downloading", sizeof(job->status.state));
    strlcpy(job->token, token, sizeof(job->token));
    portENTER_CRITICAL(&status_lock);
    status = job->status;
    portEXIT_CRITICAL(&status_lock);
    mbedtls_pk_free(&key);
    cJSON_Delete(manifest);
    if (xTaskCreate(upgrade_task, "https_ota", 8192, job, 2, NULL) == pdPASS) return ESP_OK;
    free(job);
    set_status("failed", 0, "memory_low");
    atomic_store(&active, false);
    return ESP_ERR_NO_MEM;
fail:
    mbedtls_pk_free(&key);
    cJSON_Delete(manifest);
    free(job);
    atomic_store(&active, false);
    return result;
}
