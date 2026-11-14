#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

enum { WOL_WIFI_SCAN_LIMIT = 32, WOL_WIFI_SCAN_TTL_MS = 60000 };

typedef struct {
    char ssid[33];
    int authmode;
    int8_t rssi;
    uint32_t seen_ms;
    uint32_t generation;
} wol_wifi_scan_item_t;

typedef struct {
    wol_wifi_scan_item_t items[WOL_WIFI_SCAN_LIMIT];
    uint16_t count;
    uint32_t generation;
} wol_wifi_scan_cache_t;

static inline void wol_wifi_scan_prune(wol_wifi_scan_cache_t *cache, uint32_t now)
{
    uint16_t kept = 0;
    for (uint16_t i = 0; i < cache->count; i++) {
        if ((uint32_t)(now - cache->items[i].seen_ms) >= WOL_WIFI_SCAN_TTL_MS) continue;
        cache->items[kept++] = cache->items[i];
    }
    cache->count = kept;
}

static inline void wol_wifi_scan_begin(wol_wifi_scan_cache_t *cache, uint32_t now)
{
    wol_wifi_scan_prune(cache, now);
    if (++cache->generation == 0) {
        cache->generation = 1;
        for (uint16_t i = 0; i < cache->count; i++) cache->items[i].generation = 0;
    }
}

static inline bool wol_wifi_scan_seen(const wol_wifi_scan_cache_t *cache, const char *ssid)
{
    for (uint16_t i = 0; i < cache->count; i++)
        if (cache->items[i].generation == cache->generation &&
            strcmp(cache->items[i].ssid, ssid) == 0) return true;
    return false;
}

static inline void wol_wifi_scan_merge(wol_wifi_scan_cache_t *cache, const char *ssid,
        int authmode, int8_t rssi, uint32_t now)
{
    size_t len = 0;
    while (len < 33 && ssid[len]) len++;
    if (len == 0 || len > 32) return;
    wol_wifi_scan_prune(cache, now);
    uint16_t slot = cache->count;
    for (uint16_t i = 0; i < cache->count; i++) {
        if (cache->items[i].authmode == authmode && strcmp(cache->items[i].ssid, ssid) == 0) {
            slot = i;
            // Multiple APs with the same name/security use the strongest signal
            // seen in this scan, while a later scan may report a weaker signal.
            if (cache->items[i].generation == cache->generation && cache->items[i].rssi > rssi)
                rssi = cache->items[i].rssi;
            break;
        }
    }
    if (slot == WOL_WIFI_SCAN_LIMIT) {
        slot = 0;
        for (uint16_t i = 1; i < cache->count; i++) {
            uint32_t age = now - cache->items[i].seen_ms;
            uint32_t oldest = now - cache->items[slot].seen_ms;
            if (age > oldest || (age == oldest && cache->items[i].rssi < cache->items[slot].rssi)) slot = i;
        }
    } else if (slot == cache->count) {
        cache->count++;
    }
    wol_wifi_scan_item_t *item = &cache->items[slot];
    memcpy(item->ssid, ssid, len);
    item->ssid[len] = '\0';
    item->authmode = authmode;
    item->rssi = rssi;
    item->seen_ms = now;
    item->generation = cache->generation;
}
