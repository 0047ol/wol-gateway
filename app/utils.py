"""通用工具：MAC 格式化、签名、密钥生成、分页参数。"""

import hashlib
import secrets
from typing import Tuple


def norm_mac(mac: str) -> str:
    """MAC 统一为小写无分隔符：AA:BB:CC:11:22:33 -> aabbcc112233"""
    if not mac:
        return ""
    return mac.strip().lower().replace(":", "").replace("-", "").replace(".", "")


def fmt_mac(mac: str) -> str:
    """显示用 MAC：aabbcc112233 -> AA:BB:CC:11:22:33"""
    m = norm_mac(mac)
    if len(m) != 12:
        return mac
    return ":".join(m[i : i + 2].upper() for i in range(0, 12, 2))


def gen_bind_key() -> str:
    return secrets.token_hex(16)


def device_search(q: str, name: str = "name", mac: str = "mac", ip: str = "ip"):
    """Column names are internal constants; user text is always a bound value."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    normalized_mac = (
        f"replace(replace(replace(lower({mac}), ':', ''), '-', ''), '.', '')"
    )
    return (
        f"({name} LIKE ? ESCAPE '\\' OR {normalized_mac}=? OR {ip}=?)",
        [f"%{escaped}%", norm_mac(q), q],
    )


def sign(bind_key: str, timestamp: str, payload: str) -> str:
    """签名 = sha256(bind_key + timestamp + payload)"""
    raw = f"{bind_key}{timestamp}{payload}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def paginate(page: int, page_size: int, total: int) -> Tuple[int, int, int, int]:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    return page, page_size, offset, total_pages
