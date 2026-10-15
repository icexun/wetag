"""CLI 错误归一与处置（``app/platform/cli/error_map.py``）。

这张表是所有外部命令失败后的唯一决策点，而三个分支的代价各不相同：

* 归一错 ⇒ 重试策略错。把 429 当 5xx 无所谓（都退避重试），但把**鉴权失败**
  当平台抖动就会拿着一个坏 token 连打三轮，每轮都被记成一次失败登录——真正的
  越权尝试从此淹没在噪音里。反向同样糟：把 5xx 当鉴权失败会让一次网关抖动
  停掉整条链路并发出安全告警。
* 归一不出来也不能当成功。``exit_code != 0`` 必须落到 ``UNKNOWN``，否则一次
  未知失败会带着空数据往下走，最终表现为"没有变化"。

所以本文件的断言分三层：单条 stderr 的归一（含顺序陷阱与中文分支）、
stderr/stdout/退出码的兜底顺序、以及错误码 → 处置动作的不变式。
最后一层最重要：它守的是"哪些错误**绝不**重试"。
"""

from __future__ import annotations

import pytest

from app.platform.cli.error_map import (
    RETRYABLE,
    SECURITY_CODES,
    CliAction,
    action_for,
    classify,
    is_retryable,
    is_security_event,
)
from app.platform.cli.result import CliErrorCode as C

# ---------------------------------------------------------------------------
# 一、真实报错文本的归一
#
# 每条都取自三个 CLI 实际可能吐出的形态。用真实文本而不是"含 401 的字符串"，
# 才能发现"我们的正则只认自己造的那句话"这类问题。
# ---------------------------------------------------------------------------

CASES: list[tuple[str, C]] = [
    # 二进制不存在——必须优先于 NOT_FOUND，见下方专门用例
    ("/bin/sh: 1: lark-cli: command not found", C.BINARY_MISSING),
    ('exec: "tapd-cli": executable file not found in $PATH', C.BINARY_MISSING),
    ("fork/exec /usr/local/bin/wecom-cli: no such file or directory", C.BINARY_MISSING),
    # 配额
    ("Error: quota exceeded for app cli_9f2", C.QUOTA_EXCEEDED),
    ("错误：本月配额已用尽", C.QUOTA_EXCEEDED),
    ("api quota remaining: 0", C.QUOTA_EXCEEDED),
    # 限流
    ("HTTP 429 upstream error: too many requests", C.RATE_LIMITED),
    ("服务端返回 限流，请稍后重试", C.RATE_LIMITED),
    ("rate-limit hit, retry after 3s", C.RATE_LIMITED),
    # 鉴权
    ("HTTP 401 unauthorized: invalid_token", C.AUTH_FAILED),
    ("not logged in, run `lark-cli auth login` first", C.AUTH_FAILED),
    ("tenant access token expired", C.AUTH_FAILED),
    ("未登录", C.AUTH_FAILED),
    # 权限
    ("HTTP 403 forbidden: access to this doc is not granted", C.PERMISSION_DENIED),
    # 缺 scope 时飞书只说缺哪个 scope，不带 403——这是今日 R3 最可能收到的那句
    ("required scope is missing: drive:drive.metadata:readonly", C.PERMISSION_DENIED),
    ('"subtype": "missing_scope", "message": "missing required scope(s): task:task:read"',
     C.PERMISSION_DENIED),
    ("unauthorized: user authorization does not cover the required scope(s): approval:task:read",
     C.PERMISSION_DENIED),
    ("权限不足：无法访问该文档", C.PERMISSION_DENIED),
    ("access denied", C.PERMISSION_DENIED),
    # 不存在
    ("HTTP 404 not_found: document has been deleted", C.NOT_FOUND),
    ("文档不存在或已删除", C.NOT_FOUND),
    ("the requested node does not exist", C.NOT_FOUND),
    # 乐观锁
    ("HTTP 409 conflict: revision_id mismatch", C.CONFLICT),
    ("版本冲突，请重新拉取后再提交", C.CONFLICT),
    ("revision has changed since last read", C.CONFLICT),
    # 参数
    ("HTTP 400 invalid_param: doc_token", C.INVALID_ARGUMENT),
    ("参数错误：--scope 只能是 full/outline/section", C.INVALID_ARGUMENT),
    # 上游
    ("HTTP 500 internal server error", C.UPSTREAM_5XX),
    ("HTTP 503 service unavailable", C.UPSTREAM_5XX),
    ("502 bad gateway", C.UPSTREAM_5XX),
    # 只给状态码、不给任何可读描述的形态：5xx 必须按三位数匹配而不是枚举 500/502
    ("upstream returned HTTP 504", C.UPSTREAM_5XX),
    # 超时
    ("context deadline exceeded", C.TIMEOUT),
    ("操作超时", C.TIMEOUT),
    ("request timed out after 30s", C.TIMEOUT),
]


