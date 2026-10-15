"""Auto Mode 自动批准规则匹配（US7-4 / US7-10）。

规则作用于整条业务线：JSON 列表，每项 {"tool": "doc.update" | "doc.*",
"params": {"doc_id": "^doc-.*"}}；tool 支持 glob，params 值按正则 fullmatch
匹配 str(参数值)。规则无法解析时一律按「未命中」处理并告警（US7-10），
不 fail-open。
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.platform.observability.alerting import alert, AlertKind


@dataclass(frozen=True)
class AutoApproveDecision:
    """一次自动批准匹配的结果。hit=False 即进入确认流程。"""

    hit: bool
    rule_version: str | None = None  # 命中规则的版本（US7-4 审计标注）
    rule_index: int | None = None
    error: str | None = None  # 规则不可解析时的原因（US7-10）


class AutoModeMatcher:
    """业务线级自动批准规则匹配器。规则在构造时解析一次。"""

    def __init__(
        self,
        rules: Any = None,
        *,
        rule_version: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._rule_version = rule_version
        self._workspace_id = workspace_id
        self._alerted = False
        self._rules, self._error = self._parse(rules)

    @staticmethod
    def _parse(rules: Any) -> tuple[list[dict[str, Any]], str | None]:
        if rules is None or rules == "":
            return [], None
        if isinstance(rules, str):
            try:
                rules = json.loads(rules)
            except (ValueError, TypeError) as exc:
                return [], f"自动批准规则不是合法 JSON: {exc}"
        if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
            return [], "自动批准规则必须是 JSON 对象列表"
        return list(rules), None

    @property
    def error(self) -> str | None:
        return self._error

    def match(
        self, tool_name: str, tool_input: Mapping[str, Any] | None = None
    ) -> AutoApproveDecision:
        """匹配工具名与参数模式；规则不可解析时按未命中处理并告警（US7-10）。"""
        if self._error is not None:
            if not self._alerted:
                self._alerted = True
                alert(
                    AlertKind.FAILURE,
                    "自动批准规则无法解析，按未命中处理",
                    self._error,
                    workspace_id=self._workspace_id,
                )
            return AutoApproveDecision(hit=False, error=self._error)
        for idx, rule in enumerate(self._rules):
            if self._rule_matches(rule, tool_name, tool_input or {}):
                return AutoApproveDecision(
                    hit=True, rule_version=self._rule_version, rule_index=idx
                )
        return AutoApproveDecision(hit=False)

    @staticmethod
    def _rule_matches(
        rule: dict[str, Any], tool_name: str, tool_input: Mapping[str, Any]
    ) -> bool:
        tool_pattern = rule.get("tool")
        if not tool_pattern or not fnmatch.fnmatchcase(tool_name, str(tool_pattern)):
            return False
        for key, pattern in (rule.get("params") or {}).items():
            value = tool_input.get(key)
            if value is None:
                return False
            try:
                if not re.fullmatch(str(pattern), str(value)):
                    return False
            except re.error:
                return False  # 单条参数模式非法按不命中处理，不放行
        return True
