#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include "wol.h"
#include "esp_netif_net_stack.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "lwip/etharp.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/tcpip.h"

typedef struct {
    struct netif *netif;
    int count;
    bool read;
    uint32_t ip[8];
    bool found[8];
    uint8_t mac[8][6];
} arp_batch_t;

typedef struct {
    SemaphoreHandle_t done;
    atomic_uint refs;
    arp_batch_t batch;
} arp_callback_ctx_t;

static const char *TAG = "lan";
#define ARP_CALLBACK_TIMEOUT_MS 2000
#define ARP_SCAN_MAX_DURATION_MS 90000
#define MANUAL_SCAN_LOCK_TIMEOUT_MS 8000

static void arp_context_release(arp_callback_ctx_t *ctx)
{
    if (atomic_fetch_sub(&ctx->refs, 1) == 1) {
        vSemaphoreDelete(ctx->done);
        free(ctx);
    }
}

// lwIP's ARP table must only be accessed from its TCP/IP task.
static void arp_callback(void *arg)
{
    arp_callback_ctx_t *ctx = arg;
    arp_batch_t *batch = &ctx->batch;
    for (int i = 0; i < batch->count; i++) {
        ip4_addr_t ip = {.addr = htonl(batch->ip[i])};
        if (!batch->read) {
            // Reserve a pending entry before sending. Bare ARP requests leave
            // simultaneous replies competing for the same small cache slot.
            if (!batch->found[i]) etharp_query(batch->netif, &ip, NULL);
        } else {
            if (batch->found[i]) continue;
            struct eth_addr *eth;
            const ip4_addr_t *found_ip;
            batch->found[i] = etharp_find_addr(batch->netif, &ip, &eth, &found_ip) >= 0;
            if (batch->found[i]) memcpy(batch->mac[i], eth->addr, 6);
        }
    }
    xSemaphoreGive(ctx->done);
    arp_context_release(ctx);
}

static esp_err_t arp_execute(arp_batch_t *batch)
{
    arp_callback_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) return ESP_ERR_NO_MEM;
    ctx->done = xSemaphoreCreateBinary();
    if (!ctx->done) { free(ctx); return ESP_ERR_NO_MEM; }
    ctx->batch = *batch;
    atomic_init(&ctx->refs, 2); // caller and queued TCP/IP callback
    if (tcpip_callback(arp_callback, ctx) != ERR_OK) {
        arp_context_release(ctx);
        arp_context_release(ctx);
        return ESP_FAIL;
    }
    if (xSemaphoreTake(ctx->done, pdMS_TO_TICKS(ARP_CALLBACK_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "ARP callback timed out; releasing LAN scan lock");
        arp_context_release(ctx);
        return ESP_ERR_TIMEOUT;
    }
    *batch = ctx->batch;
    arp_context_release(ctx);
    return ESP_OK;
}

static bool publish_scan_host(const char *cmd_id, const char *ip, const char *mac)
{
    cJSON *event = cJSON_CreateObject();
    cJSON *hosts = cJSON_AddArrayToObject(event, "hosts");
    cJSON *host = cJSON_CreateObject();
    if (!event || !hosts || !host) {
        cJSON_Delete(event);
        cJSON_Delete(host);
        return false;
    }
    cJSON_AddStringToObject(event, "cmd_id", cmd_id);
    cJSON_AddBoolToObject(event, "partial", true);
    cJSON_AddBoolToObject(event, "done", false);
    cJSON_AddStringToObject(host, "ip", ip);
    cJSON_AddStringToObject(host, "mac", mac);
    cJSON_AddItemToArray(hosts, host);
    bool ok = wol_publish("scan/result", event, 1);
    cJSON_Delete(event);
    return ok;
}

static atomic_bool manual_scan_pending;
static atomic_bool manual_scan_cancelled;

void wol_scan_cancel(void)
{
    atomic_store(&manual_scan_cancelled, true);
}

void wol_scan_cancel_reset(void)
{
    atomic_store(&manual_scan_cancelled, false);
}

