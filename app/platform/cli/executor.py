"""``CliExecutor``：三个 CLI 共用的安全与可观测收口点（doc2 §5.2、§10.2 第 5 项）。

七条纪律都在这里落成代码，任何一条绕过都必须改本文件（因此 review 有唯一落点）：

1. **参数数组执行**——``asyncio.create_subprocess_exec``，永不 ``shell=True``、
   永不字符串拼接。参数由 Pydantic model 的字段**生成**，而不是由调用方提供 argv。
2. **命令白名单**——未在 :class:`CliCommandSpec` 登记的 key 直接拒绝。
   **删除类命令一律不登记**（裁决 #10：不可达优于运行时拒绝）。
3. **资源限制**——独立超时（``asyncio.wait_for``，本机无 ``timeout``/``gtimeout``）、
   最大输出字节、工作目录。
4. **凭据注入**——最小环境 + 仅当前 workspace 的一组密钥。
5. **输出处理**——统一 ``CliResult``，业务层永不接触 stdout 原文。
6. **日志脱敏**——摘要一律过 ``result.digest()``。
7. **审计**——每次调用落 ``AuditSink.tool_call``（裁决 #9：并入 audit_log）。

``AuditSink`` / ``AlertSink`` / ``SpawnFn`` 都是 Protocol，由 ``container.py`` 注入：
``platform/`` **不反向 import** ``runtime/``（依赖方向纪律），``SpawnFn`` 同时是
测试注入点——算法层测试不需要真的起进程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Protocol, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from app.platform.cli.error_map import (
    CliAction,
    action_for,
    classify,
    is_retryable,
    is_security_event,
)
from app.platform.cli.result import CliErrorCode, CliResult, digest

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class RiskLevel(StrEnum):
    """§10.3 四级审批分档。

    今日 Auto Mode 全通（裁决 #13），但分档必须先存在——否则"以后再加审批"
    会变成"永远没有审批"。``DESTRUCTIVE`` 刻意定义了却**没有任何命令登记**：
    删除类操作在白名单里不存在，从而不可达。
    """

    READ = "READ"
    LOW_WRITE = "LOW_WRITE"
    HIGH_WRITE = "HIGH_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


# --- 注入点（Protocol，由 container.py 装配）---------------------------------


class AuditSink(Protocol):
    """审计落点。Capability 经注入的它写入，不反向 import runtime。"""

    async def tool_call(self, **fields: Any) -> None: ...


class AlertSink(Protocol):
    """告警通道。``security`` 用于疑似越权/凭据失效，**不静默重试**。"""

    async def security(self, title: str, **fields: Any) -> None: ...

    async def warn(self, title: str, **fields: Any) -> None: ...


class SpawnFn(Protocol):
    """起子进程。测试注入 ``FakeSpawn`` 回放 fixture，无需真的装 CLI。"""

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]: ...


class _NullAudit:
    async def tool_call(self, **fields: Any) -> None:
        logger.debug("audit.tool_call %s", fields)


class _NullAlert:
    async def security(self, title: str, **fields: Any) -> None:
        logger.error("SECURITY %s %s", title, fields)

    async def warn(self, title: str, **fields: Any) -> None:
        logger.warning("%s %s", title, fields)


# --- 命令登记 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CliCommandSpec:
    """一条被允许执行的命令。

    ``argv_prefix`` 是**固定**的子命令路径，调用方无法影响它；
    可变部分只能通过 ``args_model`` 的字段进入，并由 :func:`_argv_from_model`
    渲染成 ``--kebab-case value``（``arg_style="flag"``）或 ``key=value``
    （``arg_style="kv"``，tapd-cli）形态。
    """

    key: str
    binary_key: str
    argv_prefix: tuple[str, ...]
    args_model: type[BaseModel]
    risk: RiskLevel = RiskLevel.READ
    timeout_s: float | None = None
    max_output_bytes: int | None = None
    json_output: bool = True
    # 需要注入的凭据环境变量名（与当前 workspace 的 secrets 取交集）
    env_keys: tuple[str, ...] = ()
    max_attempts: int | None = None
    # 位置参数字段名，按声明顺序渲染成裸值追加在末尾（如 rg 的搜索路径）。
    # 仍然是 argv 数组的独立元素，不存在被当成 flag 或 shell 语法的可能。
    positional_fields: tuple[str, ...] = ()
    # 视为"正常"的退出码。rg 无命中时退出 1，那是空结果而不是故障。
    ok_exit_codes: tuple[int, ...] = (0,)
    # "flag"：--kebab-case value；"kv"：key=value（键名取字段别名，原样输出）
    arg_style: str = "flag"


def _kebab(name: str) -> str:
    return "--" + name.replace("_", "-")


def _kv_argv(dumped: dict[str, Any]) -> list[str]:
    argv: list[str] = []
    for name, value in dumped.items():
        if isinstance(value, bool):
            if value:
                argv.append(f"{name}=1")
        elif isinstance(value, (list, tuple)):
            argv.append(f"{name}={','.join(str(v) for v in value)}")
        else:
            argv.append(f"{name}={value}")
    return argv


def _argv_from_model(
    model: BaseModel, positional_fields: Sequence[str] = (), arg_style: str = "flag"
) -> list[str]:
    """由 Pydantic model 字段生成参数数组。

    规则刻意简单，因为复杂就意味着有洞：

    * ``None`` 与未设置的字段跳过；
    * ``bool`` 只在 True 时输出裸 flag（kv 形态为 ``key=1``）；
    * ``list``/``tuple`` 重复输出同一个 flag（kv 形态逗号拼接）；
    * ``positional_fields`` 里的字段输出裸值，按声明顺序追加在**末尾**；
    * 其余转 ``str``。

    每一项都是 argv 数组里的**独立元素**，shell 元字符不具备任何语法意义。
    """
    argv: list[str] = []
    tail: list[str] = []
    # by_alias：字段名与 Python 关键字/BaseModel 属性冲突时用 alias 表达真实 flag
    dumped = model.model_dump(exclude_none=True, by_alias=True)
    if arg_style == "kv":
        return _kv_argv(dumped)
    positional = tuple(positional_fields)
    for name, value in dumped.items():
        if name in positional:
            continue
        flag = _kebab(name)
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([flag, str(item)])
        else:
            argv.extend([flag, str(value)])
    for name in positional:
        if name in dumped and dumped[name] is not None:
            value = dumped[name]
            if isinstance(value, (list, tuple)):
                tail.extend(str(v) for v in value)
            else:
                tail.append(str(value))
    return argv + tail


_WECOM_STATUS_KEYS = ("errcode", "errmsg", "help_message")


def _envelope_status(payload: Any) -> tuple[bool | None, str]:
    """读取 CLI 自报的结构化状态。

    ``lark-cli`` / ``tapd-cli`` 统一返回 ``{"ok": bool, "error": {...}}``。
    这是**唯一权威**的成功/失败信号：它由 CLI 自己产生，不受被读取的文档内容影响。

    ``wecom-cli`` 返回 ``{"errcode": int, "errmsg": str}``，同样是 CLI 自报的状态。

    :returns: ``(ok, error_text)``。``ok is None`` 表示不是信封格式。
    """
    if not isinstance(payload, dict):
        return None, ""
    if not isinstance(payload.get("ok"), bool):
        code = payload.get("errcode")
        if isinstance(code, int) and not isinstance(code, bool):
            return (True, "") if code == 0 else (False, f"errcode {code} {payload.get('errmsg') or ''}")
        return None, ""
    if payload["ok"]:
        return True, ""
    err = payload.get("error")
    if isinstance(err, dict):
        # code 必须进分类文本：飞书的业务错误码（如 3380003 文档已删除）是
        # 最权威的判据，message 文案可能随版本变化。
        parts = [
            str(err.get(k, ""))
            for k in ("type", "subtype", "code", "message", "hint")
            if err.get(k)
        ]
        return False, " ".join(parts)
    return False, str(err or "")


async def _default_spawn(
    argv: Sequence[str], *, env: dict[str, str], cwd: str | None
) -> tuple[int, bytes, bytes]:
    """真实起进程。**唯一**调用 create_subprocess_exec 的地方。"""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,  # 绝不让子进程等在 stdin 上
        env=env,
        cwd=cwd,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        raise
    return proc.returncode or 0, stdout, stderr


class CliExecutor:
    """CLI 执行器。一进程一个实例，由 ``container.py`` 装配。"""

    # 最小环境白名单。刻意不含 PATH 之外的任何东西——继承整个 os.environ
    # 会把别的业务线的凭据一起带进子进程。
    _BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")

    def __init__(
        self,
        *,
        workspace_id: str,
        binaries: dict[str, str],
        secrets: dict[str, str] | None = None,
        specs: Sequence[CliCommandSpec] = (),
        timeout_s: float = 30.0,
        max_output_bytes: int = 8 * 1024 * 1024,
        max_attempts: int = 3,
        cwd: str | None = None,
        audit: AuditSink | None = None,
        alert: AlertSink | None = None,
        spawn: SpawnFn | Callable[..., Awaitable[tuple[int, bytes, bytes]]] | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self._binaries = dict(binaries)
        # 仅当前业务线的一组凭据。进程级隔离（D-2）在这里体现。
        self._secrets = dict(secrets or {})
        self._specs = {s.key: s for s in specs}
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._max_attempts = max_attempts
        self._cwd = cwd
        self._audit: AuditSink = audit or _NullAudit()
        self._alert: AlertSink = alert or _NullAlert()
        self._spawn = spawn or _default_spawn
        # 退避睡眠是独立注入点：测试要验"重试了几次"，但不该为此真的睡 4 秒。
        # 与 spawn 分开，因为把两者混在一个 fake 里会让"是否重试"这件事无法单独断言。
        self._sleep = retry_sleep or asyncio.sleep

    # -- 登记查询 ---------------------------------------------------------

    def register(self, spec: CliCommandSpec) -> None:
        self._specs[spec.key] = spec

    def is_registered(self, key: str) -> bool:
        return key in self._specs

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def binary_available(self, key: str) -> bool:
        """探测可执行文件是否存在。用于端口装配时判定可用性。"""
        spec = self._specs.get(key)
        if spec is None:
            return False
        binary = self._binaries.get(spec.binary_key, spec.binary_key)
        return shutil.which(binary) is not None

    # -- 环境构造 ---------------------------------------------------------

    def _build_env(self, spec: CliCommandSpec) -> dict[str, str]:
        env = {
            k: os.environ[k] for k in self._BASE_ENV_KEYS if k in os.environ
        }
        env["TZ"] = "UTC"  # §9.4-6：时间一律 UTC，不受宿主时区影响
        # 只注入本命令声明需要、且当前 workspace 确实持有的那几个
        for name in spec.env_keys:
            value = self._secrets.get(name)
            if value:
                env[name] = value
        return env

    # -- 主入口 -----------------------------------------------------------

    async def run(
        self,
        key: str,
        args: BaseModel | dict[str, Any],
        *,
        parse_as: type[M] | None = None,
        trace_id: str | None = None,
        idem_key: str | None = None,
        cwd: str | None = None,
    ) -> CliResult[Any]:
        """执行一条已登记命令。

        :param key: 命令登记键，如 ``lark.docs.fetch``。未登记直接拒绝。
        :param args: 参数，会经 ``spec.args_model`` 校验。
        :param parse_as: 期望的输出 model；给了就用它校验 JSON，失败即
            ``OUTPUT_UNPARSABLE``（触发契约测试，而不是重试）。
        :param cwd: 本次调用的工作目录，覆盖构造时的默认值。``git`` 必须在仓库
            内执行，而仓库根目录是按 workspace 配置的，不属于 executor 级常量。
            必须是已存在的目录——不存在时直接拒绝，而不是让子进程在一个意外的
            目录里执行（那会让 ``git rev-parse`` 返回**别的仓库**的 sha）。
        """
        spec = self._specs.get(key)
        if spec is None:
            # 白名单外的命令连"被拒绝"的路径都不走业务层——直接是编程错误。
            logger.error(
                "未登记命令被调用 key=%s workspace_id=%s（删除类命令刻意不登记）",
                key,
                self.workspace_id,
            )
            await self._alert.security(
                "cli.unregistered_command", key=key, workspace_id=self.workspace_id
            )
            return CliResult.failure(
                CliErrorCode.INVALID_ARGUMENT,
                command_key=key,
                stderr_digest=f"命令未登记：{key}",
            )

        # 参数校验：schema 不过就不起进程
        try:
            model = (
                args
                if isinstance(args, BaseModel)
                else spec.args_model.model_validate(args)
            )
        except ValidationError as exc:
            return CliResult.failure(
                CliErrorCode.INVALID_ARGUMENT,
                command_key=key,
                stderr_digest=digest(str(exc)),
            )

        binary = self._binaries.get(spec.binary_key, spec.binary_key)
        argv = [
            binary,
            *spec.argv_prefix,
            *_argv_from_model(model, spec.positional_fields, spec.arg_style),
        ]
        args_digest = digest(" ".join(argv[1:]))
        timeout_s = spec.timeout_s or self._timeout_s
        max_bytes = spec.max_output_bytes or self._max_output_bytes
        max_attempts = spec.max_attempts or self._max_attempts
        env = self._build_env(spec)

        workdir = cwd or self._cwd
        if workdir is not None and not os.path.isdir(workdir):
            logger.error("工作目录不存在，拒绝执行 key=%s cwd=%s", key, workdir)
            return CliResult.failure(
                CliErrorCode.BINARY_MISSING,
                command_key=key,
                stderr_digest=digest(f"工作目录不存在：{workdir}"),
            )

        result: CliResult[Any] = CliResult.failure(
            CliErrorCode.UNKNOWN, command_key=key
        )
        for attempt in range(1, max_attempts + 1):
            result = await self._attempt(
                spec,
                argv,
                env=env,
                timeout_s=timeout_s,
                max_bytes=max_bytes,
                parse_as=parse_as,
                attempt=attempt,
                cwd=workdir,
            )

            await self._audit.tool_call(
                workspace_id=self.workspace_id,
                trace_id=trace_id,
                idem_key=idem_key,
                risk=spec.risk.value,
                args_digest=args_digest,
                **result.audit_fields(),
            )

            if result.ok:
                return result

            # 鉴权/权限类：安全事件，**不重试**（疑似越权不能靠重试掩盖）
            if is_security_event(result.error_code):
                await self._alert.security(
                    "cli.auth_failure",
                    key=key,
                    workspace_id=self.workspace_id,
                    error_code=result.error_code.value,
                    detail=result.stderr_digest,
                )
                return result

            if not is_retryable(result.error_code) or attempt >= max_attempts:
                if action_for(result.error_code) is CliAction.CONTRACT_TEST:
                    await self._alert.warn(
                        "cli.output_unparsable",
                        key=key,
                        detail=result.stderr_digest or result.stdout_digest,
                    )
                return result

            # 指数退避 + 抖动：同一批任务失败时不要整批同时重试
            delay = min(2.0 ** (attempt - 1), 8.0) * (0.5 + random.random())
            logger.info(
                "cli retry key=%s attempt=%d/%d error=%s sleep=%.2fs",
                key,
                attempt,
                max_attempts,
                result.error_code.value,
                delay,
            )
            await self._sleep(delay)

        return result

    # -- 单次尝试 ---------------------------------------------------------

    async def _attempt(
        self,
        spec: CliCommandSpec,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        timeout_s: float,
        max_bytes: int,
        parse_as: type[M] | None,
        attempt: int,
        cwd: str | None = None,
    ) -> CliResult[Any]:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            # 本机无 timeout/gtimeout，超时一律靠 asyncio.wait_for
            code, out, err = await asyncio.wait_for(
                self._spawn(argv, env=env, cwd=cwd), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return CliResult.failure(
                CliErrorCode.TIMEOUT,
                retryable=True,
                command_key=spec.key,
                stderr_digest=f"超过 {timeout_s}s 未返回",
                elapsed_ms=elapsed(),
                attempt=attempt,
            )
        except FileNotFoundError:
            # CLI 没装。与鉴权失败严格区分：这个触发实现切换，不是安全告警。
            return CliResult.failure(
                CliErrorCode.BINARY_MISSING,
                command_key=spec.key,
                stderr_digest=f"可执行文件不存在：{argv[0]}",
                elapsed_ms=elapsed(),
                attempt=attempt,
            )
        except PermissionError as exc:
            return CliResult.failure(
                CliErrorCode.BINARY_MISSING,
                command_key=spec.key,
                stderr_digest=digest(str(exc)),
                elapsed_ms=elapsed(),
                attempt=attempt,
            )

        if len(out) > max_bytes:
            # 先判大小再解析——不把超大输出读进 json.loads
            return CliResult.failure(
                CliErrorCode.OUTPUT_TOO_LARGE,
                command_key=spec.key,
                stderr_digest=f"输出 {len(out)} 字节 > 上限 {max_bytes}",
                elapsed_ms=elapsed(),
                exit_code=code,
                attempt=attempt,
            )

        stdout_text = out.decode("utf-8", errors="replace")
        stderr_text = err.decode("utf-8", errors="replace")
        common = {
            "command_key": spec.key,
            "stdout_digest": digest(stdout_text),
            "stderr_digest": digest(stderr_text),
            "elapsed_ms": elapsed(),
            "exit_code": code,
            "attempt": attempt,
        }

        # ------------------------------------------------------------------
        # 错误判定。**stdout 永远不是错误判据。**
        #
        # stdout 里装的是被读取的文档正文、代码片段、TAPD 需求描述——按设计
        # 约定全是**不可信数据**。拿正则去扫它，等于让任何一篇写着「冲突」
        # 「rate limit」「401 unauthorized」的文档凭正文内容伪造错误码：轻则把
        # 成功的读取判成失败，重则触发假的安全告警。这不是理论风险，是接真实
        # 文档时当场踩到的（一个标题含「冲突」的章节被判成 CONFLICT）。
        #
        # 权威信号只有三个：CLI 自报的信封 ``ok``、退出码、stderr。
        # ------------------------------------------------------------------
        payload: Any = None
        parse_error: str | None = None
        if spec.json_output:
            try:
                payload = json.loads(stdout_text) if stdout_text.strip() else {}
            except json.JSONDecodeError as exc:
                parse_error = f"JSON 解析失败：{exc}"

        envelope_ok, envelope_err = _envelope_status(payload)
        exit_ok = code in spec.ok_exit_codes

        if envelope_ok is False:
            # CLI 明确自报失败：只归一它自己给的 error 对象，不碰正文
            error_code = classify(envelope_err, "", exit_code=code)
            if error_code is CliErrorCode.OK:
                error_code = CliErrorCode.UNKNOWN
            # 企微的 help_message 是给用户的授权指引，须完整带回，摘要会截断它
            status = {k: payload[k] for k in _WECOM_STATUS_KEYS if k in payload}
            return CliResult.failure(
                error_code,
                retryable=is_retryable(error_code),
                data=status or None,
                **{**common, "stderr_digest": digest(envelope_err)},
            )
        if envelope_ok is None and not exit_ok:
            # 无信封且退出码异常：认 stderr。stderr 为空时才退而扫 stdout，
            # 且已经确定命令失败，不存在"正常输出被误判"的可能。
            error_code = classify(
                stderr_text, stdout_text if not stderr_text.strip() else "",
                exit_code=code,
            )
            if error_code is CliErrorCode.OK:
                error_code = CliErrorCode.UNKNOWN
            return CliResult.failure(
                error_code, retryable=is_retryable(error_code), **common
            )

        if parse_error is not None:
            # CLI 版本变化的典型症状。不重试——告警并触发契约测试。
            return CliResult.failure(
                CliErrorCode.OUTPUT_UNPARSABLE,
                **{**common, "stderr_digest": digest(parse_error)},
            )

        if not spec.json_output:
            return CliResult.success(stdout_text, **common)

        if parse_as is None:
            return CliResult.success(payload, **common)

        try:
            return CliResult.success(parse_as.model_validate(payload), **common)
        except ValidationError as exc:
            # 只回灌字段路径与消息，**不回灌原文**（防注入放大）
            locs = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            return CliResult.failure(
                CliErrorCode.OUTPUT_UNPARSABLE,
                **{**common, "stderr_digest": digest(f"schema 不符：{locs}")},
            )


__all__ = [
    "AlertSink",
    "AuditSink",
    "CliCommandSpec",
    "CliExecutor",
    "RiskLevel",
    "SpawnFn",
]
