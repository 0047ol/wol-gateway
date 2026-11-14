"""Explicit protocol version floor; broker ACLs provide identity isolation."""

import re
from .config import settings


def supported(value, minimum):
    def version(text):
        match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", str(text))
        return tuple(map(int, match.groups())) if match else None

    current, floor = version(value), version(minimum)
    return current is not None and floor is not None and current >= floor


def gateway_allowed(value):
    return not settings.ENFORCE_CLIENT_VERSIONS or supported(
        value, settings.MIN_FIRMWARE_VERSION
    )


def pc_allowed(value):
    return not settings.ENFORCE_CLIENT_VERSIONS or supported(
        value, settings.MIN_PC_VERSION
    )