static esp_err_t scan_locked(cJSON **result, bool background, const char *cmd_id)
{
    if (wol_ota_active()) return ESP_ERR_INVALID_STATE;
    *result = NULL;
    if (!wol_network_ready()) return ESP_ERR_INVALID_STATE;
    wol_settings_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    wol_settings_get(cfg);
    esp_netif_ip_info_t info;
    esp_err_t err = esp_netif_get_ip_info(wol_sta, &info);
    if (err != ESP_OK) { free(cfg); return err; }
    uint32_t ip = ntohl(info.ip.addr), local_mask = ntohl(info.netmask.addr);
    // On larger LANs, the automatic scan covers the /24 containing this gateway.
    uint32_t mask = local_mask | 0xffffff00u, network = ip & mask;
    if (*cfg->scan_cidr && !wol_cidr(cfg->scan_cidr, &network, &mask)) { free(cfg); return ESP_ERR_INVALID_ARG; }
    uint32_t host_count = ~mask;
    if ((network & local_mask) != (ip & local_mask) || (mask & local_mask) != local_mask || host_count < 3 || host_count > 255) {
        free(cfg);
        return ESP_ERR_INVALID_ARG;
    }
    int64_t scan_started = esp_timer_get_time();
    ESP_LOGI(TAG, "LAN scan started: hosts=%u concurrency=%d background=%d",
             (unsigned)(host_count - 1), cfg->scan_concurrency, background);
    cJSON *obj = cJSON_CreateObject(), *hosts = cJSON_CreateArray();
    if (!obj || !hosts) { cJSON_Delete(obj); cJSON_Delete(hosts); free(cfg); return ESP_ERR_NO_MEM; }
    cJSON_AddItemToObject(obj, "hosts", hosts);
    arp_batch_t batch = {.netif = esp_netif_get_netif_impl(wol_sta)};
    if (xSemaphoreTake(wol_lan_lock, background ? 0 : pdMS_TO_TICKS(MANUAL_SCAN_LOCK_TIMEOUT_MS)) != pdTRUE) {
        cJSON_Delete(obj);
        free(cfg);
        ESP_LOGW(TAG, "LAN scan could not acquire the shared LAN lock");
        return ESP_ERR_TIMEOUT;
    }
    err = ESP_OK;
    bool discovered[256] = {false};
    // Revisit only unresolved addresses after a full sweep, so transient loss
    // and sleeping Wi-Fi radios get a second opportunity without duplicates.
    for (int pass = 0; pass < 2 && err == ESP_OK; pass++) {
        for (uint32_t base = network + 1; base < network + host_count; base += cfg->scan_concurrency) {
            if (!background && atomic_load(&manual_scan_cancelled)) { err = ESP_ERR_INVALID_STATE; break; }
            if (esp_timer_get_time() - scan_started > ARP_SCAN_MAX_DURATION_MS * 1000LL) {
                err = ESP_ERR_TIMEOUT;
                break;
            }
            // Check cancellation and foreground takeover between short waits.
            if (wol_ota_active() || (background && atomic_load(&manual_scan_pending))) { err = ESP_ERR_NOT_FINISHED; break; }
            if (!wol_network_ready()) { err = ESP_ERR_INVALID_STATE; break; }
            batch.count = 0;
            memset(batch.found, 0, sizeof(batch.found));
            for (int i = 0; i < cfg->scan_concurrency && base + i < network + host_count; i++) {
                if (base + i != ip && !discovered[base + i - network]) batch.ip[batch.count++] = base + i;
            }
            if (!batch.count) continue;
            const int waits[] = {300, 700};
            for (int attempt = 0; attempt < 2 && err == ESP_OK; attempt++) {
                batch.read = false;
                if ((err = arp_execute(&batch)) != ESP_OK) break;
                bool missing = true;
                for (int elapsed = 0; elapsed < waits[attempt] && missing; elapsed += 50) {
                    if (!wol_network_ready() || (!background && atomic_load(&manual_scan_cancelled))) { err = ESP_ERR_INVALID_STATE; break; }
                    if (background && atomic_load(&manual_scan_pending)) { err = ESP_ERR_NOT_FINISHED; break; }
                    if (esp_timer_get_time() - scan_started > ARP_SCAN_MAX_DURATION_MS * 1000LL) { err = ESP_ERR_TIMEOUT; break; }
                    vTaskDelay(pdMS_TO_TICKS(50));
                    // Copy responses out every 50 ms rather than waiting for an
                    // entire batch: later traffic can evict early ARP replies.
                    batch.read = true;
                    if ((err = arp_execute(&batch)) != ESP_OK) break;
                    missing = false;
                    for (int i = 0; i < batch.count; i++) if (!batch.found[i]) missing = true;
                }
                if (!missing) break;
            }
            if (err != ESP_OK) break;
            for (int i = 0; i < batch.count; i++) {
                if (!batch.found[i]) continue;
                discovered[batch.ip[i] - network] = true;
                char text[16], mac[13];
                struct in_addr addr = {.s_addr = htonl(batch.ip[i])};
                inet_ntop(AF_INET, &addr, text, sizeof(text));
                wol_mac_format(batch.mac[i], mac);
                bool duplicate = false;
                const cJSON *existing;
                cJSON_ArrayForEach(existing, hosts) {
                    if (strcmp(wol_json_string(existing, "mac"), mac) == 0) { duplicate = true; break; }
                }
                if (duplicate) continue;
                cJSON *host = cJSON_CreateObject();
                cJSON_AddStringToObject(host, "ip", text);
                cJSON_AddStringToObject(host, "mac", mac);
                cJSON_AddItemToArray(hosts, host);
                if (cmd_id && !publish_scan_host(cmd_id, text, mac)) { err = ESP_FAIL; break; }
            }
            if (err != ESP_OK) break;
        }
    }
    xSemaphoreGive(wol_lan_lock);
    free(cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "LAN scan stopped: err=%s elapsed_ms=%lld", esp_err_to_name(err),
                 (long long)((esp_timer_get_time() - scan_started) / 1000));
        cJSON_Delete(obj);
    } else {
        ESP_LOGI(TAG, "LAN scan complete: hosts_found=%d elapsed_ms=%lld",
                 cJSON_GetArraySize(hosts), (long long)((esp_timer_get_time() - scan_started) / 1000));
        *result = obj;
    }
    return err;
}

