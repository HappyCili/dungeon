#!/usr/bin/env python3
"""根据 Android 渠道 SDK 还原的两段式登录客户端。

原生 SDK 先获取身份令牌，再将其交换为游戏侧使用的 ``verify_token``。
本模块明确实现该链路，并使用系统 TLS 证书校验，不复现 APK 中的宽松 TLS 配置。

用法示例：
    python login.py password --username 17767051461
    python login.py refresh
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://ocplatform.gamelunar.com"
CHANNEL_ID = "110001"
MEDIA_ID = "M521957"
# 以下默认值来自当前安装包 Manifest 和已连接 MuMu 模拟器。
DEFAULT_GAME_CODE = "dxcb4"
# 此值来自 /passport/ipme；网络出口变化时通过 --ip 覆盖。
DEFAULT_CLIENT_IP = "112.10.204.243"
DEFAULT_DEVICE_INFO = "2c54fe7b2fe5f0fe"
DEFAULT_TERMINAL_INFO = "HONOR REP-AN00"
SYSTEM_CA_FILE = Path("/etc/ssl/cert.pem")
CONTENT_TYPE = "application/x-www-form-urlencoded"
USER_AGENT = "dungeon4-login/1.0"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TOKEN_FILE = PROJECT_ROOT / "tokens.json"


class LoginError(RuntimeError):
    """服务端拒绝登录请求或返回了无效响应。"""


class ChineseArgumentParser(argparse.ArgumentParser):
    """将 argparse 的默认帮助标题替换为中文。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage:", "用法：")
            .replace("positional arguments:", "位置参数：")
            .replace("optional arguments:", "选项：")
            .replace("options:", "选项：")
        )


@dataclass(frozen=True)
class IdentityTokens:
    id_token: str
    refresh_token: str
    openid: str


@dataclass(frozen=True)
class GameTokens:
    userid: str
    verify_token: str
    pay_token: str
    is_new: int | None


@dataclass(frozen=True)
class LoginResult:
    identity: IdentityTokens
    game: GameTokens


