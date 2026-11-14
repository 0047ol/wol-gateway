"""Shared device types, display icons and UI power capabilities."""

import re
import unicodedata

from .device_logos import LOGO_PATHS


def word(aliases):
    """ASCII word edges still allow model numbers, Chinese and host separators."""
    return rf"(?<![a-z0-9])(?:{aliases})(?![a-z])"


# Specific names precede generic computer/phone words (e.g. Android tablet).
DEVICE_ICON_RULES = (
    (
        "router",
        "路由器",
        r"路由|交换机|" + word(r"router|openwrt|mikrotik|tp[-_ ]?link"),
        "M4 13h16v7H4zM7 13V5m10 8V5M8 17h.01M12 17h.01M16 17h.01M4 3l3 2 3-2m4 0 3 2 3-2",
    ),
    (
        "printer",
        "打印机",
        r"打印|" + word(r"printer|laserjet|deskjet|officejet|imageclass"),
        "M6 9V3h12v6M6 17H3V9h18v8h-3M6 14h12v7H6zM17 12h.01",
    ),
    (
        "watch",
        "手表",
        r"手表|手环|" + word(r"watch|smartwatch|smartband|mi[-_ ]?band"),
        "M8 7l1-5h6l1 5M8 17l1 5h6l1-5M7 7h10v10H7zM12 9v3l2 1",
    ),
    (
        "tablet",
        "平板",
        r"平板|"
        + word(r"ipad|tablet|surface(?:[-_ ]?pro)?|tab|pad|matepad|xiaoxin[-_ ]?pad"),
        "M4 3h16v18H4zM11 18h2",
    ),
    (
        "laptop",
        "笔记本",
        r"笔记本|笔电|"
        + word(
            r"laptop|notebook|macbook(?:[-_ ]?(?:air|pro))?|thinkpad|thinkbook|matebook|magicbook|redmibook|mibook|ideapad|zenbook|vivobook|elitebook|probook|latitude|yogabook"
        ),
        "M5 4h14v12H5zM5 16l-3 4h20l-3-4M10 18h4",
    ),
    (
        "phone",
        "手机",
        r"手机|" + word(r"iphone|smartphone|xperia"),
        "M7 2h10v20H7zM10 5h4M11 19h2",
    ),
    (
        "desktop",
        "电脑",
        r"电脑|台式|工作站|"
        + word(
            r"desktop|workstation|imac|mac(?:[-_ ]?(?:mini|studio|pro))?|mini[-_ ]?pc|pc|windows|win(?:[-_ ]?(?:xp|vista|7|8|10|11))?"
        ),
        "M2 4h15v12H2zM6 20h7M9.5 16v4M20 4h3v16h-3zM21.5 8h.01",
    ),
)
ICONS = {kind: (label, path) for kind, label, _pattern, path in DEVICE_ICON_RULES}
ICONS["lan"] = (
    "局域网设备",
    "M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8 16a6 6 0 0 1 8 0M12 20h.01",
)
ICONS["windows"] = ("WOL关机服务", "M2 3h9v8H2zM13 3h9v8h-9zM2 13h9v8H2zM13 13h9v8h-9z")
NAME_PATTERNS = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, _label, pattern, _path in DEVICE_ICON_RULES
)
EXPLICIT_PATTERNS = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        ("router", r"路由|交换机|" + word(r"router|switch")),
        ("printer", r"打印|" + word(r"printer")),
        ("watch", r"手表|手环|" + word(r"watch|smartwatch|smartband")),
        ("tablet", r"平板|" + word(r"tablet|tab")),
        ("laptop", r"笔记本|笔电|" + word(r"laptop|notebook")),
        ("phone", r"手机|" + word(r"phone|smartphone")),
        ("desktop", r"台式|工作站|" + word(r"desktop|workstation|pc")),
    )
)