esp_err_t wol_scan(cJSON **result, bool background, const char *cmd_id)
{
    *result = NULL;
    if (background) {
        if (atomic_load(&manual_scan_pending)) return ESP_ERR_NOT_FINISHED;
        return scan_locked(result, true, NULL);
    }
    if (atomic_exchange(&manual_scan_pending, true)) return ESP_ERR_TIMEOUT;
    esp_err_t err = scan_locked(result, false, cmd_id);
    atomic_store(&manual_scan_pending, false);
    return err;
}

esp_err_t wol_wake(const cJSON *payload)
{
    const cJSON *macs = cJSON_GetObjectItemCaseSensitive(payload, "macs");
    int count = cJSON_GetArraySize(macs);
    if (!cJSON_IsArray(macs) || count < 1 || count > WOL_MAX_DEVICES) return ESP_ERR_INVALID_ARG;
    const cJSON *item;
    uint8_t mac[6];
    // Validate the whole batch before sending anything.
    cJSON_ArrayForEach(item, macs) if (!cJSON_IsString(item) || !wol_mac_parse(item->valuestring, mac)) return ESP_ERR_INVALID_ARG;
    wol_settings_t *cfg = malloc(sizeof(*cfg));
    if (!cfg) return ESP_ERR_NO_MEM;
    wol_settings_get(cfg);
    struct sockaddr_in dest = {.sin_family = AF_INET, .sin_port = htons(9)};
    inet_pton(AF_INET, cfg->broadcast, &dest.sin_addr);
    free(cfg);
    if (!wol_network_ready()) return ESP_ERR_INVALID_STATE;
    int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) return ESP_FAIL;
    int on = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &on, sizeof(on)) < 0) { close(fd); return ESP_FAIL; }
    // Bind to STA so an active recovery AP cannot capture the broadcast route.
    esp_netif_ip_info_t info;
    esp_netif_get_ip_info(wol_sta, &info);
    struct sockaddr_in local = {.sin_family = AF_INET, .sin_addr.s_addr = info.ip.addr};
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) { close(fd); return ESP_FAIL; }
    esp_err_t err = ESP_OK;
    cJSON_ArrayForEach(item, macs) {
        wol_mac_parse(item->valuestring, mac);
        uint8_t packet[102];
        memset(packet, 0xff, 6);
        for (int i = 0; i < 16; i++) memcpy(packet + 6 + i * 6, mac, 6);
        for (int repeat = 0; repeat < 3; repeat++) {
            if (sendto(fd, packet, sizeof(packet), 0, (struct sockaddr *)&dest, sizeof(dest)) != sizeof(packet)) err = ESP_FAIL;
            vTaskDelay(pdMS_TO_TICKS(30));
        }
    }
    close(fd);
    return err;
}

static uint16_t checksum(const uint8_t *p, size_t length)
{
    uint32_t sum = 0;
    while (length > 1) { sum += (p[0] << 8) | p[1]; p += 2; length -= 2; }
    if (length) sum += p[0] << 8;
    while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
    return (uint16_t)~sum;
}

