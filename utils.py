#!/usr/bin/env python3
"""Utilities for AgentScope agent creation, model config, and tool management."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from config.properties import AgentProperties, ModelProperties, ToolProperties

from agentrun.integration.agentscope.message_adapter import AgentScopeMessageAdapter
from agentrun.integration.utils.canonical import (
    CanonicalMessage,
    CanonicalToolCall,
    MessageRole as CanonicalMessageRole,
)
from agentrun.server.model import AgentEvent, EventType, Message as AgentRunMessage
from agentrun.server.model import MessageRole as AgentRunMessageRole
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.mcp import HttpStatelessClient as AgentScopeHttpStatelessClient
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import OpenAIChatModel
from agentscope.pipeline import stream_printing_messages
from agentscope.tool import Toolkit

try:
    from agentrun.utils.log import logger
except ImportError:
    logger = logging.getLogger(__name__)

_shared_toolkit: Optional[Toolkit] = None
_mcp_clients_lifo: List[Any] = []
_tools_loaded = False
_api_tool_count = 0
_mcp_server_count = 0
_thread_memories: Dict[str, InMemoryMemory] = {}

_GENERATE_KWARGS_KEYS = frozenset({
    "temperature", "top_p", "max_tokens",
    "frequency_penalty", "presence_penalty", "seed",
})
_LEGACY_PARAM_KEYS = frozenset({
    "streaming", "stream_usage", "timeout",
    "max_retries", "default_headers", "default_query",
})


def agentrun_message_to_canonical(msg: AgentRunMessage) -> CanonicalMessage:
    content = msg.content
    if content is None:
        text = None
    elif isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False)

    role_map = {
        AgentRunMessageRole.SYSTEM: CanonicalMessageRole.SYSTEM,
        AgentRunMessageRole.USER: CanonicalMessageRole.USER,
        AgentRunMessageRole.ASSISTANT: CanonicalMessageRole.ASSISTANT,
        AgentRunMessageRole.TOOL: CanonicalMessageRole.TOOL,
    }
    canonical_role = role_map.get(msg.role, CanonicalMessageRole.USER)

    if msg.role == AgentRunMessageRole.TOOL:
        return CanonicalMessage(
            role=canonical_role, content=text,
            tool_call_id=msg.tool_call_id or "", name=msg.name,
        )

    tool_calls = None
    if msg.tool_calls:
        tool_calls = []
        for tc in msg.tool_calls:
            fn = tc.function if hasattr(tc, "function") else tc.get("function", {})
            if not isinstance(fn, dict):
                fn = {}
            name = fn.get("name", "") or ""
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {}
            tid = tc.id if hasattr(tc, "id") else tc.get("id", "")
            tool_calls.append(
                CanonicalToolCall(id=str(tid), name=str(name), arguments=args or {})
            )

    return CanonicalMessage(
        role=canonical_role, content=text, name=msg.name, tool_calls=tool_calls,
    )


def convert_agentrun_messages_to_agentscope(messages: List[Any]) -> List[Msg]:
    adapter = AgentScopeMessageAdapter()
    canonical = []
    for m in messages:
        if not isinstance(m, AgentRunMessage):
            m = AgentRunMessage.model_validate(m)
        canonical.append(agentrun_message_to_canonical(m))
    result = adapter.from_canonical(canonical)
    return result if isinstance(result, list) else [result]


def _format_tool_result_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(output)


async def iter_agent_scope_agent_events(
    agent: ReActAgent, msgs: List[Msg],
) -> AsyncIterator[AgentEvent]:
    prev_text_by_id: Dict[str, str] = {}
    emitted_tool_call_keys: set = set()

    async for item in stream_printing_messages([agent], agent.reply(msgs)):
        msg, is_last = item[0], item[1]
        mid = msg.id

        full_text = msg.get_text_content() or ""
        prev = prev_text_by_id.get(mid, "")
        if full_text.startswith(prev):
            delta = full_text[len(prev):]
            if delta:
                yield AgentEvent(event=EventType.TEXT, data={"delta": delta})
            prev_text_by_id[mid] = full_text
        else:
            if full_text:
                yield AgentEvent(event=EventType.TEXT, data={"delta": full_text})
            prev_text_by_id[mid] = full_text

        if is_last:
            for block in msg.get_content_blocks("tool_use"):
                key = (mid, block.get("id"))
                if key in emitted_tool_call_keys:
                    continue
                emitted_tool_call_keys.add(key)
                args = block.get("input", {}) or {}
                args_str = (
                    json.dumps(args, ensure_ascii=False)
                    if isinstance(args, dict) else str(args)
                )
                yield AgentEvent(
                    event=EventType.TOOL_CALL,
                    data={"id": block.get("id", ""), "name": block.get("name", ""), "args": args_str},
                )

        if is_last:
            for block in msg.get_content_blocks("tool_result"):
                out = _format_tool_result_output(block.get("output"))
                yield AgentEvent(
                    event=EventType.TOOL_RESULT,
                    data={"id": block.get("id", ""), "result": out},
                )


def _parse_mcp_configs(raw_json: str) -> List[Tuple[str, dict]]:
    """Parse a JSON string into a list of (server_name, config) tuples.

    Accepts Cursor-style format::

        { "mcpServers": { "name": { "transport": "...", "url": "..." } } }

    Also accepts legacy list/object formats as fallback.
    """
    results: List[Tuple[str, dict]] = []
    try:
        cfg = _expand_mcp_env(json.loads(raw_json))
    except json.JSONDecodeError as e:
        logger.error("Error parsing MCP config: %s", e)
        return results
    if isinstance(cfg, dict) and "mcpServers" in cfg:
        servers = cfg["mcpServers"]
        if isinstance(servers, dict):
            for name, server_cfg in servers.items():
                if isinstance(server_cfg, dict):
                    results.append((name, server_cfg))
    elif isinstance(cfg, list):
        for i, item in enumerate(cfg):
            if isinstance(item, dict):
                results.append((f"mcp_server_{i}", item))
    elif isinstance(cfg, dict):
        results.append(("mcp_server_0", cfg))
    return results


def _expand_mcp_env(value: Any) -> Any:
    """Expand ${env:NAME} placeholders without committing secrets to mcp.json."""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise ValueError(f"MCP environment variable is not set: {name}")
            return resolved

        return re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if isinstance(value, list):
        return [_expand_mcp_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_mcp_env(item) for key, item in value.items()}
    return value


def _load_mcp_json_file() -> str | None:
    """Try to load mcp.json from the project directory."""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_json_path = os.path.join(project_dir, "mcp.json")
    if os.path.isfile(mcp_json_path):
        try:
            with open(mcp_json_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                logger.info("Loaded MCP config from %s", mcp_json_path)
                return content
        except Exception as e:
            logger.warning("Failed to read %s: %s", mcp_json_path, e)
    return None


async def _register_mcp_servers_on_toolkit(
    toolkit: Toolkit,
) -> Tuple[int, List[Any]]:
    clients: List[Any] = []
    # MCP credentials may be carried in query parameters. Avoid logging URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    mcp_file_content = _load_mcp_json_file()
    tool_props = ToolProperties.instance()
    mcp_tools_str = tool_props.mcp_tools
    sandbox_mcp_tools_str = tool_props.sandbox_mcp_tools

    if not mcp_file_content and not mcp_tools_str and not sandbox_mcp_tools_str:
        return 0, clients

    init_timeout = tool_props.mcp_init_timeout
    named_configs: List[Tuple[str, dict]] = []

    if mcp_file_content:
        named_configs.extend(_parse_mcp_configs(mcp_file_content))

    if not named_configs:
        for raw in (mcp_tools_str, sandbox_mcp_tools_str):
            if raw:
                named_configs.extend(_parse_mcp_configs(raw))

    if not named_configs:
        return 0, clients

    _TRANSPORT_ALIASES = {
        "streamable-http": "streamable_http",
        "streamableHttp": "streamable_http",
        "streamable-Http": "streamable_http",
    }

    mcp_registered = 0
    for server_name, server_cfg in named_configs:
        if not isinstance(server_cfg, dict):
            logger.warning("Skipping MCP server config '%s': not a dict", server_name)
            continue
        transport = server_cfg.get("transport") or server_cfg.get("type")
        if not transport:
            logger.warning(
                "Skipping MCP server config '%s': missing 'transport' or 'type' field", server_name
            )
            continue
        transport = _TRANSPORT_ALIASES.get(transport, transport)
        url = server_cfg.get("url")
        headers = server_cfg.get("headers")
        timeout = float(server_cfg.get("timeout", 30))
        sse_read_timeout = float(server_cfg.get("sse_read_timeout", 60 * 5))
        try:
            client = AgentScopeHttpStatelessClient(
                name=server_name,
                transport=transport,
                url=url,
                headers=headers,
                timeout=timeout,
                sse_read_timeout=sse_read_timeout,
            )
            await asyncio.wait_for(
                toolkit.register_mcp_client(client),
                timeout=init_timeout,
            )
            mcp_registered += 1
            logger.info("Registered MCP client %s", server_name)
        except asyncio.TimeoutError:
            logger.error("Timeout registering MCP client %s", server_name)
        except Exception as e:
            logger.error("Error registering MCP client %s: %s", server_name, e, exc_info=True)
    return mcp_registered, clients


def active_integration_tools(
    tools_by_integration: Dict[str, Any], *, which: Callable[[str], Optional[str]] | None = None
) -> Dict[str, Any]:
    """只保留已启用（``INTEGRATIONS``）且 CLI 在 PATH 上的集成的工具（或 skill）。"""
    import shutil

    from app.contracts.ports import parse_integrations
    from config.properties import CliProperties, WorkspaceProperties

    which = which or shutil.which
    enabled = parse_integrations(WorkspaceProperties.instance().integrations)
    clip = CliProperties.instance()
    binaries = {"lark": clip.lark_bin, "tapd": clip.tapd_bin, "wecom": clip.wecom_bin}
    active: Dict[str, Any] = {}
    for name, tools in tools_by_integration.items():
        if name not in enabled:
            logger.info("集成 %s 未启用，不注册其工具", name)
        elif not which(str(binaries[name])):
            logger.warning("集成 %s 的 CLI（%s）不在 PATH 上，不注册其工具", name, binaries[name])
        else:
            active[name] = tools
    return active


async def _build_shared_toolkit_async() -> None:
    global _shared_toolkit, _mcp_clients_lifo, _api_tool_count, _mcp_server_count
    toolkit = Toolkit()
    mcp_count, clients = await _register_mcp_servers_on_toolkit(toolkit)
    # 飞书 / TAPD / 企微工具走各自 CLI（CliExecutor 白名单收口），不走 MCP
    from app.capability.agents.lark_office_tools import AGENT_LARK_OFFICE_TOOLS
    from app.capability.agents.lark_tools import AGENT_LARK_TOOLS
    from app.capability.agents.subscription_tools import AGENT_SUBSCRIPTION_TOOLS
    from app.capability.agents.tapd_tools import AGENT_TAPD_TOOLS
    from app.capability.agents.wecom_office_tools import AGENT_WECOM_OFFICE_TOOLS
    from app.capability.agents.wecom_tools import AGENT_WECOM_TOOLS

    local_tools: list[Callable[..., Any]] = list(AGENT_SUBSCRIPTION_TOOLS)
    for name, tools in active_integration_tools(
        {
            "lark": (*AGENT_LARK_TOOLS, *AGENT_LARK_OFFICE_TOOLS),
            "tapd": AGENT_TAPD_TOOLS,
            "wecom": (*AGENT_WECOM_TOOLS, *AGENT_WECOM_OFFICE_TOOLS),
        }
    ).items():
        local_tools.extend(tools)
    for fn in local_tools:
        toolkit.register_tool_function(fn)
    _shared_toolkit = toolkit
    _mcp_clients_lifo = clients
    _api_tool_count = 0
    _mcp_server_count = mcp_count
    logger.info(
        "Toolkit ready: %d MCP servers, %d local tools",
        mcp_count,
        len(local_tools),
    )


async def _close_mcp_clients_async() -> None:
    for client in reversed(_mcp_clients_lifo):
        try:
            if getattr(client, "is_connected", False):
                await client.close()
        except Exception as e:
            logger.warning("Error closing MCP client: %s", e)
    _mcp_clients_lifo.clear()


def _run_coro_sync(coro, timeout: float = 120) -> None:
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            fut = executor.submit(asyncio.run, coro)
            fut.result(timeout=timeout)
    except RuntimeError:
        asyncio.run(coro)


def ensure_tools_loaded() -> None:
    global _tools_loaded
    if _tools_loaded:
        return
    _run_coro_sync(_build_shared_toolkit_async())
    _tools_loaded = True


def reset_tools_cache() -> None:
    global _shared_toolkit, _tools_loaded
    global _api_tool_count, _mcp_server_count
    if _mcp_clients_lifo:
        try:
            _run_coro_sync(_close_mcp_clients_async())
        except Exception as e:
            logger.warning("MCP cleanup during reset: %s", e)
    _shared_toolkit = None
    _tools_loaded = False
    _api_tool_count = 0
    _mcp_server_count = 0
    _thread_memories.clear()
    logger.info("Tools cache reset")


def get_shared_toolkit() -> Toolkit:
    ensure_tools_loaded()
    assert _shared_toolkit is not None
    return _shared_toolkit


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


def render_persona(template: str, lookup: Callable[[str], Any]) -> str:
    """展开 ``{{KEY}}``；含未配置占位符的行整行省略。"""
    lines = []
    for line in template.splitlines():
        keys = _PLACEHOLDER.findall(line)
        values = {k: lookup(k) for k in keys}
        if any(v in (None, "") for v in values.values()):
            continue
        lines.append(_PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), line))
    return "\n".join(lines).strip()


def load_persona() -> str:
    """人设模板（``PERSONA_FILE``，默认 ``config/persona.md``）：身份、项目事实、工作约定。"""
    import config

    props = AgentProperties.instance()
    path = props.persona_file
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            template = f.read()
    except OSError as e:
        logger.warning("Failed to read persona file %s: %s", path, e)
        return ""
    return render_persona(
        template, lambda k: props.bot_name if k == "BOT_NAME" else config.get(k)
    )


def load_skills(skills_dir: str = None) -> str:
    if skills_dir is None:
        default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
        skills_dir = AgentProperties.instance().skills_dir or default_dir
    if not os.path.isdir(skills_dir):
        return ""
    paths = sorted(glob.glob(os.path.join(skills_dir, "**", "SKILL.md"), recursive=True))
    if not paths:
        return ""
    # 集成专属 skill 按目录名前缀归属；集成不可用时其工具未注册，skill 也不进提示词
    owner = {"feishu-": "lark", "tapd-": "tapd", "wecom-": "wecom"}
    grouped: Dict[str, List[str]] = {}
    for path in paths:
        skill = os.path.basename(os.path.dirname(path))
        key = next((v for k, v in owner.items() if skill.startswith(k)), "")
        grouped.setdefault(key, []).append(path)
    active = active_integration_tools({k: v for k, v in grouped.items() if k})
    paths = sorted([*grouped.get("", []), *(p for ps in active.values() for p in ps)])
    sections = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                sections.append(f.read().strip())
        except Exception as e:
            logger.warning("Failed to read skill file %s: %s", path, e)
    if not sections:
        return ""
    content = "\n\n---\n\n".join(sections)
    logger.info("Loaded %d skill file(s) from %s", len(sections), skills_dir)
    return content


def expand_prompt_from_vars(prompt_template: str, vars_dict: Dict[str, Any]) -> str:
    if not prompt_template:
        return prompt_template
    vars_dict = vars_dict or {}
    matches = re.findall(r"\{([^}]+)\}", prompt_template)
    if not matches:
        return prompt_template
    expanded = prompt_template
    for var_name in matches:
        if var_name in vars_dict:
            expanded = expanded.replace(f"{{{var_name}}}", str(vars_dict[var_name]))
        else:
            logger.warning("Template variable not found: {%s}", var_name)
    return expanded


def format_model_error(error_str: str) -> str:
    error_lower = error_str.lower()
    if "connection" in error_lower and ("refused" in error_lower or "failed" in error_lower):
        return f"Connection failed: check BASE_URL. Original: {error_str}"
    if "timeout" in error_lower:
        return f"Timeout: check network or server status. Original: {error_str}"
    if "unauthorized" in error_lower or "401" in error_str:
        return f"Auth failed: check API_KEY. Original: {error_str}"
    if "not found" in error_lower or "404" in error_str:
        return f"Not found: check BASE_URL and MODEL. Original: {error_str}"
    return f"API call failed: {error_str}"


def create_model(
    model_parameter_rules: Optional[str] = None,
    custom_headers: Optional[str] = None,
    custom_query: Optional[str] = None,
    stream: bool = True,
) -> OpenAIChatModel:
    model_props = ModelProperties.instance()
    agent_props = AgentProperties.instance()

    model = model_props.name
    tool_call_timeout = agent_props.tool_call_timeout

    generate_kwargs: Dict[str, Any] = {
        "temperature": model_props.temperature,
    }
    model_parameter_rules_str = model_parameter_rules or model_props.parameter_rules
    if model_parameter_rules_str:
        try:
            rules = json.loads(model_parameter_rules_str)
            if isinstance(rules, dict):
                for key, value in rules.items():
                    if key in _GENERATE_KWARGS_KEYS:
                        generate_kwargs[key] = value
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse MODEL_PARAMETER_RULES: %s", e)

    key = model_props.api_key or "default"
    resolved_base = model_props.base_url

    client_kwargs: Dict[str, Any] = {"timeout": tool_call_timeout}
    if resolved_base:
        client_kwargs["base_url"] = resolved_base

    custom_headers_str = custom_headers or model_props.custom_headers
    if custom_headers_str:
        try:
            hobj = json.loads(custom_headers_str)
            if isinstance(hobj, dict):
                client_kwargs["default_headers"] = hobj
        except json.JSONDecodeError:
            pass

    custom_query_str = custom_query or model_props.custom_query
    if custom_query_str:
        try:
            qobj = json.loads(custom_query_str)
            if isinstance(qobj, dict):
                client_kwargs["default_query"] = qobj
        except json.JSONDecodeError:
            pass

    return OpenAIChatModel(
        model_name=model,
        api_key=key,
        stream=stream,
        client_kwargs=client_kwargs,
        generate_kwargs=generate_kwargs,
        stream_tool_parsing=True,
    )


# 工作记忆工厂：bootstrap 注入 PG 实现后，线程记忆落库、重启不丢；
# 未注入（测试/脚本场景）回退进程内 InMemoryMemory
_working_memory_factory: Optional[Callable[[str], Any]] = None


def bind_working_memory_factory(factory: Optional[Callable[[str], Any]]) -> None:
    global _working_memory_factory
    _working_memory_factory = factory
    if factory is not None:
        _thread_memories.clear()


def _memory_for_thread(thread_id: str) -> Any:
    if _working_memory_factory is not None:
        return _working_memory_factory(thread_id)
    mem = _thread_memories.get(thread_id)
    if mem is None:
        mem = InMemoryMemory()
        _thread_memories[thread_id] = mem
    return mem


def create_agent(
    system_prompt: Optional[str] = None,
    model_parameter_rules: Optional[str] = None,
    custom_headers: Optional[str] = None,
    custom_query: Optional[str] = None,
    stream: Optional[bool] = None,
    thread_id: Optional[str] = None,
) -> ReActAgent:
    ensure_tools_loaded()
    if stream is None:
        # 缺省跟网关能力走（MODEL_STREAM）：部分网关不支持 stream=true
        stream = bool(ModelProperties.instance().stream)
    toolkit = get_shared_toolkit()
    model = create_model(
        model_parameter_rules,
        custom_headers,
        custom_query,
        stream=stream,
    )

    # 身份层始终在最前：人设模板优先，缺省回退 PROMPT 配置；
    # 调用方传入的（如 Rules 装配的议题规则）紧随其后，而非互相覆盖
    base_prompt = load_persona() or AgentProperties.instance().prompt
    if system_prompt is None:
        system_prompt = base_prompt
    elif base_prompt and system_prompt != base_prompt:
        system_prompt = f"{base_prompt}\n\n{system_prompt}"

    skills_content = load_skills()
    if skills_content:
        system_prompt = f"{system_prompt}\n\n{skills_content}"

    memory = _memory_for_thread(thread_id) if thread_id else InMemoryMemory()
    agent_props = AgentProperties.instance()
    max_iters = agent_props.max_iters
    max_iters = max(5, min(150, max_iters))

    agent = ReActAgent(
        name="agent",
        sys_prompt=system_prompt,
        model=model,
        formatter=OpenAIChatFormatter(),
        toolkit=toolkit,
        memory=memory,
        max_iters=max_iters,
    )
    logger.info("ReActAgent created (max_iters=%s)", max_iters)
    return agent
