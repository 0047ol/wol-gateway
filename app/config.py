"""全局配置：从环境变量读取，给一套本地默认值。"""

import os
import json


def _mqtt_auth():
    try:
        with open(
            os.getenv(
                "MQTT_AUTH_FILE",
                os.path.join(
                    os.path.dirname(os.path.dirname(__file__)), ".mqtt-auth.json"
                ),
            ),
            encoding="utf-8",
        ) as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


_mqtt_credentials = _mqtt_auth()


def _get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # HTTP
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8088"))

    # 调试模式：环境变量 DEBUG=1 开启
    # - uvicorn 自动 reload
    # - 日志级别 DEBUG
    DEBUG = _get_bool("DEBUG", True)

    # MQTT
    MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
    MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
    MQTT_USERNAME = os.getenv(
        "MQTT_USERNAME", _mqtt_credentials.get("username", "wol_server")
    )
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", _mqtt_credentials.get("password", ""))

    # 主题前缀
    MQTT_TOPIC_PREFIX = "wol/gateway"

    MQTT_AUTH_DIR = os.getenv("MQTT_AUTH_DIR", "")
    MQTT_PUBLIC_HOST = os.getenv("MQTT_PUBLIC_HOST", "YOUR_SERVER_HOST")
    MQTT_PUBLIC_PORT = int(os.getenv("MQTT_PUBLIC_PORT", "8883"))
    MQTT_CA_FILE = os.getenv("MQTT_CA_FILE", "")
    ENROLLMENT_URL = os.getenv("ENROLLMENT_URL", "https://YOUR_SERVER_HOST:8443")
    MIN_FIRMWARE_VERSION = os.getenv("MIN_FIRMWARE_VERSION", "0.5.1")
    MIN_PC_VERSION = os.getenv("MIN_PC_VERSION", "1.1.0")
    ENFORCE_CLIENT_VERSIONS = True

    # 数据库
    # Production credentials live in a private JSON file, never in source or URLs.
    MYSQL_CONFIG_FILE = os.getenv("MYSQL_CONFIG_FILE", "")
    DB_BACKEND = os.getenv(
        "DB_BACKEND", "mysql" if MYSQL_CONFIG_FILE else "sqlite"
    ).lower()
    DB_PATH = os.getenv(
        "DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "wol.db")
    )
    ADMIN_AUTH_PATH = os.getenv(
        "ADMIN_AUTH_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), ".admin-auth.json"),
    )
    SHARE_SECRET_PATH = os.getenv("SHARE_SECRET_PATH", "")
    OTA_STORAGE_PATH = os.getenv("OTA_STORAGE_PATH", "")

    # 签名时间窗（秒）
    SIGN_TOLERANCE = 60

    # 心跳超时扫描间隔（秒）
    HB_SCAN_INTERVAL = 10

    # 指令 ack 超时（秒）
    CMD_ACK_TIMEOUT = 8

    # Legacy setting retained for old integrations; browser actions use admin login.
    POWER_ADMIN_TOKEN = os.getenv("POWER_ADMIN_TOKEN", "")
    POWER_AGENT_PUBLIC_URL = os.getenv("POWER_AGENT_PUBLIC_URL", "")

    # 分页默认值
    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100


settings = Settings()
