"""Validated, deterministic table ordering before pagination."""

import ipaddress
import re
import unicodedata


def sort_options(key, direction, allowed, default="id"):
    return (key if key in allowed else default, "asc" if direction == "asc" else "desc")


def value_key(value, key):
    if key == "id":
        return int(value or 0)
    if key == "ip":
        try:
            return int(ipaddress.ip_address(value or ""))
        except ValueError:
            return -1
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if key == "mac":
        return re.sub(r"[^0-9a-f]", "", text)
    # Numeric pieces compare numerically: PC2 precedes PC10.
    return tuple(
        (1, int(piece)) if piece.isdigit() else (0, piece)
        for piece in re.split(r"(\d+)", text)
    )


def sorted_rows(rows, key="id", direction="desc"):
    return sorted(
        rows,
        key=lambda row: (value_key(row.get(key), key), int(row.get("id", 0))),
        reverse=direction != "asc",
    )
