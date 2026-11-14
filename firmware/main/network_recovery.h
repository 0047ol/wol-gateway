#pragma once
#include <stdbool.h>
#include <stdint.h>

/* A failed background reconnect can be paused while a setup client is present.
 * Its previous successful connection cleared the attempt timestamp, so state
 * reporting must not depend on that timestamp to expose this failure. */
static inline bool wol_wifi_retry_paused(bool network_ready, bool setup_active,
        bool station_connecting, uint8_t disconnect_count, uint8_t clients)
{
    return !network_ready && !setup_active && !station_connecting &&
        disconnect_count > 0 && clients > 0;
}

/* Refresh an established Wi-Fi link only after persistent transport timeouts.
 * Certificate/authentication failures never increment this timeout counter. */
static inline bool wol_transport_recovery_due(uint32_t timeouts, int64_t first_ms,
        int64_t last_ms, int64_t now_ms, bool network_ready, bool busy)
{
    return network_ready && !busy && timeouts >= 3 &&
        now_ms - first_ms >= 45000 && now_ms - last_ms >= 120000;
}
