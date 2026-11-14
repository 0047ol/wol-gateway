"""Unicode names that fit the firmware's 64-byte UTF-8 fields."""

NAME_LIMIT = 16
NAME_ERROR = "名称最多 16 个字符"


def valid_name(value):
    return len(value) <= NAME_LIMIT and len(value.encode("utf-8")) <= 64
