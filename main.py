#!/usr/bin/env python3
"""
AgentScope Agent wrapped with AgentRun SDK.

Exposes OpenAI-compatible and AG-UI HTTP endpoints with streaming support.
"""

import json
import sys
import traceback
import uuid
from typing import AsyncIterator

import config
from config.properties import AgentProperties, ModelProperties, ServerProperties
from agentrun.server import AgentRequest, AgentRunServer
from agentrun.server.model import AgentEvent
from agentrun.utils.log import logger

from app.platform.observability.entry_span import entry_span

from utils import (
    convert_agentrun_messages_to_agentscope,
    create_agent,
    ensure_tools_loaded,
    expand_prompt_from_vars,
    format_model_error,
    get_shared_toolkit,
    iter_agent_scope_agent_events,
    reset_tools_cache,
)


def initialize_agent():
    """Initialize tools by loading them (called once at startup)."""
    try:
        reset_tools_cache()
        try:
            ensure_tools_loaded()
            toolkit = get_shared_toolkit()
            total = len(toolkit.tools)
            logger.info("Tools loaded: %s registered tool function(s)", total)
        except Exception as tool_error:
            logger.error(
                "Failed to load tools - Type: %s, Message: %s",
                type(tool_error).__name__,
                str(tool_error),
                exc_info=True,
            )
            logger.warning(
                "Failed to load tools, continuing without tools"
            )
            total = 0
        logger.info("Tools initialized successfully (with %s tools)", total)
        return True
    except Exception as e:
        logger.error("Failed to initialize: %s", str(e), exc_info=True)
        return False


_SESSION_HEADERS = ("x-session-id", "x-agentrun-session-id", "session-id")


def _session_id(raw_headers: dict) -> str:
    lowered = {k.lower(): v for k, v in raw_headers.items()}
    for key in _SESSION_HEADERS:
        if lowered.get(key):
            return lowered[key]
    return uuid.uuid4().hex


async def invoke_agent(request: AgentRequest) -> AsyncIterator[str | AgentEvent]:
    """
    AgentRun SDK agent invocation function.
    Yields str (non-streaming) or AgentEvent (streaming).
    """
    raw_headers = {}
    if hasattr(request, "raw_request") and request.raw_request:
        raw_headers = dict(request.raw_request.headers) or {}
    thread_id = _session_id(raw_headers)
    async with entry_span(thread_id, name="wetag:debug-agent"):
        async for item in _invoke(request, raw_headers, thread_id):
            yield item


async def _invoke(
    request: AgentRequest, raw_headers: dict, thread_id: str
) -> AsyncIterator[str | AgentEvent]:
    try:
        model_props = ModelProperties.instance()

        model_parameter_rules_str = (
            raw_headers.get("X-Model-Parameter-Rules")
            or raw_headers.get("x-model-parameter-rules")
            or model_props.parameter_rules
        )

        # Extract custom headers (X-APIG-* prefix)
        custom_headers_dict = {}
        for header_key, header_value in raw_headers.items():
            if header_key.lower().startswith("x-apig-"):
                real_header_key = header_key[7:]
                if real_header_key:
                    custom_headers_dict[real_header_key] = header_value

        custom_headers = None
        if custom_headers_dict:
            custom_headers = json.dumps(custom_headers_dict)
        if not custom_headers:
            custom_headers = model_props.custom_headers

        # Extract custom query params (X-APIG-* prefix)
        custom_query_dict = {}
        raw_query_params = {}
        if hasattr(request, "raw_request") and request.raw_request:
            raw_query_params = (
                dict(request.raw_request.query_params)
                if hasattr(request.raw_request, "query_params")
                else {}
            )
        for query_key, query_value in raw_query_params.items():
            if query_key.lower().startswith("x-apig-"):
                real_query_key = query_key[7:]
                if real_query_key:
                    custom_query_dict[real_query_key] = query_value

        custom_query = None
        if custom_query_dict:
            custom_query = json.dumps(custom_query_dict)
        if not custom_query:
            custom_query = model_props.custom_query

        # System prompt with variable expansion
        agent_props = AgentProperties.instance()
        system_prompt = agent_props.prompt
        system_prompt_vars = {}
        prompt_variables_str = agent_props.prompt_variables
        if prompt_variables_str:
            try:
                parsed_data = json.loads(prompt_variables_str)
                if isinstance(parsed_data, dict):
                    system_prompt_vars = parsed_data
                elif isinstance(parsed_data, list):
                    system_prompt_vars = {}
                    for item in parsed_data:
                        if (
                            isinstance(item, dict)
                            and "variable_key" in item
                            and "variable_value" in item
                        ):
                            system_prompt_vars[item["variable_key"]] = item[
                                "variable_value"
                            ]
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to parse PROMPT_VARIABLES: %s", e)

        # Merge request-level prompt vars
        request_prompt_vars = {}
        if hasattr(request, "raw_request") and request.raw_request:
            try:
                request_body = await request.raw_request.json()
                if isinstance(request_body, dict):
                    forwarded_props = request_body.get("forwardedProps", {})
                    if isinstance(forwarded_props, dict):
                        request_prompt_vars = forwarded_props.get(
                            "system_prompt_vars", {}
                        )
                        if not isinstance(request_prompt_vars, dict):
                            request_prompt_vars = {}
            except Exception as e:
                logger.debug("Failed to get system_prompt_vars from request: %s", e)

        if request_prompt_vars:
            system_prompt_vars.update(request_prompt_vars)
        if system_prompt_vars:
            system_prompt = expand_prompt_from_vars(system_prompt, system_prompt_vars)

        logger.info("[Main] Using thread_id: %s", thread_id)

        stream = getattr(request, "stream", False)

        current_agent = create_agent(
            system_prompt=system_prompt,
            model_parameter_rules=model_parameter_rules_str,
            custom_headers=custom_headers,
            custom_query=custom_query,
            stream=stream,
            thread_id=thread_id,
        )

        messages = getattr(request, "messages", [])
        agentscope_messages = convert_agentrun_messages_to_agentscope(messages)

        if stream:
            async for ev in iter_agent_scope_agent_events(
                current_agent, agentscope_messages
            ):
                yield ev
        else:
            result_msg = await current_agent.reply(agentscope_messages)
            text = result_msg.get_text_content() if result_msg else ""
            if text:
                yield text

    except Exception as e:
        traceback.print_exc()
        logger.error("invoke agent error: %s", e)
        yield format_model_error(str(e))


def main():
    """Main entry point using AgentRun SDK."""
    logger.info("Starting AgentScope Agent with AgentRun SDK ...")
    config.init()

    model_props = ModelProperties.instance()
    logger.info(
        "Model config loaded — model=%s, base_url=%s, api_key=%s",
        model_props.name,
        model_props.base_url,
        "***" if model_props.api_key else "(not set)",
    )
    if not model_props.api_key:
        logger.warning("API_KEY not set; agent may fail on first request.")

    if not initialize_agent():
        logger.warning("Tool initialization failed, but continuing...")

    server_props = ServerProperties.instance()
    port = server_props.port
    host = server_props.host

    logger.info("Server will run on %s:%s", host, port)
    logger.info("Endpoints:")
    logger.info("  POST http://%s:%s/openai/v1/chat/completions", host, port)
    logger.info("  POST http://%s:%s/ag-ui/agent", host, port)

    try:
        memory_collection_name = server_props.memory_collection_name
        server = AgentRunServer(
            invoke_agent=invoke_agent,
            memory_collection_name=memory_collection_name,
        )
        server.start(port=port, host=host)
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
    except Exception as e:
        logger.error("Failed to start AgentRunServer: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