@pytest.mark.parametrize("stderr,expected", CASES, ids=[c[1].value + ":" + c[0][:20] for c in CASES])
def test_real_cli_messages_are_normalized(stderr: str, expected: C) -> None:
    assert classify(stderr, exit_code=1) is expected


def test_exceeded_not_just_exceed() -> None:
    """``\\bexceed\\b`` 匹配不上 ``exceeded``——而 CLI 吐的全是过去式。

    这个词尾差异会让配额耗尽被降级成 ``UNKNOWN``：动作从"切降级路径"变成
    "告警不重试"，于是每次配额用光都变成一次人工介入而不是自动降级。
    """
    for text in ("quota exceeded", "quota exhausted", "exceeded the quota", "配额超限"):
        assert classify(text, exit_code=1) is C.QUOTA_EXCEEDED, text


def test_chinese_messages_do_not_need_word_boundaries() -> None:
    """``\\b`` 对 CJK 无效：中日韩字符本身是 word char，两侧都是 word char 时
    ``\\b`` 不成立，加了反而永不匹配。国内 CLI 的报错大半是中文，这条失效等于
    整张表对一半输入形同不存在。
    """
    zh = {
        "配额不足": C.QUOTA_EXCEEDED,
        "频率限制": C.RATE_LIMITED,
        "认证失败": C.AUTH_FAILED,
        "无权限": C.PERMISSION_DENIED,
        "已删除": C.NOT_FOUND,
        "乐观锁失败": C.CONFLICT,
        "参数无效": C.INVALID_ARGUMENT,
        "服务不可用": C.UPSTREAM_5XX,
        "超时": C.TIMEOUT,
    }
    for text, expected in zh.items():
        assert classify(text, exit_code=1) is expected, text


# ---------------------------------------------------------------------------
# 二、顺序陷阱
#
# 归一表是"首个命中即返回"，所以规则顺序本身就是行为。这几条钉住的都是
# 真实出现过的复合文本。
# ---------------------------------------------------------------------------


def test_a_429_carried_inside_an_upstream_error_is_still_rate_limiting() -> None:
    """``HTTP 429 upstream error`` 同时命中 429 与"上游错误"两种直觉。

    判成 5xx 也会退避重试，看似无害——但 5xx 与 429 的退避节奏该不同，而且
    真正的问题是这说明 5xx 规则排在了更具体的规则前面，下一个受害者就是
    "500 unauthorized" 这种把鉴权失败吃掉的组合。
    """
    assert classify("HTTP 429 upstream error", exit_code=1) is C.RATE_LIMITED


def test_a_quota_message_wrapped_in_a_500_is_still_quota() -> None:
    """配额规则必须在 5xx 之前：配额耗尽要走降级，5xx 要走重试。

    判错方向是重试——对着一个已经打光的配额继续退避重试，既不会成功，
    又会把降级路径（仅用文档更新时间判定进行中）永远推迟到重试耗尽之后。
    """
    assert classify("HTTP 500: quota exhausted", exit_code=1) is C.QUOTA_EXCEEDED


def test_a_missing_binary_is_not_a_missing_document() -> None:
    """``no such file or directory`` 同时长得像 404。

    分错的后果是动作完全相反：``BINARY_MISSING`` 是端口不可用，
    ``NOT_FOUND`` 要回群里澄清"这个文档还在吗"——
    而真相是我们连命令都没装。
    """
    assert classify("fork/exec /usr/bin/tapd-cli: no such file or directory") is C.BINARY_MISSING
    assert action_for(classify("exec: tapd-cli: command not found")) is CliAction.SWITCH_IMPL


