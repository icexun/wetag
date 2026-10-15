"""企微待办子域连通性探测（``01-overall`` §12 D-1 建议的"开工第一个动作"）。

用法::

    python scripts/check_wecom_todo.py
    python scripts/check_wecom_todo.py --bin /path/to/wecom-cli --probe-name 张三

为什么它是一个独立脚本而不是一条单元测试：它要回答的问题是**范围问题**——
E-4（Todo 提取与确认卡片）与 E-5（提醒与逾期升级）今日是否在交付范围内。
D-1 把 ``TodoPort`` 定成单实现无降级，于是这个子域成了这两项能力的单点。
越早证伪越省事：不可用就把 E-4 / E-5 移出今日范围，在群内文案里明示能力缺失，
**而不是写一个本地台账替身**（替身会让系统每一处状态都自洽，唯独与当事人的
待办列表不一致，见 ``todo/wecom.py`` 模块 docstring）。

退出码：

* ``0`` 待办域可用；
* ``2`` 二进制不存在（今日预期结果，计划 R2）；
* ``3`` 二进制在但子域受限 / 凭据无效——这一条比 2 更值得立刻处理，因为它意味着
  能力本来能有，只差配置。

刻意不做的事：**不建真待办**。探测只跑只读的 ``user search``——建一条待办能证明
的更多，代价是给真人手机上推一条垃圾待办，而这是无法撤回的（删除类命令不登记）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.capability.integration.todo.wecom import (  # noqa: E402
    AmbiguousUser,
    WecomTodoAdapter,
    WecomTodoUnavailable,
)
from app.platform.cli.executor import CliExecutor  # noqa: E402
from app.platform.cli.registry import WECOM_COMMANDS  # noqa: E402
from app.platform.cli.result import CliErrorCode  # noqa: E402

EXIT_OK = 0
EXIT_BINARY_MISSING = 2
EXIT_SUBDOMAIN_UNAVAILABLE = 3


def _line(mark: str, text: str) -> None:
    print(f"{mark}  {text}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="企微待办子域探测（只读）")
    parser.add_argument(
        "--bin",
        default=os.environ.get("WECOM_CLI_BIN", "wecom-cli"),
        help="wecom-cli 路径，默认取 $WECOM_CLI_BIN",
    )
    parser.add_argument(
        "--probe-name",
        default="__wetag_probe__",
        help="用于 user search 的显示名。默认是一个必然查不到的名字——"
        "查不到即证明子域通了，不需要真存在的人",
    )
    parser.add_argument("--workspace", default=os.environ.get("WORKSPACE_ID", "demo"))
    args = parser.parse_args()

    print("=== 企微待办子域探测（D-1：此域不可用则 E-4/E-5 不交付，不做本地替身）===")

    # 第一层：文件在不在。这一层不需要启动子进程，也不需要凭据。
    resolved = shutil.which(args.bin)
    if resolved is None:
        _line("❌ FAIL", f"{args.bin} 不在 PATH 上")
        print()
        print("结论：待办域不可用（二进制缺失）。按 D-1，今日应：")
        print("  1. 把 E-4 / E-5 移出交付范围；")
        print("  2. 群内卡片明示「待办跟进能力暂不可用」；")
        print("  3. 不实现本地台账替身。")
        return EXIT_BINARY_MISSING
    _line("✅ PASS", f"二进制存在：{resolved}")

    token = os.environ.get("WECOM_CLI_TOKEN")
    _line(
        "✅ PASS" if token else "⚠️  WARN",
        "WECOM_CLI_TOKEN 已配置" if token else "WECOM_CLI_TOKEN 未配置（鉴权多半会失败）",
    )

    executor = CliExecutor(
        workspace_id=args.workspace,
        binaries={"wecom": args.bin},
        # 只注入这一条命令声明需要的凭据，且只有本业务线的（D-2 进程级隔离）
        secrets={"WECOM_CLI_TOKEN": token} if token else {},
        specs=WECOM_COMMANDS,
    )
    adapter = WecomTodoAdapter(executor, workspace_id=args.workspace)

    # 第二层：子命令路径对不对、凭据有没有效。
    result = await executor.run(
        "wecom.todo.user_search",
        {"keywords": [args.probe_name], "search_mode": "list"},
    )
    _line("ℹ️  INFO", f"user search → ok={result.ok} code={result.error_code.value}")

    if result.error_code is CliErrorCode.INVALID_ARGUMENT:
        _line(
            "❌ FAIL",
            "参数被拒：极可能是 registry.WECOM_COMMANDS 的 flag 名与实际 CLI 不一致。"
            "请用 `wecom-cli contact users search --help` 校对后重跑",
        )
        return EXIT_SUBDOMAIN_UNAVAILABLE

    if result.error_code in (
        CliErrorCode.AUTH_FAILED,
        CliErrorCode.PERMISSION_DENIED,
    ):
        _line("❌ FAIL", "二进制与子命令都在，但鉴权/权限不通——差的是配置，不是能力")
        print("  → 这比「没装」更值得立刻处理：补齐凭据或申请待办域权限即可让 E-4/E-5 回到范围内")
        return EXIT_SUBDOMAIN_UNAVAILABLE

    if not result.ok and result.error_code is not CliErrorCode.NOT_FOUND:
        _line("❌ FAIL", f"探测命令失败：{result.stderr_digest or result.stdout_digest}")
        return EXIT_SUBDOMAIN_UNAVAILABLE

    _line("✅ PASS", "待办子域已下发（只读命令可执行）")

    # 第三层：真名解析。只有显式给了 --probe-name 才做——它需要一个真实存在的人。
    if args.probe_name != "__wetag_probe__":
        try:
            ref = await adapter.resolve_user(args.probe_name)
            _line("✅ PASS", f"resolve_user({args.probe_name}) → {ref.user_id}")
        except AmbiguousUser as exc:
            # 这**不是**失败：它证明子域完全可用，只是这个名字需要澄清。
            _line("✅ PASS", f"子域可用；{args.probe_name} 匹配到多人 → 需澄清（预期行为）")
            _line("ℹ️  INFO", f"候选：{', '.join(exc.candidates)}")
        except WecomTodoUnavailable as exc:
            _line("❌ FAIL", f"resolve_user 失败：{exc.error_code} {exc.reason}")
            return EXIT_SUBDOMAIN_UNAVAILABLE

    print()
    print("结论：待办域可用，E-4 / E-5 可进入交付范围。")
    print("提醒：建待办无幂等键，调用方必须走 claim/settle；重试等于第二条待办。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
