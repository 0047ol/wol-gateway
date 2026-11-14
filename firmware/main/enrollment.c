#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "wol.h"
#include "esp_http_client.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "esp_log.h"

extern const char enroll_ca_start[] asm("_binary_enroll_ca_pem_start");
static char response[2048];
static size_t response_length;
static int64_t last_attempt;

static esp_err_t collect(esp_http_client_event_t *event)
{
    if (event->event_id == HTTP_EVENT_ON_DATA) {
        if (response_length + event->data_len >= sizeof(response)) return ESP_FAIL;
        memcpy(response + response_length, event->data, event->data_len);
        response_length += event->data_len;
        response[response_length] = 0;
    }
    return ESP_OK;
}

bool wol_enroll(void)
{
    int64_t now = esp_timer_get_time() / 1000000;
    if (time(NULL) < 1704067200 || (last_attempt && now-last_attempt < 15)) return false;
    last_attempt = now;
    wol_config_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return false;
    wol_config_get(cfg);
    // A terminal server rejection must remain visible to the portal and must
    // not generate a new HTTP request every 15 seconds. The binding endpoint
    // calls wol_binding_retry() after the user submits a new code/key.
    if (wol_binding_blocked()) { free(cfg); return false; }
    if (!*cfg->pairing_code && !*cfg->bind_key) { free(cfg); return false; }
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "mac", wol_mac);
    cJSON_AddStringToObject(body, "version", esp_app_get_description()->version);
    if (*cfg->bind_key) cJSON_AddStringToObject(body, "bind_key", cfg->bind_key);
    else cJSON_AddStringToObject(body, "code", cfg->pairing_code);
    char *raw = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    response_length=0; response[0]=0;
    esp_http_client_config_t options = {.url="https://YOUR_SERVER_HOST:8443/api/enroll/esp",
        .cert_pem=enroll_ca_start, .timeout_ms=12000, .event_handler=collect,
        .method=HTTP_METHOD_POST, .buffer_size=2048};
    esp_http_client_handle_t client=esp_http_client_init(&options);
    bool ok=false;
    if (client && raw) {
        esp_http_client_set_header(client,"Content-Type","application/json");
        esp_http_client_set_post_field(client,raw,strlen(raw));
        esp_err_t transport = esp_http_client_perform(client);
        int status = esp_http_client_get_status_code(client);
        if (transport==ESP_OK && status==200) {
            cJSON *result=cJSON_Parse(response);
            const char *username=wol_json_string(result,"username"), *password=wol_json_string(result,"password");
            const char *key=wol_json_string(result,"bind_key");
            if (!strcmp(username,wol_mac) && strlen(password)>=32 && strlen(password)<sizeof(cfg->mqtt_password) && strlen(key)==32) {
                esp_err_t saved = wol_config_set_enrollment(cfg->pairing_code, cfg->bind_key, username, password, key);
                ok = saved == ESP_OK;
                if (saved == ESP_ERR_INVALID_STATE) {
                    ESP_LOGI("enrollment", "Discarded enrollment for superseded pairing");
                    cJSON_Delete(result);
                    goto cleanup;
                }
                if (ok) ESP_LOGI("enrollment", "MQTT credentials provisioned automatically");
            }
            cJSON_Delete(result);
            if (!ok) wol_binding_error("enrollment_unavailable");
        } else {
            wol_settings_t latest;
            wol_settings_get(&latest);
            if (strcmp(latest.pairing_code, cfg->pairing_code) != 0 || strcmp(latest.bind_key, cfg->bind_key) != 0)
                goto cleanup;
            const char *detail = "";
            cJSON *failure = response_length ? cJSON_Parse(response) : NULL;
            if (failure) detail = wol_json_string(failure, "detail");
            if (transport != ESP_OK)
                wol_binding_error("enrollment_network_error");
            else if (status >= 500 || status == 429)
                wol_binding_error("enrollment_unavailable");
            else
                wol_binding_error(detail);
            int tls_code = 0, tls_flags = 0;
            esp_err_t tls_error = esp_http_client_get_and_clear_last_tls_error(client, &tls_code, &tls_flags);
            ESP_LOGW("enrollment", "Automatic enrollment failed: http=%d transport=%s errno=%d tls_error=0x%x tls=0x%x flags=0x%x detail=%s",
                     status, esp_err_to_name(transport), esp_http_client_get_errno(client),
                     tls_error, tls_code, tls_flags, *detail ? detail : "unknown");
            cJSON_Delete(failure);
        }
    }
cleanup:
    if (client) esp_http_client_cleanup(client);
    free(raw); free(cfg);
    memset(response,0,sizeof(response));
    return ok;
}