class TokenStore:
    """用于替代 SDK 的 SharedPreferences ``config`` 文件的本地令牌存储。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def save(self, result: LoginResult) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "id_token": result.identity.id_token,
            # 保留 SDK 的原始键名拼写，以兼容其持久化格式。
            "refrsh_token": result.identity.refresh_token,
            "verify_token": result.game.verify_token,
            "pay_token": result.game.pay_token,
            "userid": result.game.userid,
            "openid": result.identity.openid,
        }
        with temporary.open("w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def _read_payload(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def load_game_tokens(self) -> dict[str, str] | None:
        """读取游戏侧 userid / verify_token；文件缺失或字段无效时返回 None。"""

        payload = self._read_payload()
        if payload is None:
            return None
        userid = payload.get("userid")
        verify_token = payload.get("verify_token")
        if (
            not isinstance(userid, str)
            or not userid
            or not isinstance(verify_token, str)
            or not verify_token
        ):
            return None
        return {"userid": userid, "verify_token": verify_token}

    def load_refresh_token(self) -> str | None:
        """读取 refrsh_token；文件缺失或字段无效时返回 None。"""

        payload = self._read_payload()
        if payload is None:
            return None
        token = payload.get("refrsh_token")
        if not isinstance(token, str) or not token:
            return None
        return token

    def refresh_token(self) -> str:
        token = self.load_refresh_token()
        if token is None:
            if not self.path.exists():
                raise LoginError(f"令牌文件不存在：{self.path}")
            raise LoginError("令牌文件不包含 refrsh_token")
        return token


class GameLoginClient:
    """实现渠道 SDK 的身份登录和游戏令牌交换。"""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        channel_id: str = CHANNEL_ID,
        media_id: str = MEDIA_ID,
        timeout: float = 15.0,
        ca_file: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.channel_id = channel_id
        self.media_id = media_id
        self.timeout = timeout
        self.ssl_context = _create_ssl_context(ca_file)

    def password_login(
        self,
        *,
        username: str,
        password: str,
        game_code: str,
        ip: str,
        device_info: str,
    ) -> IdentityTokens:
        return self._identity_login(
            "/passport/signin",
            {
                "utype": "0" if _is_mainland_phone(username) else "1",
                "username": username,
                "password": password,
                "gamecode": game_code,
                "ip": ip,
                "devinfo": device_info,
            },
        )

    def sms_login(
        self,
        *,
        phone_number: str,
        sms_code: str,
        game_code: str,
        ip: str,
        device_info: str,
    ) -> IdentityTokens:
        return self._identity_login(
            "/passport/signin",
            {
                "utype": "0",
                "username": phone_number,
                "smscode": sms_code,
                "gamecode": game_code,
                "ip": ip,
                "devinfo": device_info,
            },
        )

    def one_tap_login(
        self,
        *,
        carrier_token: str,
        carrier: str,
        app_id: str,
        game_code: str,
        ip: str,
        device_info: str,
    ) -> IdentityTokens:
        return self._identity_login(
            "/passport/signin_with_onetap",
            {
                "gamecode": game_code,
                "token": carrier_token,
                "carrier": carrier,
                "appid": app_id,
                "ip": ip,
                "devinfo": device_info,
            },
        )

    def refresh_identity(self, refresh_token: str) -> IdentityTokens:
        return self._identity_login(
            "/passport/refresh_token", {"token": refresh_token}
        )

    def exchange_game_token(
        self,
        *,
        identity: IdentityTokens,
        game_code: str,
        ip: str,
        device_info: str,
        terminal_info: str,
    ) -> GameTokens:
        data = self._post(
            "/passport/channel_signin",
            {
                "openid": identity.openid,
                "idtoken": identity.id_token,
                "game": game_code,
                "channel": self.channel_id,
                "ip": ip,
                "devinfo": device_info,
                "termininfo": terminal_info,
                "media": self.media_id,
            },
        )
        return GameTokens(
            userid=_required_string(data, "userid"),
            verify_token=_required_string(data, "verify_token"),
            pay_token=_required_string(data, "pay_token"),
            is_new=data.get("New") if isinstance(data.get("New"), int) else None,
        )

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
        identity = self.password_login(
            username=username,
            password=password,
            game_code=game_code,
            ip=ip,
            device_info=device_info,
        )
        return LoginResult(
            identity=identity,
            game=self.exchange_game_token(
                identity=identity,
                game_code=game_code,
                ip=ip,
                device_info=device_info,
                terminal_info=terminal_info,
            ),
        )

    def login_with_sms(
        self,
        *,
        phone_number: str,
        sms_code: str,
        game_code: str,
        ip: str,
        device_info: str,
        terminal_info: str,
    ) -> LoginResult:
        identity = self.sms_login(
            phone_number=phone_number,
            sms_code=sms_code,
            game_code=game_code,
            ip=ip,
            device_info=device_info,
        )
        return LoginResult(
            identity=identity,
            game=self.exchange_game_token(
                identity=identity,
                game_code=game_code,
                ip=ip,
                device_info=device_info,
                terminal_info=terminal_info,
            ),
        )

    def login_with_refresh(
        self,
        *,
        refresh_token: str,
        game_code: str,
        ip: str,
        device_info: str,
        terminal_info: str,
    ) -> LoginResult:
        identity = self.refresh_identity(refresh_token)
        return LoginResult(
            identity=identity,
            game=self.exchange_game_token(
                identity=identity,
                game_code=game_code,
                ip=ip,
                device_info=device_info,
                terminal_info=terminal_info,
            ),
        )

    def _identity_login(
        self, endpoint: str, parameters: Mapping[str, str]
    ) -> IdentityTokens:
        data = self._post(endpoint, parameters)
        id_token = _required_string(data, "id_token")
        return IdentityTokens(
            id_token=id_token,
            refresh_token=_required_string(data, "refresh_token"),
            openid=_openid_from_jwt_payload(id_token),
        )

    def _post(self, endpoint: str, parameters: Mapping[str, str]) -> Mapping[str, Any]:
        # APK 的 jsonPost() 使用 POST，但会将全部参数拼接到 URL 中。
        query = urlencode(parameters)
        request = Request(
            f"{self.base_url}{endpoint}?{query}",
            data=b"",
            method="POST",
            headers={"Content-Type": CONTENT_TYPE, "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LoginError(f"HTTP 状态 {exc.code}：{detail}") from exc
        except URLError as exc:
            raise LoginError(f"网络错误：{exc.reason}") from exc

        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise LoginError("服务端响应不是 JSON") from exc
        if not isinstance(payload, dict):
            raise LoginError("服务端响应不是 JSON 对象")
        if payload.get("code") != 0:
            message = payload.get("err") or payload.get("msg") or "未知登录错误"
            raise LoginError(f"服务端拒绝该请求：{message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LoginError("成功响应中没有 data 对象")
        return data


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LoginError(f"响应不包含非空字段：{key}")
    return value


def _create_ssl_context(ca_file: Path | None) -> ssl.SSLContext:
    """创建保持证书和主机名校验的 TLS 上下文。"""
    candidate = ca_file or _default_ca_file()
    try:
        if candidate is not None:
            return ssl.create_default_context(cafile=str(candidate))
        return ssl.create_default_context()
    except (OSError, ssl.SSLError) as exc:
        raise LoginError(f"无法加载 CA 证书文件：{candidate}") from exc


def _default_ca_file() -> Path | None:
    """优先使用环境变量指定的 CA 文件，其次使用系统证书包。"""
    env_ca_file = os.environ.get("SSL_CERT_FILE")
    if env_ca_file:
        path = Path(env_ca_file).expanduser()
        if path.is_file():
            return path
    if SYSTEM_CA_FILE.is_file():
        return SYSTEM_CA_FILE
    default_path = ssl.get_default_verify_paths().cafile
    if default_path:
        path = Path(default_path)
        if path.is_file():
            return path
    return None


def _openid_from_jwt_payload(id_token: str) -> str:
    parts = id_token.split(".")
    if len(parts) < 2:
        raise LoginError("id_token 不符合 JWT 格式")
    encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload))
    except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise LoginError("id_token 载荷不是有效的 Base64 JSON") from exc
    if not isinstance(payload, dict):
        raise LoginError("id_token 载荷不是对象")
    return _required_string(payload, "openid")


def _is_mainland_phone(value: str) -> bool:
    return len(value) == 11 and value.startswith("1") and value.isdigit()


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _print_result(result: LoginResult, token_file: Path) -> None:
    print("登录成功")
    print("账户标识已接收")
    print(f"verify_token：{_redact(result.game.verify_token)}")
    print(f"refresh_token：{_redact(result.identity.refresh_token)}")
    print(f"令牌已保存至：{token_file.expanduser()}")


def _add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--game", default=DEFAULT_GAME_CODE, help="SDK 的 gamecode 值")
    parser.add_argument(
        "--ip",
        default=DEFAULT_CLIENT_IP,
        help="SDK 发送的客户端 IP 值；网络出口变化时请覆盖",
    )
    parser.add_argument(
        "--device-info", default=DEFAULT_DEVICE_INFO, help="Android 的 devinfo 值"
    )
    parser.add_argument(
        "--terminal-info",
        default=DEFAULT_TERMINAL_INFO,
        help="channel_signin 使用的 termininfo 值",
    )
    parser.add_argument("--channel", default=CHANNEL_ID, help="渠道 ID")
    parser.add_argument("--media", default=MEDIA_ID, help="媒体 ID")
    parser.add_argument("--base-url", default=BASE_URL, help="认证服务基础 URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP 超时时间，单位为秒")
    parser.add_argument(
        "--ca-file",
        type=Path,
        default=_default_ca_file(),
        help="CA 证书文件；默认自动使用系统证书包",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"本地令牌存储文件（默认：{DEFAULT_TOKEN_FILE}）",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=ChineseArgumentParser
    )

    password = commands.add_parser("password", help="使用账号和密码登录")
    _add_connection_arguments(password)
    password.add_argument("--username", required=True)

    sms = commands.add_parser("sms", help="使用手机号和已有短信验证码登录")
    _add_connection_arguments(sms)
    sms.add_argument("--phone", required=True)
    sms.add_argument("--sms-code", required=True)

    refresh = commands.add_parser("refresh", help="刷新本地身份令牌并交换游戏令牌")
    _add_connection_arguments(refresh)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = GameLoginClient(
        base_url=args.base_url,
        channel_id=args.channel,
        media_id=args.media,
        timeout=args.timeout,
        ca_file=args.ca_file,
    )
    store = TokenStore(args.token_file)

    try:
        if args.command == "password":
            password = getpass.getpass("密码：")
            result = client.login_with_password(
                username=args.username,
                password=password,
                game_code=args.game,
                ip=args.ip,
                device_info=args.device_info,
                terminal_info=args.terminal_info,
            )
        elif args.command == "sms":
            result = client.login_with_sms(
                phone_number=args.phone,
                sms_code=args.sms_code,
                game_code=args.game,
                ip=args.ip,
                device_info=args.device_info,
                terminal_info=args.terminal_info,
            )
        else:
            result = client.login_with_refresh(
                refresh_token=store.refresh_token(),
                game_code=args.game,
                ip=args.ip,
                device_info=args.device_info,
                terminal_info=args.terminal_info,
            )
        store.save(result)
    except LoginError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1

    _print_result(result, store.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
