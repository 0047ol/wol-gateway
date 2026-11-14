#include <errno.h>
#include <string.h>
#include <unistd.h>
#include "captive_portal.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

// Reply to one uncompressed DNS question, bounded by the received datagram.
// A queries point to the AP; AAAA/HTTPS queries receive a valid empty answer.
static size_t dns_reply(uint8_t *packet, size_t length, size_t capacity, uint32_t address)
{
    if (length < 12 || (packet[2] & 0xf8) || packet[4] != 0 || packet[5] != 1) return 0;
    size_t end = 12;
    size_t name_length = 0;
    for (;;) {
        if (end >= length) return 0;
        uint8_t label = packet[end++];
        if (!label) break;
        if (label > 63 || end + label > length || (name_length += label + 1) > 253) return 0;
        end += label;
    }
    if (end + 4 > length) return 0;
    uint16_t type = (packet[end] << 8) | packet[end + 1];
    uint16_t klass = (packet[end + 2] << 8) | packet[end + 3];
    end += 4;
    if (klass != 1) return 0;
    bool answer_a = type == 1;
    if (answer_a && end + 16 > capacity) return 0;
    packet[2] = 0x84 | (packet[2] & 1); // response, authoritative, preserve RD
    packet[3] = 0;
    memset(packet + 6, 0, 6); // remove query additional records, including EDNS
    if (answer_a) {
        packet[7] = 1;
        const uint8_t answer[] = {0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 0, 0, 4};
        memcpy(packet + end, answer, sizeof(answer));
        memcpy(packet + end + sizeof(answer), &address, 4);
        end += 16;
    }
    return end;
}

static void dns_task(void *arg)
{
    esp_netif_t *ap = arg;
    esp_netif_ip_info_t info;
    ESP_ERROR_CHECK(esp_netif_get_ip_info(ap, &info));
    int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) {
        ESP_LOGE("portal_dns", "Could not create DNS socket: %d", errno);
        vTaskDelete(NULL);
        return;
    }
    // Bind only to the setup interface, leaving STA-side DNS untouched.
    struct sockaddr_in local = {.sin_family = AF_INET, .sin_port = htons(53), .sin_addr.s_addr = info.ip.addr};
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        ESP_LOGE("portal_dns", "Could not bind DNS socket: %d", errno);
        close(fd);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI("portal_dns", "Captive DNS ready on " IPSTR ":53", IP2STR(&info.ip));
    for (;;) {
        uint8_t packet[512];
        struct sockaddr_in peer;
        socklen_t peer_length = sizeof(peer);
        int length = recvfrom(fd, packet, sizeof(packet), 0, (struct sockaddr *)&peer, &peer_length);
        if (length < 0) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if ((peer.sin_addr.s_addr & info.netmask.addr) != (info.ip.addr & info.netmask.addr)) continue;
        size_t reply_length = dns_reply(packet, (size_t)length, sizeof(packet), info.ip.addr);
        if (reply_length) sendto(fd, packet, reply_length, 0, (struct sockaddr *)&peer, peer_length);
    }
}

void wol_captive_dns_start(esp_netif_t *ap_netif)
{
    // Advertise the AP DNS server in DHCP offers before clients renew leases.
    esp_netif_dhcp_status_t status;
    ESP_ERROR_CHECK(esp_netif_dhcps_get_status(ap_netif, &status));
    if (status == ESP_NETIF_DHCP_STARTED) ESP_ERROR_CHECK(esp_netif_dhcps_stop(ap_netif));
    esp_netif_ip_info_t info;
    ESP_ERROR_CHECK(esp_netif_get_ip_info(ap_netif, &info));
    esp_netif_dns_info_t dns = {.ip.type = ESP_IPADDR_TYPE_V4, .ip.u_addr.ip4 = info.ip};
    ESP_ERROR_CHECK(esp_netif_set_dns_info(ap_netif, ESP_NETIF_DNS_MAIN, &dns));
    uint8_t offer_dns = 1;
    ESP_ERROR_CHECK(esp_netif_dhcps_option(ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER,
                                        &offer_dns, sizeof(offer_dns)));
    ESP_ERROR_CHECK(esp_netif_dhcps_start(ap_netif));
    configASSERT(xTaskCreate(dns_task, "portal_dns", 4096, ap_netif, 3, NULL) == pdPASS);
}
