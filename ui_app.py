from __future__ import annotations

import argparse

from app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日任务本地操作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="监视源码变化并自动重启（开发模式；会中断运行中的任务）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间")
    app = create_app()
    app.run(
        host=args.host,
        port=args.port,
        debug=False,
        use_reloader=args.reload,
        threaded=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