def test_tapd_cli_api_errors() -> None:
    """tapd-cli 把 API 错误打成 ``API 错误 422: {...}``：token 失效归鉴权，其余归参数错。"""
    assert classify('API 错误 422: {"info": "The access token provided is invalid"}') is C.AUTH_FAILED
    assert classify('API 错误 422: {"info": "workspace_id is invalid"}') is C.INVALID_ARGUMENT


def test_a_document_not_found_is_not_a_missing_binary() -> None:
    """反向：正文里的 "file not found" 不该被当成命令缺失。"""
    assert classify("HTTP 404: file not found in wiki space", exit_code=1) is C.NOT_FOUND


# ---------------------------------------------------------------------------
# 三、输入来源与兜底
# ---------------------------------------------------------------------------


def test_stderr_takes_precedence_over_stdout() -> None:
    """两处都有可识别文本时以 stderr 为准——它是这次失败的直接原因，
    stdout 里可能还残留着上一条成功记录的内容。
    """
    assert classify("HTTP 429 too many requests", "HTTP 403 forbidden", 1) is C.RATE_LIMITED


def test_a_business_error_printed_on_stdout_is_still_found() -> None:
    """有些 CLI 把业务错误打进 stdout 的 JSON，stderr 是空的。

    只看 stderr 会让这类失败落到 ``UNKNOWN`` ——动作从"告警不重试"看着一样，
    但告警内容会丢掉"是权限问题"这个唯一有用的信息，也不会进安全事件通道。
    """
    code = classify("", '{"ok":false,"error":{"type":"forbidden","message":"无权限"}}', 1)
    assert code is C.PERMISSION_DENIED
    assert is_security_event(code) is True


def test_an_unrecognized_failure_is_unknown_not_ok() -> None:
    """认不出来 + 退出码非 0 ⇒ UNKNOWN。

    这是整张表最重要的一条兜底。若返回 ``OK``，一次未知失败会带着空数据继续
    往下走，最终表现为"没有变化"——一个假阴性，而且没有任何
    地方留下"其实失败了"的痕迹。
    """
    assert classify("《╯-╰》 unparseable gibberish", exit_code=1) is C.UNKNOWN
    assert classify("", "", 1) is C.UNKNOWN
    assert classify("", "", 137) is C.UNKNOWN


def test_exit_zero_with_noise_is_ok() -> None:
    """退出码 0 且没有可识别错误 ⇒ 成功。CLI 往 stderr 打进度是常态，
    不能因为 stderr 非空就判失败。
    """
    assert classify("fetching 3/3 ...", exit_code=0) is C.OK
    assert classify("warning: deprecated flag", exit_code=None) is C.OK


def test_a_recognizable_error_wins_over_a_zero_exit_code() -> None:
    """退出码 0 但输出里明写着 401——以文本为准。

    有 CLI 在业务失败时仍返回 0（错误写在 JSON 里）。信退出码就会把一次鉴权
    失败当成功，然后拿着空数据继续。
    """
    assert classify("HTTP 401 unauthorized", exit_code=0) is C.AUTH_FAILED


# ---------------------------------------------------------------------------
# 四、处置动作的不变式
#
# 这一节不测"某个码映射到某个动作"这种可以随口改的表格值，而测**不变式**：
# 哪些错误绝不重试、安全事件与重试互斥、新增错误码不会静默落到重试。
# ---------------------------------------------------------------------------


def test_only_transient_platform_faults_are_retryable() -> None:
    """可重试集合是白名单，且恰好是三个平台抖动类。

    逐一列出而非"不在黑名单即可重试"：白名单加错一个成员的代价是对着一个
    确定性失败重试到超时，而黑名单漏一个的代价一样——但白名单的漏法是
    "不重试"，是安全的那一侧。
    """
    assert RETRYABLE == frozenset({C.TIMEOUT, C.RATE_LIMITED, C.UPSTREAM_5XX})


@pytest.mark.parametrize(
    "code",
    [C.AUTH_FAILED, C.PERMISSION_DENIED, C.QUOTA_EXCEEDED, C.INVALID_ARGUMENT,
     C.NOT_FOUND, C.BINARY_MISSING, C.OUTPUT_UNPARSABLE, C.OUTPUT_TOO_LARGE,
     C.CONFLICT, C.UNKNOWN],
)
def test_deterministic_failures_are_never_retried(code: C) -> None:
    """确定性失败重试只会放大问题：同样的 token、同样的参数、同样的结果。

    ``CONFLICT`` 也在此列——它确实要重试，但必须**先重算 diff**，那是
    ``workflows/doc_update.py`` 的事。执行器层面盲目重试会把同一份过期内容
    再写一次，覆盖掉别人刚保存的修改。
    """
    assert is_retryable(code) is False