static bool ping_host(const char *ip)
{
    if (!wol_ipv4(ip) || !wol_network_ready()) return false;
    int fd = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
    if (fd < 0) return false;
    if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0) {
        ESP_LOGW(TAG, "Unable to make ICMP socket nonblocking");
        close(fd);
        return false;
    }
    esp_netif_ip_info_t info;
    esp_netif_get_ip_info(wol_sta, &info);
    struct sockaddr_in local = {.sin_family = AF_INET, .sin_addr.s_addr = info.ip.addr};
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) { close(fd); return false; }
    struct sockaddr_in dest = {.sin_family = AF_INET};
    inet_pton(AF_INET, ip, &dest.sin_addr);
    uint8_t packet[16] = {8, 0, 0, 0};
    esp_fill_random(packet + 4, 12);
    uint16_t check = checksum(packet, sizeof(packet));
    packet[2] = check >> 8;
    packet[3] = check & 0xff;
    bool online = false;
    if (sendto(fd, packet, sizeof(packet), 0, (struct sockaddr *)&dest, sizeof(dest)) == sizeof(packet)) {
        int64_t deadline = esp_timer_get_time() + 700000;
        while (esp_timer_get_time() < deadline) {
            int64_t remaining = deadline - esp_timer_get_time();
            if (remaining <= 0) break;
            uint8_t reply[128];
            struct sockaddr_in source;
            socklen_t size = sizeof(source);
            int length = recvfrom(fd, reply, sizeof(reply), 0, (struct sockaddr *)&source, &size);
            if (length < 0) {
                if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            if (length < 20) continue;
            int header = (reply[0] & 0x0f) * 4;
            if (header >= 20 && length == header + sizeof(packet) && source.sin_addr.s_addr == dest.sin_addr.s_addr &&
                reply[header] == 0 && reply[header + 1] == 0 &&
                memcmp(reply + header + 4, packet + 4, sizeof(packet) - 4) == 0 && checksum(reply + header, sizeof(packet)) == 0) {
                online = true;
                break;
            }
        }
    }
    close(fd);
    return online;
}

static TaskHandle_t monitor_handle;

void wol_monitor_request(void)
{
    if (monitor_handle) xTaskNotifyGive(monitor_handle);
}

void wol_monitor_task(void *arg)
{
    monitor_handle = xTaskGetCurrentTaskHandle();
    wol_config_t *cfg = malloc(sizeof(*cfg));
    configASSERT(cfg);
    for (;;) {
        wol_config_get(cfg);
        if (!wol_ota_active() && wol_mqtt_bound() && wol_network_ready() && cfg->device_count) {
            bool need_scan = false;
            for (int i = 0; i < cfg->device_count; i++) if (!*cfg->devices[i].ip) need_scan = true;
            cJSON *discovery = NULL;
            if (need_scan && wol_scan(&discovery, true, NULL) != ESP_OK) {
                // An interrupted discovery is not evidence that devices went offline.
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
            cJSON *report = cJSON_CreateObject(), *devices = cJSON_AddArrayToObject(report, "devices");
            bool report_complete = true;
            for (int i = 0; i < cfg->device_count; i++) {
                if (wol_ota_active()) { report_complete = false; break; }
                wol_device_t *device = &cfg->devices[i];
                const char *ip = device->ip;
                const cJSON *host;
                if (!*ip) {
                    cJSON_ArrayForEach(host, cJSON_GetObjectItemCaseSensitive(discovery, "hosts")) {
                        if (strcmp(wol_json_string(host, "mac"), device->mac) == 0) { ip = wol_json_string(host, "ip"); break; }
                    }
                }
                while (atomic_load(&manual_scan_pending)) vTaskDelay(pdMS_TO_TICKS(20));
                if (xSemaphoreTake(wol_lan_lock, pdMS_TO_TICKS(2000)) != pdTRUE) {
                    report_complete = false;
                    break;
                }
                bool online = ping_host(ip);
                xSemaphoreGive(wol_lan_lock);
                cJSON *d = cJSON_CreateObject();
                cJSON_AddStringToObject(d, "mac", device->mac);
                cJSON_AddStringToObject(d, "status", online ? "online" : "offline");
                cJSON_AddStringToObject(d, "ip", ip);
                cJSON_AddItemToArray(devices, d);
                vTaskDelay(pdMS_TO_TICKS(20));
            }
            if (report_complete) wol_publish("subdevice/report", report, 0);
            cJSON_Delete(report);
            cJSON_Delete(discovery);
        }
        // Long probe cycles do not delay heartbeats or command processing.
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(cfg->ping_interval * 1000));
    }
}
