"""Recoverable share passwords for administrators; login still uses a hash."""

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .config import settings

_key_lock = threading.Lock()


def _cipher(create=False):
    path = Path(
        settings.SHARE_SECRET_PATH
        or Path(settings.ADMIN_AUTH_PATH).with_name(".share-secret.key")
    )
    with _key_lock:
        if create and not path.exists():
            key = Fernet.generate_key()
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
        return Fernet(path.read_bytes())


def seal_password(password, token):
    try:
        return (
            _cipher(create=True)
            .encrypt(json.dumps([token, password]).encode())
            .decode()
        )
    except (OSError, ValueError):
        raise HTTPException(503, "分享密码保存暂不可用，请稍后重试") from None


def reveal_password(secret, token):
    if not secret:
        return ""
    try:
        context, password = json.loads(_cipher().decrypt(secret.encode()))
        return password if context == token and isinstance(password, str) else ""
    except (OSError, ValueError, TypeError, InvalidToken):
        return ""