def test_a_security_event_is_never_retried_and_always_alerts() -> None:
    """鉴权/权限类 = 疑似越权安全事件。两条纪律必须同时成立。

    静默重试会把一次配置错误变成一串失败登录记录，让真正的越权尝试淹没在
    噪音里；不告警则是反过来——越权尝试悄无声息。
    """
    assert SECURITY_CODES == frozenset({C.AUTH_FAILED, C.PERMISSION_DENIED})
    for code in SECURITY_CODES:
        assert is_retryable(code) is False
        assert action_for(code) is CliAction.ALERT_NO_RETRY


def test_retryable_and_security_are_disjoint() -> None:
    """不变式：任何一个码不能既是安全事件又可重试。

    比上一条更强——它对将来新增的码同样成立。
    """
    assert not (RETRYABLE & SECURITY_CODES)
    for code in C:
        assert not (is_retryable(code) and is_security_event(code)), code


def test_quota_degrades_instead_of_retrying() -> None:
    """配额打光 ⇒ 降级（仅用文档更新时间判定"进行中"），不是重试也不是告警。

    这是"部分可用优于整体不可用"在错误表里的落点：配额耗尽时索引仍可基于
    文档更新时间继续工作，检测覆盖范围变窄但不归零。
    """
    assert action_for(C.QUOTA_EXCEEDED) is CliAction.DEGRADE
    assert is_retryable(C.QUOTA_EXCEEDED) is False


def test_a_conflict_recomputes_once_rather_than_retrying_blindly() -> None:
    assert action_for(C.CONFLICT) is CliAction.RECOMPUTE_RETRY_ONCE


def test_unparsable_output_triggers_a_contract_test_not_a_retry() -> None:
    """输出解析不了几乎总是 CLI 升级改了输出格式。

    重试无用（下一次还是同样的格式），告警也不够——要跑契约测试定位是哪个
    字段变了。这是唯一一个"动作是让人去改代码"的错误码。
    """
    assert action_for(C.OUTPUT_UNPARSABLE) is CliAction.CONTRACT_TEST
    assert is_retryable(C.OUTPUT_UNPARSABLE) is False


def test_a_missing_binary_switches_implementation_rather_than_alerting() -> None:
    """``tapd-cli`` 今天就不存在——这是已知的降级路径，不是需要值班响应的告警。"""
    assert action_for(C.BINARY_MISSING) is CliAction.SWITCH_IMPL


def test_every_error_code_has_an_explicit_action() -> None:
    """新增一个错误码时必须同时决定它的处置动作。

    ``action_for`` 的兜底是 ``ALERT_NO_RETRY``（安全的一侧），所以漏登记不会
    造成事故——但会造成一次本可自动降级的失败变成人工介入。这条断言让漏登记
    在测试阶段就红。
    """
    unmapped = [c for c in C if action_for(c) is CliAction.ALERT_NO_RETRY]
    # 真正该落到"告警不重试"的只有这三个
    assert set(unmapped) == {C.AUTH_FAILED, C.PERMISSION_DENIED, C.OUTPUT_TOO_LARGE, C.UNKNOWN}


def test_an_unregistered_code_falls_back_to_the_safe_side() -> None:
    """将来新增一个错误码、忘了登记动作时，兜底必须是"告警不重试"。

    兜底若是重试，一个未知语义的失败会先被重试三轮再报出来：故障被延迟，
    而且如果它其实是确定性失败（比如又一种鉴权形态），三轮全会失败。
    """
    assert action_for("A_CODE_ADDED_TOMORROW") is CliAction.ALERT_NO_RETRY  # type: ignore[arg-type]
    assert is_retryable("A_CODE_ADDED_TOMORROW") is False  # type: ignore[arg-type]


def test_the_ok_code_never_reaches_the_action_table_in_practice() -> None:
    """占位项存在只为字典全覆盖。真有人把 OK 送进来，说明调用方没判 ok——
    这里返回什么都是错的，所以只钉住"它有个值、不抛异常"。
    """
    assert action_for(C.OK) in set(CliAction)
