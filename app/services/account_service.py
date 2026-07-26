from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from app.config_store import ConfigStore
from app.credentials import CredentialStore
from game_session import GameSessionManager
from harvest_fief import (
    AccountZone,
    GameEndpoint,
    HarvestError,
    build_parser as build_game_parser,
    list_zones,
    request_account_session,
    resolve_game_endpoint,
)
from login import (
    DEFAULT_CLIENT_IP,
    DEFAULT_DEVICE_INFO,
    DEFAULT_GAME_CODE,
    DEFAULT_TERMINAL_INFO,
    DEFAULT_TOKEN_FILE,
    GameLoginClient,
    LoginError,
    LoginResult,
    TokenStore,
)


class AccountLoginError(RuntimeError):
    """登录或区服列表请求失败时返回给本地界面的脱敏错误。"""


class LoginClient(Protocol):
    def login_with_password(
        self,
        *,
        username: str,
        password: str,
        game_code: str,
        ip: str,
        device_info: str,
        terminal_info: str,
    ) -> LoginResult:
        """完成渠道登录并交换游戏侧凭据。"""

    def login_with_refresh(
        self,
        *,
        refresh_token: str,
        game_code: str,
        ip: str,
        device_info: str,
        terminal_info: str,
    ) -> LoginResult:
        """使用 refresh_token 刷新身份并交换游戏侧凭据。"""


class TokenResultStore(Protocol):
    def save(self, result: LoginResult) -> None:
        """安全保存游戏侧凭据。"""

    def load_game_tokens(self) -> dict[str, str] | None:
        """读取本地缓存的游戏侧凭据。"""

    def load_refresh_token(self) -> str | None:
        """读取本地缓存的 refresh_token。"""


ZoneLoader = Callable[[Mapping[str, str]], Sequence[AccountZone]]
EndpointResolver = Callable[[Mapping[str, str], Namespace], GameEndpoint]


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    raw_id: int | str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


def _load_account_zones(tokens: Mapping[str, str]) -> tuple[AccountZone, ...]:
    """复用入口解析中的 Logincheck 请求，仅获取账号的区服列表。"""

    args = build_game_parser().parse_args([])
    session = request_account_session(tokens, args)
    return list_zones(session.zone_info)


