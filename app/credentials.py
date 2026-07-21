from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError


class CredentialStore(Protocol):
    def set_password(self, username: str, password: str) -> None: ...

    def delete_password(self, username: str) -> None: ...

    def is_configured(self, username: str) -> bool: ...


class CredentialStorageError(RuntimeError):
    pass


class KeyringCredentialStore:
    def __init__(self, service_name: str = "daily-task-console") -> None:
        self.service_name = service_name

    def set_password(self, username: str, password: str) -> None:
        try:
            keyring.set_password(self.service_name, username, password)
        except KeyringError as exc:
            raise CredentialStorageError("系统凭据库不可用") from exc

    def delete_password(self, username: str) -> None:
        try:
            keyring.delete_password(self.service_name, username)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as exc:
            raise CredentialStorageError("系统凭据库不可用") from exc

    def is_configured(self, username: str) -> bool:
        if not username:
            return False
        try:
            return keyring.get_password(self.service_name, username) is not None
        except KeyringError:
            return False
