"""结构化输出客户端——**所有语义判断的唯一入口**。

刻意**不走 ``ReActAgent`` 工具循环**。语义节点要的是"给定这段文本，
按这个 schema 判一次"，不是让模型自己决定调哪个工具、循环几轮。走工具循环会
带来三个不想要的东西：不可控的轮数、不可复现的调用顺序、以及把外部写操作暴露
给模型自主决策。Workflow 的顺序由代码固定，模型只负责判断。

三条降级路径，按能力从强到弱，首次失败即 **latch**（记住网关不支持，后续不再
重试那条路，省掉每次调用的固定浪费）：

1. ``native``——AgentScope 的 ``structured_model=``，内部走 ``response_format``
   或 tool_call，由它处理 provider 差异；
2. ``response_format``——直接下 ``json_schema``，绕过 AgentScope 的包装；
3. ``prompt_json``——把 schema 写进提示词要求纯 JSON 输出。最弱但几乎总能用。

**失败一律 ``ok=False``。** 这是整个模块最重要的一行语义：上层必须把它转成
``DetectionOutcome.completed=False`` 并给出 ``incomplete_reason``。绝不允许
"模型没答上来 ⇒ 没抽到 ⇒ 回复没有"——那正是 G2 要防的假阴性，而且它比
漏报更糟：它是**带着确定语气的漏报**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# 模型爱把 JSON 包在 ```json fence 里
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

_MODES = ("native", "response_format", "prompt_json")


@dataclass(slots=True)
class StructuredResult(Generic[M]):
    """一次结构化调用的结果。

    ``ok=False`` 时 ``data is None``，``reason`` 说明失败原因。调用方**必须**
    检查 ``ok``——照抄 ``result.data.xxx`` 会得到 ``AttributeError`` 而不是
    一个看起来像"没有"的空对象，这是刻意的。
    """

    ok: bool
    data: M | None = None
    reason: str = ""
    mode: str = ""
    attempts: int = 1
    elapsed_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def unwrap(self) -> M:
        if not self.ok or self.data is None:
            raise RuntimeError(f"结构化输出失败：{self.reason}")
        return self.data


class StructuredAgent:
    """薄结构化输出客户端。一进程一个实例，由 ``container.py`` 装配。

    ``semaphore`` 全进程共享（D-2：一业务线一进程），与文档回源并发分开计量——
    两者打的是不同的外部配额，混在一个信号量里会互相饿死。
    """

    def __init__(
        self,
        model: Any,
        *,
        semaphore: asyncio.Semaphore | None = None,
        max_attempts: int = 3,
        timeout_s: float = 90.0,
        mode: str | None = None,
    ) -> None:
        self._model = model
        self._sem = semaphore or asyncio.Semaphore(4)
        self._max_attempts = max(1, max_attempts)
        self._timeout_s = timeout_s
        # None = 尚未探明；一旦探明就 latch，不再每次重试不支持的路径
        self._mode: str | None = mode
        self._disabled: set[str] = set()

    @property
    def mode(self) -> str | None:
        return self._mode

    # -- 主入口 -----------------------------------------------------------
    async def complete(
        self,
        schema: type[M],
        *,
        system: str,
        user: str,
        trace_id: str | None = None,
        max_attempts: int | None = None,
    ) -> StructuredResult[M]:
        """按 ``schema`` 判一次。

        校验失败时**只把字段路径与错误消息回灌**给模型，绝不回灌它上一次的原始
        输出。回灌原文会把模型自己产出的内容（可能已被输入文本带偏）再次送进
        上下文，等于给注入做放大器；而 ``loc + msg`` 足够让它改对字段。
        """
        import time

        started = time.monotonic()
        budget = max_attempts or self._max_attempts
        hint = ""
        last = "未执行"
        # 同一次请求里，某种协议形态已经跑满超时时间后换下一条路；继续用完全
        # 相同的 kwargs 重试，只会把一次故障拖成 max_attempts × timeout。
        # 不写入 _disabled：超时不等于网关永久不支持，下一次请求仍可重新探测。
        timed_out_modes: set[str] = set()
        # ``attempt`` 只统计**真正让模型判断过**的次数；latch 探路不计（见下）。
        attempt = 0
        while attempt < budget:
            mode = self._pick_mode(skip=timed_out_modes)
            if mode is None:
                return StructuredResult(
                    ok=False,
                    reason=last
                    if timed_out_modes
                    else "三条结构化输出路径全部不可用",
                    attempts=max(attempt, 1),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            attempt += 1
            try:
                async with self._sem:
                    raw, usage = await asyncio.wait_for(
                        self._call(mode, schema, system, user + hint),
                        timeout=self._timeout_s,
                    )
            except asyncio.TimeoutError:
                last = f"模型超时 {self._timeout_s}s"
                timed_out_modes.add(mode)
                logger.warning(
                    "structured 超时 trace=%s mode=%s attempt=%d/%d timeout=%.1fs，切换协议模式",
                    trace_id,
                    mode,
                    attempt,
                    budget,
                    self._timeout_s,
                )
                continue
            except _ModeUnsupported as exc:
                # 网关不支持这条路。latch 掉，立刻换下一条，**不计入重试预算**。
                #
                # "探路失败"和"模型答错了"是两件事，记在同一个预算里会出事：一个
                # 只支持 prompt_json 的网关（R6 里最可能的形态）要先撞掉 native
                # 和 response_format 两条，预算就只剩一次。于是 ``max_attempts=2``
                # 时第一次调用直接判失败，``=3`` 时也再经不起一次 schema 校验重试。
                # 而上层会把这个失败转成 ``completed=False``——一个完全健康的网关
                # 表现成"检测未完成"。
                #
                # 回退 ``attempt`` 不会死循环：``_pick_mode`` 只返回不在
                # ``_disabled`` 里的模式，每次 latch 都让 ``_disabled`` 严格变大，
                # 最多三次就全满，下一轮 ``_pick_mode`` 返回 None 直接返回。
                self._disabled.add(mode)
                self._mode = None
                logger.warning("structured 模式不可用，latch 掉 mode=%s：%s", mode, exc)
                last = f"{mode} 不可用：{exc}"
                attempt -= 1
                continue
            except Exception as exc:  # 网关抖动、连接重置
                last = f"{type(exc).__name__}: {exc}"
                logger.warning("structured 调用失败 trace=%s：%s", trace_id, last)
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
                continue

            try:
                data = _coerce(schema, raw)
            except ValidationError as exc:
                # 只回灌 loc + msg，**不回灌原文**（防注入放大）
                detail = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors()[:6]
                )
                hint = (
                    f"\n\n[上一次输出不符合要求，请修正这些字段后重新输出完整 JSON]\n{detail}"
                )
                last = f"schema 不符：{detail}"
                logger.info("structured 校验失败重试 trace=%s：%s", trace_id, detail)
                continue
            except ValueError as exc:
                hint = "\n\n[上一次输出不是合法 JSON，请只输出 JSON 本体，不要任何解释文字]"
                last = str(exc)
                continue

            self._mode = mode  # 这条路走通了，latch 住
            return StructuredResult(
                ok=True,
                data=data,
                mode=mode,
                attempts=attempt,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                usage=usage,
            )

        return StructuredResult(
            ok=False,
            reason=last,
            mode=self._mode or "",
            attempts=attempt,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # -- 模式选择 ---------------------------------------------------------
    def _pick_mode(self, *, skip: set[str] | None = None) -> str | None:
        skipped = skip or set()
        if (
            self._mode
            and self._mode not in self._disabled
            and self._mode not in skipped
        ):
            return self._mode
        for m in _MODES:
            if m not in self._disabled and m not in skipped:
                return m
        return None

    # -- 三条路 -----------------------------------------------------------
    async def _call(
        self, mode: str, schema: type[M], system: str, user: str
    ) -> tuple[Any, dict[str, Any]]:
        messages = _messages(system, user)
        if mode == "native":
            return await self._call_native(schema, messages)
        if mode == "response_format":
            return await self._call_response_format(schema, messages)
        return await self._call_prompt_json(schema, system, user)

    async def _call_native(
        self, schema: type[M], messages: list[dict[str, Any]]
    ) -> tuple[Any, dict[str, Any]]:
        try:
            resp = await self._model(messages, structured_model=schema)
        except TypeError as exc:
            raise _ModeUnsupported(f"模型对象不接受 structured_model：{exc}") from exc
        except Exception as exc:
            if _is_unsupported(exc):
                raise _ModeUnsupported(str(exc)[:200]) from exc
            raise
        meta = getattr(resp, "metadata", None)
        if meta:
            return meta, _usage(resp)
        # 声称支持却没给 metadata：当作不支持，换下一条路
        text = _text_of(resp)
        if text:
            return text, _usage(resp)
        raise _ModeUnsupported("structured_model 未返回 metadata")

    async def _call_response_format(
        self, schema: type[M], messages: list[dict[str, Any]]
    ) -> tuple[Any, dict[str, Any]]:
        fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": False,
                "schema": schema.model_json_schema(),
            },
        }
        try:
            resp = await self._model(messages, response_format=fmt)
        except TypeError as exc:
            raise _ModeUnsupported(f"不接受 response_format：{exc}") from exc
        except Exception as exc:
            if _is_unsupported(exc):
                raise _ModeUnsupported(str(exc)[:200]) from exc
            raise
        return _text_of(resp) or getattr(resp, "metadata", None), _usage(resp)

    async def _call_prompt_json(
        self, schema: type[M], system: str, user: str
    ) -> tuple[Any, dict[str, Any]]:
        """最弱兜底：schema 写进提示词。几乎任何网关都能用。"""
        spec = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys2 = (
            f"{system}\n\n"
            "你必须只输出一个 JSON 对象，不要输出任何解释、前言或 Markdown 代码块标记。\n"
            f"JSON 必须符合以下 JSON Schema：\n{spec}"
        )
        resp = await self._model(_messages(sys2, user))
        return _text_of(resp), _usage(resp)


class _ModeUnsupported(RuntimeError):
    """网关不支持这条结构化路径。触发 latch 换路，不计重试预算。"""


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

_UNSUPPORTED_HINTS = (
    "response_format",
    "json_schema",
    "not support",
    "unsupported",
    "unrecognized",
    "unknown parameter",
    "extra inputs are not permitted",
)

# 网关瞬时拒绝的签名：部分 MaaS 网关间歇性 400（code 400001，"rejected by an
# internal MaaS component"）。它的错误 type 是 OpenAI 通用的 invalid_request_error——
# 若仅凭 type 归类，一次抖动就会被当成"不支持"把最强的路永久 latch 掉（真实事故：
# 进程余生只剩最弱的 prompt_json，且每次调用白烧一次重试预算）。
_TRANSIENT_HINTS = (
    "internal maas component",
)


def _is_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(h in text for h in _TRANSIENT_HINTS):
        return False
    return any(h in text for h in _UNSUPPORTED_HINTS)


def _messages(system: str, user: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _text_of(resp: Any) -> str:
    """把 AgentScope 的 content 块拍平成文本。"""
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") in (None, "text") and blk.get("text"):
                    parts.append(str(blk["text"]))
            elif isinstance(blk, str):
                parts.append(blk)
            else:
                text = getattr(blk, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def _usage(resp: Any) -> dict[str, Any]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    # AgentScope 的 ChatUsage 是 dict 子类，但里面塞了 provider 的原始 usage
    # 对象。审计日志只要能 json 序列化的标量，别把嵌套对象带进去。
    out: dict[str, Any] = {}
    for k in ("input_tokens", "output_tokens", "total_tokens", "time"):
        v = u.get(k) if isinstance(u, dict) else getattr(u, k, None)
        if isinstance(v, (int, float)):
            out[k] = v
    return out


def _coerce(schema: type[M], raw: Any) -> M:
    """把模型返回的任意形态收敛成 schema 实例。

    :raises ValidationError: 结构不符——触发带 loc+msg 的定向重试。
    :raises ValueError: 压根不是 JSON——触发"只输出 JSON"的提示重试。
    """
    if isinstance(raw, schema):
        return raw
    if isinstance(raw, dict):
        return schema.model_validate(raw)
    if isinstance(raw, str):
        return schema.model_validate(_loads(raw))
    if raw is None:
        raise ValueError("模型返回空内容")
    return schema.model_validate(raw)


def _loads(text: str) -> Any:
    """容错 JSON 解析：剥 fence、截首尾括号。"""
    s = text.strip()
    if not s:
        raise ValueError("模型返回空文本")
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 前后有零碎解释文字时，取最外层的 {...}
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析为 JSON：{exc}") from exc
    raise ValueError("响应中找不到 JSON 对象")


__all__ = ["StructuredAgent", "StructuredResult"]
