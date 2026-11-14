idf_component_register(
    SRCS "main.c" "config.c" "network.c" "captive_portal.c" "lan.c" "gateway_mqtt.c" "enrollment.c" "ota.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event esp_http_server esp_http_client nvs_flash
             mbedtls lwip esp_timer esp_system esp_app_format esp_https_ota app_update
    EMBED_TXTFILES "portal.html" "enroll_ca.pem" "ota_public_key.pem"
)