# A brand is only a fallback. Explicit device types always win.
PHONE_HINT = re.compile(
    r"小米|红米|华为|荣耀|三星|一加|魅族|真我|摩托罗拉|诺基亚|"
    + word(
        r"android|xiaomi|redmi|poco|huawei|honor|oppo|vivo|iqoo|oneplus|samsung|galaxy|pixel|realme|meizu|motorola|nokia"
    ),
    re.IGNORECASE,
)
PC_HINT = re.compile(
    r"联想|戴尔|惠普|华硕|宏碁|微软|" + word(r"lenovo|dell|hp|asus|acer|microsoft"),
    re.IGNORECASE,
)
OTHER_TYPE = re.compile(
    r"电视|插座|摄像|音箱|音响|耳机|冰箱|空调|手柄|净化|扫地|灯|投影|盒子|"
    + word(
        r"tv|camera|socket|speaker|headphones?|earbuds?|airpods|plug|vacuum|light|projector|box"
    ),
    re.IGNORECASE,
)
BRAND_RULES = (
    (
        "apple",
        "Apple",
        r"苹果|"
        + word(
            r"apple|iphone|ipad|imac|mac(?:book(?:[-_ ]?(?:air|pro))?|[-_ ]?(?:mini|studio|pro))?|airpods"
        ),
    ),
    (
        "xiaomi",
        "Xiaomi",
        r"小米|红米|" + word(r"xiaomi|redmi(?:book)?|poco|mibook|mi[-_ ]?band"),
    ),
    ("huawei", "Huawei", r"华为|" + word(r"huawei|matebook|matepad")),
    ("honor", "HONOR", r"荣耀|" + word(r"honor|magicbook")),
    ("samsung", "Samsung", r"三星|" + word(r"samsung|galaxy")),
    ("oneplus", "OnePlus", r"一加|" + word(r"oneplus")),
    ("oppo", "OPPO", word(r"oppo")),
    ("vivo", "vivo", word(r"vivo|iqoo")),
    ("google", "Google", r"谷歌|" + word(r"google|pixel")),
    (
        "lenovo",
        "Lenovo",
        r"联想|" + word(r"lenovo|thinkpad|thinkbook|ideapad|yogabook|xiaoxin[-_ ]?pad"),
    ),
    ("asus", "ASUS", r"华硕|" + word(r"asus|zenbook|vivobook")),
    ("dell", "Dell", r"戴尔|" + word(r"dell|latitude")),
    ("hp", "HP", r"惠普|" + word(r"hp|elitebook|probook|laserjet|deskjet|officejet")),
    ("acer", "Acer", r"宏碁|" + word(r"acer")),
    ("tplink", "TP-Link", r"普联|" + word(r"tp[-_ ]?link")),
    ("epson", "Epson", r"爱普生|" + word(r"epson")),
    ("nokia", "Nokia", r"诺基亚|" + word(r"nokia")),
    ("motorola", "Motorola", r"摩托罗拉|" + word(r"motorola")),
    ("sony", "Sony", r"索尼|" + word(r"sony|xperia")),
    (
        "windows",
        "Windows",
        r"微软|"
        + word(
            r"microsoft|surface(?:[-_ ]?pro)?|windows|win(?:[-_ ]?(?:xp|vista|7|8|10|11))?"
        ),
    ),
    ("android", "Android", r"安卓|" + word(r"android")),
)
BRAND_PATTERNS = tuple(
    (brand, label, re.compile(pattern, re.IGNORECASE))
    for brand, label, pattern in BRAND_RULES
)


def device_icon(device, service_detected=False):
    name = unicodedata.normalize("NFKC", str(device.get("name") or "")).casefold()
    other_type = bool(OTHER_TYPE.search(name))
    kind = next(
        (kind for kind, pattern in EXPLICIT_PATTERNS if pattern.search(name)), "lan"
    )
    if kind == "lan" and not other_type:
        kind = next(
            (kind for kind, pattern in NAME_PATTERNS if pattern.search(name)), "lan"
        )
    if kind == "lan" and not other_type:
        if PC_HINT.search(name):
            kind = "desktop"
        elif PHONE_HINT.search(name) or "安卓" in name:
            kind = "phone"
    if (
        kind == "lan"
        and not other_type
        and (service_detected or device.get("pc_service_bound"))
    ):
        kind = "desktop"
    override = device.get("type_override", "")
    if override in ICONS and override != "windows":
        kind = override
    device_type = kind
    brand, brand_label = next(
        (
            (brand, label)
            for brand, label, pattern in BRAND_PATTERNS
            if pattern.search(name)
        ),
        ("", ""),
    )
    label, path = ICONS[device_type]
    logo = brand in LOGO_PATHS or brand == "windows"
    if logo:
        kind = brand
        path = ICONS["windows"][1] if brand == "windows" else LOGO_PATHS[brand]
    # An otherwise unidentified device with this Windows-only service is a PC.
    if (
        device_type in {"desktop", "laptop"}
        and device.get("pc_service_bound")
        and not device.get("pc_service_ignored")
    ):
        kind = "windows"
        label, path = ICONS[kind]
        brand_label = ""
        logo = True
    return {
        "device_type": device_type,
        "power_capable": device_type in {"desktop", "laptop"},
        "icon_kind": kind,
        "icon_solid": logo,
        "icon_brand": brand_label,
        "icon_label": label,
        "icon_path": path,
    }
