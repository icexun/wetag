"""回归测试：**stdout 永远不是错误判据**。

这不是假想的威胁模型，是接真实飞书文档时当场踩到的：一篇技术方案里有一节讲
冲突处理，正文里 conflict 一词密集出现，于是一次 ``ok: true`` 的成功读取被
``classify()`` 判成了 ``CONFLICT``——整节内容直接丢失。

按设计约定，群消息、文档正文、代码注释全是**不可信数据**。如果错误归一去扫
stdout，那么：

* 一篇写着「rate limit」的文档 → 假 RATE_LIMITED → 无意义退避重试；
* 一段含「401 unauthorized」的代码 → 假 AUTH_FAILED → **伪造安全告警**；
* 一篇讲冲突处理的文档 → 假 CONFLICT → 内容被丢 → 变更被当成没有变化。

最后一条最危险：它让「失败语义诚实」这条底线被正文内容单方面绕过。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.platform.cli.executor import CliCommandSpec, CliExecutor, RiskLevel
from app.platform.cli.result import CliErrorCode


class _Args(BaseModel):
    doc: str


def _executor(stdout: str, *, exit_code: int = 0, stderr: str = "", json_output: bool = True,
              ok_exit_codes: tuple[int, ...] = (0,)) -> CliExecutor:
    async def spawn(argv, *, env, cwd):
        return exit_code, stdout.encode(), stderr.encode()

    spec = CliCommandSpec(
        key="probe",
        binary_key="fake",
        argv_prefix=("run",),
        args_model=_Args,
        risk=RiskLevel.READ,
        json_output=json_output,
        ok_exit_codes=ok_exit_codes,
    )
    return CliExecutor(
        workspace_id="alpha",
        binaries={"fake": "/bin/true"},
        specs=[spec],
        spawn=spawn,
        max_attempts=1,
    )


def _envelope(content: str) -> str:
    return json.dumps({"ok": True, "data": {"document": {"content": content}}})


# 每一条都是「文档正文里合法出现的字样」，历史上都会被误判
@pytest.mark.parametrize(
    "content,would_be",
    [
        ("<h3>6.3 冲突检测</h3><p>conflict detection 的总流程</p>", "CONFLICT"),
        ("<p>接口返回 429 rate limit exceeded 时按指数退避</p>", "RATE_LIMITED"),
        ("<p>若响应 401 unauthorized 则提示用户重新登录</p>", "AUTH_FAILED"),
        ("<p>403 permission denied 属于不可重试错误</p>", "PERMISSION_DENIED"),
        ("<p>upstream 502 bad gateway 计入 5xx 熔断</p>", "UPSTREAM_5XX"),
        ("<p>配额已用尽（quota exceeded）时降级</p>", "QUOTA_EXCEEDED"),
        ("<p>资源 404 not found 视为已删除</p>", "NOT_FOUND"),
        ("<p>请求超时 timeout 后重试一次</p>", "TIMEOUT"),
    ],
)
async def test_成功信封不受正文内容影响(content: str, would_be: str) -> None:
    """``ok: true`` 是权威信号，正文里写什么都不能把它翻成失败。"""
    result = await _executor(_envelope(content)).run("probe", {"doc": "D1"})
    assert result.ok is True, f"正文含「{would_be}」字样导致成功被误判"
    assert result.error_code is CliErrorCode.OK
    assert result.data["data"]["document"]["content"] == content


async def test_信封自报失败才归一错误码() -> None:
    """``ok: false`` 时只读 CLI 自己的 error 对象。"""
    payload = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "auth",
                "subtype": "token_expired",
                "message": "401 unauthorized",
            },
        }
    )
    result = await _executor(payload).run("probe", {"doc": "D1"})
    assert result.ok is False
    assert result.error_code is CliErrorCode.AUTH_FAILED
    assert result.retryable is False  # 安全类不重试


async def test_信封自报文档已删除归为NOT_FOUND() -> None:
    """飞书删除文档的实测信封（code 3380003）必须归一成 NOT_FOUND。

    归一成 UNKNOWN 的后果：refresh 把「文档已删除」当瞬时故障，走
    「连续 3 次才归档」的慢路径——而永久性失败会让 stale 计数永远 >0，
    全部检测永远「未完成」。error.code 是权威判据，必须进分类文本。
    """
    payload = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "api",
                "subtype": "unknown",
                "code": 3380003,
                "message": "Document page has been deleted. This page can no "
                "longer be edited; choose another page or create a new document",
            },
        }
    )
    result = await _executor(payload).run("probe", {"doc": "D1"})
    assert result.ok is False
    assert result.error_code is CliErrorCode.NOT_FOUND
    assert result.retryable is False  # 永久性失败不重试


async def test_信封失败但正文另有字样时不被正文带偏() -> None:
    """error 对象说超时，data 里写着 conflict——以 error 对象为准。"""
    payload = json.dumps(
        {
            "ok": False,
            "error": {"type": "network", "message": "request timeout"},
            "data": {"note": "409 conflict 409 conflict"},
        }
    )
    result = await _executor(payload).run("probe", {"doc": "D1"})
    assert result.error_code is CliErrorCode.TIMEOUT


async def test_非json输出也不扫stdout() -> None:
    """``rg --json`` 的命中行里可能就有 401/conflict 字样。"""
    hits = "\n".join(
        json.dumps({"type": "match", "data": {"lines": {"text": line}}})
        for line in ["if code == 401: raise Unauthorized", "# 409 conflict 时重算"]
    )
    result = await _executor(hits, json_output=False).run("probe", {"doc": "D1"})
    assert result.ok is True
    assert result.data == hits


async def test_rg无命中退出码1是空结果而非故障() -> None:
    result = await _executor(
        "", exit_code=1, json_output=False, ok_exit_codes=(0, 1)
    ).run("probe", {"doc": "D1"})
    assert result.ok is True
    assert result.data == ""


async def test_退出码异常时以stderr为准() -> None:
    result = await _executor(
        _envelope("<p>一切正常</p>"),
        exit_code=2,
        stderr="429 Too Many Requests",
        json_output=False,
    ).run("probe", {"doc": "D1"})
    assert result.ok is False
    assert result.error_code is CliErrorCode.RATE_LIMITED
    assert result.retryable is True


async def test_退出码异常且stderr为空才退而扫stdout() -> None:
    """已确定命令失败，此时读 stdout 不会误伤正常输出。"""
    result = await _executor(
        "fatal: authentication failed", exit_code=128, json_output=False
    ).run("probe", {"doc": "D1"})
    assert result.ok is False
    assert result.error_code is CliErrorCode.AUTH_FAILED


async def test_无信封且退出码异常时不会判成OK() -> None:
    """兜底：识别不出具体错因也必须是失败，不能静默成功。"""
    result = await _executor(
        "", exit_code=3, stderr="something entirely unfamiliar", json_output=False
    ).run("probe", {"doc": "D1"})
    assert result.ok is False
    assert result.error_code is CliErrorCode.UNKNOWN