class AccountService:
    def __init__(
        self,
        config_store: ConfigStore,
        credentials: CredentialStore,
        *,
        login_client_factory: Callable[[], LoginClient] = GameLoginClient,
        token_store: TokenResultStore | None = None,
        zone_loader: ZoneLoader = _load_account_zones,
        endpoint_resolver: EndpointResolver = resolve_game_endpoint,
        restore_on_init: bool = True,
        session_manager: GameSessionManager | None = None,
    ) -> None:
        self._config_store = config_store
        self._credentials = credentials
        self._login_client_factory = login_client_factory
        self._token_store = token_store or TokenStore(DEFAULT_TOKEN_FILE)
        self._zone_loader = zone_loader
        self._endpoint_resolver = endpoint_resolver
        self._session_manager = session_manager
        self._connection_status = "unconfigured"
        self._zones: tuple[Zone, ...] = ()
        self._game_tokens: dict[str, str] | None = None
        if restore_on_init:
            self.restore_session()

    def _close_game_session(self) -> None:
        """Drop the shared game WebSocket (SocketManager.destroy / logout)."""

        if self._session_manager is not None:
            self._session_manager.close()

    def login(
        self, username: str, password: str, remember_password: bool
    ) -> dict[str, object]:
        resolved_password = self._resolve_password(username, password)
        self._connection_status = "logging_in"
        self._game_tokens = None
        self._close_game_session()
        try:
            result = self._login_client_factory().login_with_password(
                username=username,
                password=resolved_password,
                game_code=DEFAULT_GAME_CODE,
                ip=DEFAULT_CLIENT_IP,
                device_info=DEFAULT_DEVICE_INFO,
                terminal_info=DEFAULT_TERMINAL_INFO,
            )
            zones = self._zones_from_tokens(
                {
                    "userid": result.game.userid,
                    "verify_token": result.game.verify_token,
                }
            )
            self._token_store.save(result)
            if remember_password:
                self._credentials.set_password(username, resolved_password)
            else:
                self._credentials.delete_password(username)
            self._config_store.set_account(username, remember_password)
            self._activate_session(
                {
                    "userid": result.game.userid,
                    "verify_token": result.game.verify_token,
                },
                zones,
            )
        except Exception as exc:
            self._zones = ()
            self._game_tokens = None
            self._connection_status = "failed"
            if isinstance(exc, AccountLoginError):
                raise
            if isinstance(exc, (LoginError, HarvestError, OSError, ValueError)):
                raise AccountLoginError(
                    "登录或获取区服失败，请检查账号、密码、网络和区服状态"
                ) from exc
            raise AccountLoginError("登录或获取区服时发生本地错误") from exc

        return {
            "zones": [zone.to_dict() for zone in self._zones],
            "connection": self.connection_snapshot(),
            "message": f"已加载 {len(self._zones)} 个账号区服",
        }

    def restore_session(self) -> bool:
        """从本地令牌（必要时刷新令牌或记住的密码）恢复登录态。

        成功返回 True；无可用缓存或恢复失败时保持未配置状态并返回 False。
        """

        if self._connection_status == "available" and self._game_tokens is not None:
            return True

        game_tokens = self._token_store.load_game_tokens()
        if game_tokens is not None and self._try_activate_tokens(game_tokens):
            return True

        if self._try_refresh_tokens():
            return True

        if self._try_password_restore():
            return True

        return False

    def _resolve_password(self, username: str, password: str) -> str:
        if password:
            return password
        stored = self._credentials.get_password(username)
        if stored:
            return stored
        raise AccountLoginError("请输入密码，或先勾选记住密码并成功登录一次")

    def _zones_from_tokens(self, game_tokens: Mapping[str, str]) -> tuple[Zone, ...]:
        account_zones = self._zone_loader(game_tokens)
        zones = tuple(
            Zone(str(zone.zone_id), zone.name, zone.zone_id)
            for zone in account_zones
        )
        if not zones:
            raise AccountLoginError("账号没有可用区服")
        return zones

    def _activate_session(
        self, game_tokens: Mapping[str, str], zones: tuple[Zone, ...]
    ) -> None:
        self._clear_unavailable_selected_zone(zones)
        self._zones = zones
        self._game_tokens = dict(game_tokens)
        self._connection_status = "available"

    def _try_activate_tokens(self, game_tokens: dict[str, str]) -> bool:
        try:
            zones = self._zones_from_tokens(game_tokens)
            self._activate_session(game_tokens, zones)
            return True
        except (AccountLoginError, HarvestError, OSError, ValueError, LoginError):
            return False

    def _try_refresh_tokens(self) -> bool:
        refresh_token = self._token_store.load_refresh_token()
        if not refresh_token:
            return False
        try:
            result = self._login_client_factory().login_with_refresh(
                refresh_token=refresh_token,
                game_code=DEFAULT_GAME_CODE,
                ip=DEFAULT_CLIENT_IP,
                device_info=DEFAULT_DEVICE_INFO,
                terminal_info=DEFAULT_TERMINAL_INFO,
            )
            game_tokens = {
                "userid": result.game.userid,
                "verify_token": result.game.verify_token,
            }
            if not self._try_activate_tokens(game_tokens):
                return False
            self._token_store.save(result)
            return True
        except Exception:
            return False

    def _try_password_restore(self) -> bool:
        settings = self._config_store.snapshot()
        username = settings.account.username
        if not settings.account.remember_password or not username:
            return False
        password = self._credentials.get_password(username)
        if not password:
            return False
        try:
            result = self._login_client_factory().login_with_password(
                username=username,
                password=password,
                game_code=DEFAULT_GAME_CODE,
                ip=DEFAULT_CLIENT_IP,
                device_info=DEFAULT_DEVICE_INFO,
                terminal_info=DEFAULT_TERMINAL_INFO,
            )
            game_tokens = {
                "userid": result.game.userid,
                "verify_token": result.game.verify_token,
            }
            if not self._try_activate_tokens(game_tokens):
                return False
            self._token_store.save(result)
            return True
        except Exception:
            return False

    def _clear_unavailable_selected_zone(self, zones: tuple[Zone, ...]) -> None:
        selected = self._config_store.snapshot().zone
        if selected.id and not any(zone.id == selected.id for zone in zones):
            self._config_store.set_zone("", "")

    def select_zone(self, zone_id: str, zone_name: str) -> None:
        zone = next((item for item in self._zones if item.id == zone_id), None)
        if zone is None or zone.name != zone_name:
            raise ValueError("区服不在当前登录结果中")
        previous = self._config_store.snapshot().zone
        self._config_store.set_zone(zone.id, zone.name)
        if previous.id != zone.id or previous.name != zone.name:
            # New zone needs a fresh game-server Login (native reconnect path).
            self._close_game_session()

    def resolve_selected_game_endpoint(self) -> GameEndpoint:
        """为当前登录和已选区服解析一次临时游戏服入口。"""

        tokens = self._game_tokens
        selected = self._config_store.snapshot().zone
        if self._connection_status != "available" or tokens is None:
            raise AccountLoginError("请先登录并获取区服")
        if not selected.id:
            raise AccountLoginError("请先选择区服")
        if not any(zone.id == selected.id for zone in self._zones):
            raise AccountLoginError("当前区服不在本次登录结果中")

        args = build_game_parser().parse_args(["--zone-id", selected.id])
        try:
            return self._endpoint_resolver(tokens, args)
        except (HarvestError, OSError, ValueError) as exc:
            raise AccountLoginError(
                "解析游戏服入口失败，请重新登录并选择区服"
            ) from exc

    def connection_snapshot(self) -> dict[str, str]:
        labels = {
            "unconfigured": "未配置",
            "logging_in": "登录中",
            "available": "可用",
            "failed": "失败",
        }
        return {"status": self._connection_status, "label": labels[self._connection_status]}

    def zones(self) -> list[dict[str, str]]:
        return [zone.to_dict() for zone in self._zones]

    def password_configured(self) -> bool:
        settings = self._config_store.snapshot()
        return settings.account.remember_password and self._credentials.is_configured(
            settings.account.username
        )
