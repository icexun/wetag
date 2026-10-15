"""ripgrep / git grep 检索 → ``CodeEvidence``（``app/capability/integration/repo/search.py``）。

这里是 G3（"每条结论都能跳到行号"）的源头。行号取自工具输出、commit 取自钉住的
快照，**两者都由代码填，模型碰不到**——所以模型无法编造一个能通过
``CODE_LOCATOR_RE`` 的定位。本文件盯的就是这条链不被悄悄弄断。

``test_repo_search_real.py`` 用真 ``rg`` 跑通同一条链，但它在缺 ``rg`` 的机器上会
跳过，而且真工具证明不了失败分支——恰恰是失败分支决定了"没查到"和"没去查"会不会
被混为一谈。这里用 Fake 执行器补上那一半。

三条主张：

1. **"没去查" ≠ "没查到"。** 所有子仓路径都不存在时必须抛 ``PortUnavailable``，
   而不是返回空列表——空列表会一路变成"代码里没有相关实现"。
2. **不可信的 `subdir` 不许逃出仓库根。** 它可能来自模型输出或需求字段。
3. **``rg`` 缺失是"换实现"的信号，不是"没有命中"的信号。** 切 git 后端，并且只切
   一次。
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.capability.integration.repo.search import RepoSearch
from app.contracts.errors import PortUnavailable
from app.contracts.ports import CODE_LOCATOR_RE
from app.contracts.ports import CodeQuery, RepoSnapshot, RepoTarget
from app.platform.cli.result import CliErrorCode, CliResult

SHA = "0123456789abcdef0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeCli:
    """按命令 key 回放。``calls`` 留下全部调用，断言后端切换与缓存都看它。"""

    def __init__(self, **by_key: Any) -> None:
        self._by_key = by_key
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def run(
        self, key: str, args: Any, *, cwd: str | None = None, **kw: Any
    ) -> CliResult[Any]:
        self.calls.append((key, dict(args), cwd))
        canned = self._by_key.get(key)
        if callable(canned):
            canned = canned(dict(args))
        if canned is None:
            return CliResult.failure(CliErrorCode.BINARY_MISSING)
        return canned

    def keys(self, key: str) -> list[dict[str, Any]]:
        return [a for k, a, _ in self.calls if k == key]


def _snapshot(root: str) -> RepoSnapshot:
    return RepoSnapshot(
        root=root,
        branch="master",
        commit_sha=SHA,
        synced_at=datetime.now(timezone.utc),
    )


def _query(root: str, *, patterns: tuple[str, ...] = ("creditLimit",), **kw: Any) -> CodeQuery:
    kw.setdefault("targets", ())
    kw.setdefault("context_lines", 1)
    return CodeQuery(snapshot=_snapshot(root), patterns=patterns, **kw)


def _rg_match(path: str, lineno: int, *, as_bytes: bool = False) -> str:
    node: dict[str, Any] = (
        {"bytes": base64.b64encode(path.encode()).decode()} if as_bytes else {"text": path}
    )
    return json.dumps({"type": "match", "data": {"path": node, "line_number": lineno}})


def _rg(*lines: str) -> CliResult[str]:
    return CliResult.success("\n".join(lines))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个小仓：两个子目录，若干行可被切上下文的文件。"""
    (tmp_path / "apps" / "ios").mkdir(parents=True)
    (tmp_path / "libs").mkdir()
    (tmp_path / "apps" / "ios" / "limit.py").write_text(
        "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    )
    (tmp_path / "libs" / "rule.py").write_text(
        "class LimitRule:\n    def check(self):\n        return 1\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 一、"没去查" ≠ "没查到"
# ---------------------------------------------------------------------------


async def test_no_existing_target_is_a_port_failure_not_an_empty_result(repo: Path) -> None:
    """所有子仓路径都不存在时抛 ``PortUnavailable``。

    返回空列表会一路变成"代码里没有相关实现"——一个配置错误被报成一条结论。区别必须
    在这一层就做出来，因为再往上就只剩"有几条证据"这个数字了。
    """
    cli = FakeCli()
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with pytest.raises(PortUnavailable) as e:
        await search.search(_query(str(repo), targets=(RepoTarget(repo="r", subdir="nope"),)))

    assert "未执行任何检索" in e.value.reason
    assert cli.calls == [], "路径都不存在却还是去 fork 了进程"


async def test_a_query_with_no_patterns_is_also_never_looked_at(repo: Path) -> None:
    """一个 pattern 都没有的查询同样是"没去查"。

    patterns 来自 ``code_hints`` 等字段，全为空是会发生的。此时返回空列表就是把
    "我没有可搜的词"报成"代码里没有这些词"。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with pytest.raises(PortUnavailable):
        await search.search(_query(str(repo), patterns=()))

    assert cli.calls == []


async def test_a_real_search_with_no_hit_is_an_empty_list(repo: Path) -> None:
    """真的搜了但没命中，那就是空列表——与上一条必须可区分。"""
    search = RepoSearch(FakeCli(**{"rg.search": _rg()}))  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_one_missing_target_does_not_sink_the_others(repo: Path) -> None:
    """一个子仓不存在只跳过它，其余照搜。

    一次最多给 3 个子仓，其中一个写错就整轮不搜，等于让一个错路径否掉两个对的。
    """
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 2))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(
        _query(
            str(repo),
            targets=(RepoTarget(repo="r", subdir="nope"), RepoTarget(repo="r", subdir="libs")),
        )
    )

    assert len(out) == 1
    assert len(cli.keys("rg.search")) == 1, "不存在的那个子仓也去搜了"


# ---------------------------------------------------------------------------
# 二、不可信的 subdir 不许逃出仓库根
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evil", ["../../etc", "..", "libs/../../..", "/etc"])
async def test_a_subdir_cannot_escape_the_repo_root(repo: Path, evil: str) -> None:
    """``subdir`` 可能来自模型输出或需求字段，即不可信数据。

    逃出去的话 rg 会去搜仓库外的文件，而结果会被当成"这个 commit 里的代码证据"
    ——locator 里的 ``@sha`` 指着一份压根不属于那个快照的内容。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with pytest.raises(PortUnavailable):
        await search.search(_query(str(repo), targets=(RepoTarget(repo="r", subdir=evil),)))

    assert cli.calls == [], f"{evil!r} 被放进了检索路径"


async def test_a_file_as_subdir_is_rejected(repo: Path) -> None:
    """指到文件（而非目录）的 subdir 也拒绝——rg 的搜索范围必须是目录。"""
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with pytest.raises(PortUnavailable):
        await search.search(
            _query(str(repo), targets=(RepoTarget(repo="r", subdir="apps/ios/limit.py"),))
        )


async def test_a_sibling_sharing_the_root_prefix_is_still_outside(tmp_path: Path) -> None:
    """``/repo-evil`` 不在 ``/repo`` 里面——前缀比对必须带上路径分隔符。

    少一个 ``os.sep`` 就是经典的前缀漏洞：任何与仓库同名开头的兄弟目录都成了"仓内"，
    而 ``subdir`` 是不可信数据，攻击者选名字不要钱。
    """
    (tmp_path / "repo" / "libs").mkdir(parents=True)
    (tmp_path / "repo-evil").mkdir()
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with pytest.raises(PortUnavailable):
        await search.search(
            _query(
                str(tmp_path / "repo"),
                targets=(RepoTarget(repo="r", subdir="../repo-evil"),),
            )
        )

    assert cli.calls == [], "搜到仓库外面去了"


async def test_a_symlinked_repo_root_does_not_reject_everything(tmp_path: Path) -> None:
    """仓库根本身是符号链接时，正常子仓仍然要能搜。

    只对候选路径做 realpath 而不对根做，两边就不在同一个命名空间里比——合法子仓会
    被判成"逃出仓库根"，整条链路退化成永远 ``PortUnavailable``。越界校验失效方向朝
    严也是失效：它会让代码维度永久性地什么都查不了。
    """
    (tmp_path / "real" / "libs").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(
        _query(str(tmp_path / "link"), targets=(RepoTarget(repo="r", subdir="libs"),))
    )

    assert cli.keys("rg.search")[0]["path"] == os.path.realpath(
        str(tmp_path / "real" / "libs")
    )


async def test_the_search_root_is_narrowed_to_the_subdir(repo: Path) -> None:
    """检索范围确实收窄到子仓，不是每次都搜整仓。

    不收窄的话命中量爆炸，超过上限后多出来的片段只给语义节点添噪音。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo), targets=(RepoTarget(repo="r", subdir="libs"),)))

    assert cli.keys("rg.search")[0]["path"] == os.path.realpath(str(repo / "libs"))


async def test_no_target_searches_the_whole_snapshot(repo: Path) -> None:
    """路由没给子仓时搜整个快照根——这条路由 ``conflict_code`` 的 ``route_empty``
    闸门挡着，到不了这里，但这一层不该自己再造一个空结果。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo)))

    assert cli.keys("rg.search")[0]["path"] == os.path.realpath(str(repo))


# ---------------------------------------------------------------------------
# 三、rg 缺失 → 切 git 后端，且只切一次
# ---------------------------------------------------------------------------


async def test_a_missing_ripgrep_switches_to_the_git_backend(repo: Path) -> None:
    """``BINARY_MISSING`` 是"换实现"的信号，不是"没有命中"的信号。

    当成后者，搜索就悄悄退化成永远零命中——机器上没装 rg 这件事永远不会被发现。
    """
    cli = FakeCli(
        **{
            "rg.search": None,  # BINARY_MISSING
            "git.grep": CliResult.success(f"{SHA}:libs/rule.py:2:    def check"),
            "git.show": CliResult.success("class LimitRule:\n    def check(self):\n        return 1"),
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert len(out) == 1
    assert out[0].path == "libs/rule.py"
    assert out[0].line_start <= 2 <= out[0].line_end


async def test_the_backend_is_latched_after_the_first_probe(repo: Path) -> None:
    """后端一旦确定就锁定，不要每个 pattern 都再试一次缺失的 rg。

    每轮多出 N 次注定失败的进程启动；更糟的是检测耗时随 pattern 数线性变差，
    看起来像"模型变慢了"。
    """
    cli = FakeCli(
        **{
            "rg.search": None,
            "git.grep": CliResult.success(""),
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo), patterns=("a", "b", "c")))

    assert len(cli.keys("rg.search")) == 1, f"每个 pattern 都重试了 rg：{cli.calls}"
    assert len(cli.keys("git.grep")) == 3


async def test_a_single_pattern_failure_does_not_sink_the_round(repo: Path) -> None:
    """某个 pattern 检索失败（比如正则不合法）只丢它，其他 pattern 照跑。"""
    hit = _rg(_rg_match(str(repo / "libs" / "rule.py"), 2))

    def by_pattern(args: dict[str, Any]) -> CliResult[Any]:
        if args["regexp"] == ["bad("]:
            return CliResult.failure(CliErrorCode.INVALID_ARGUMENT)
        return hit

    cli = FakeCli(**{"rg.search": by_pattern})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), patterns=("bad(", "creditLimit")))

    assert len(out) == 1, "一个 pattern 失败废掉了整轮"


async def test_each_pattern_gets_its_own_invocation(repo: Path) -> None:
    """逐 pattern 调一次，而不是一次传全部 ``-e``。

    一次传全部拿不到"哪个词命中的"，而这个信息决定证据的相关性排序。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo), patterns=("a", "b")))

    assert [a["regexp"] for a in cli.keys("rg.search")] == [["a"], ["b"]]


async def test_ripgrep_is_not_asked_for_context_lines(repo: Path) -> None:
    """上下文自己从磁盘读，不用 ``rg -C``。

    rg 的上下文行混在 JSON Lines 里作为 ``type=="context"``，与 match 的配对关系
    要靠顺序推断——而文件就在本地且已钉在一个 commit 上，按行号切一刀更准。
    """
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo), context_lines=5))

    assert cli.keys("rg.search")[0]["context"] == 0


# ---------------------------------------------------------------------------
# 四、证据本身：行号与 commit 由代码填
# ---------------------------------------------------------------------------


async def test_the_locator_is_well_formed(repo: Path) -> None:
    """locator 必须过 ``CODE_LOCATOR_RE``——不过就得被丢弃而不是降级使用。"""
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "apps" / "ios" / "limit.py"), 10))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert len(out) == 1
    assert CODE_LOCATOR_RE.match(out[0].locator), f"locator 不合法：{out[0].locator}"
    assert out[0].commit_sha == SHA, "证据上的 commit 不是这次钉住的快照"


async def test_paths_are_relative_to_the_snapshot_root(repo: Path) -> None:
    """路径相对仓库根。写绝对路径的话 locator 会带上跑测试那台机器的目录，
    别人点开只能看到一条不存在的路径；而且 ``CODE_LOCATOR_RE`` 也过不了。
    """
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 1))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert out[0].path == "libs/rule.py"


async def test_the_excerpt_carries_the_surrounding_lines(repo: Path) -> None:
    """摘录带上下文，且行号区间与摘录逐行对应。

    区间和内容不一致的话，人点开去核对会发现引的不是那几行——证据就没法核验了。
    """
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "apps" / "ios" / "limit.py"), 10))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=2))

    ev = out[0]
    assert (ev.line_start, ev.line_end) == (8, 12)
    assert ev.excerpt.splitlines() == ["line8", "line9", "line10", "line11", "line12"]


async def test_context_is_clamped_at_the_file_edges(repo: Path) -> None:
    """命中在第 1 行时不能产出 0 或负的起始行——那样的 locator 过不了正则。"""
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 1))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=5))

    assert out[0].line_start == 1
    assert CODE_LOCATOR_RE.match(out[0].locator)


async def test_context_is_clamped_at_the_end_of_file(repo: Path) -> None:
    """命中在最后一行时，结束行不能超过文件长度。

    起始行那侧有 ``max(1, ...)`` 挡着，结束行这侧没有天然下限：切片会安静地短一截，
    于是 locator 写着 ``17-23`` 而摘录只有 4 行。人点开去核对第 23 行，那里什么都
    没有——这正是"证据可核验"被弄断的样子，而且它不报错。
    """
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "apps" / "ios" / "limit.py"), 20))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=3))

    ev = out[0]
    assert (ev.line_start, ev.line_end) == (17, 20)
    assert ev.excerpt.splitlines() == ["line17", "line18", "line19", "line20"]
    assert ev.line_end - ev.line_start + 1 == len(ev.excerpt.splitlines())


async def test_the_symbol_is_guessed_for_display(repo: Path) -> None:
    """``symbol`` 纯展示用，猜不到就是 None——它不进 locator，猜错不影响可核验性。"""
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 3))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=3))

    assert out[0].symbol in {"LimitRule", "check"}


async def test_duplicate_hits_are_collapsed(repo: Path) -> None:
    """同一 ``(path, 行号)`` 只留一条。

    多个 pattern 命中同一行是常态（术语翻译出的词往往同现）。不去重的话同一段代码
    在卡片上出现两遍，而 ``max_hits`` 的名额被自己挤掉。
    """
    same = _rg(_rg_match(str(repo / "libs" / "rule.py"), 2))
    cli = FakeCli(**{"rg.search": same})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), patterns=("a", "b", "c")))

    assert len(out) == 1


async def test_the_hit_count_is_capped(repo: Path) -> None:
    """命中量有硬上限：超出部分只给语义节点添噪音。"""
    lines = [_rg_match(str(repo / "apps" / "ios" / "limit.py"), i) for i in range(1, 16)]
    cli = FakeCli(**{"rg.search": _rg(*lines)})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), max_hits=4))

    assert len(out) == 4


async def test_the_per_pattern_cap_reaches_the_tool(repo: Path) -> None:
    cli = FakeCli(**{"rg.search": _rg()})
    search = RepoSearch(cli, max_hits_per_pattern=7)  # type: ignore[arg-type]

    await search.search(_query(str(repo)))

    assert cli.keys("rg.search")[0]["max_count"] == 7


# ---------------------------------------------------------------------------
# 五、rg 输出解析：坏数据不废整轮
# ---------------------------------------------------------------------------


async def test_non_match_records_are_ignored(repo: Path) -> None:
    """``begin`` / ``end`` / ``summary`` 这些不是命中。

    当成命中会产出没有行号的证据——而 ``line_number`` 缺失时若不校验，就会拿 0 或
    None 去拼 locator。
    """
    cli = FakeCli(
        **{
            "rg.search": _rg(
                json.dumps({"type": "begin", "data": {"path": {"text": "x"}}}),
                json.dumps({"type": "summary", "data": {}}),
                _rg_match(str(repo / "libs" / "rule.py"), 2),
                json.dumps({"type": "end", "data": {"path": {"text": "x"}}}),
            )
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert len(await search.search(_query(str(repo)))) == 1


async def test_a_context_record_is_not_a_hit(repo: Path) -> None:
    """``type == "context"`` 带着合法的 ``line_number``，只校验行号拦不住它。

    上下文行被当成命中，等于把"这个文件附近出现过这个词"报成"就是这一行"。
    今天传的是 ``context: 0`` 所以 rg 不会吐 context 记录，但那是个调用参数——
    哪天有人为了别的目的调大它，命中数就会凭空翻几倍，而且每条都指着错的行。
    """
    path = str(repo / "apps" / "ios" / "limit.py")
    cli = FakeCli(
        **{
            "rg.search": _rg(
                json.dumps(
                    {"type": "context", "data": {"path": {"text": path}, "line_number": 5}}
                ),
                _rg_match(path, 10),
            )
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=0))

    assert [e.line_start for e in out] == [10], f"上下文行被当成命中：{out}"


async def test_a_plain_string_path_is_accepted(repo: Path) -> None:
    """``path`` 偶尔是裸字符串而不是 ``{"text": ...}``。

    只认 dict 的话这条命中被静默丢掉——又一条"少了证据但没人知道"。
    """
    cli = FakeCli(
        **{
            "rg.search": _rg(
                json.dumps(
                    {
                        "type": "match",
                        "data": {"path": str(repo / "libs" / "rule.py"), "line_number": 2},
                    }
                )
            )
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert [e.path for e in out] == ["libs/rule.py"]


async def test_a_broken_json_line_only_loses_itself(repo: Path) -> None:
    """JSON Lines 里出现坏行时跳过它，不废整轮——后面的命中是好的。"""
    cli = FakeCli(
        **{
            "rg.search": _rg(
                "{不是合法 JSON",
                "",
                "   ",
                _rg_match(str(repo / "libs" / "rule.py"), 2),
            )
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert len(await search.search(_query(str(repo)))) == 1


async def test_a_base64_path_is_decoded(repo: Path) -> None:
    """rg 对非 UTF-8 路径给的是 ``{"bytes": base64}``，不是 ``{"text": ...}``。

    不解这一路，含非 ASCII 路径的命中会被静默丢掉——而中文目录名在这个仓里是常态。
    """
    cli = FakeCli(
        **{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 2, as_bytes=True))}
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert [e.path for e in out] == ["libs/rule.py"]


@pytest.mark.parametrize("bad", [0, -3, None, "12"])
async def test_a_bad_line_number_drops_the_hit(repo: Path, bad: Any) -> None:
    """行号不是正整数就丢掉这条命中。

    0 或负数拼出来的 locator 指不到任何位置；字符串会让下游切片崩在别处，
    到时候看到的堆栈与真正的原因隔着三层。
    """
    cli = FakeCli(
        **{
            "rg.search": _rg(
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": str(repo / "libs" / "rule.py")},
                            "line_number": bad,
                        },
                    }
                )
            )
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_a_hit_beyond_the_end_of_file_drops_out(repo: Path) -> None:
    """行号超出文件长度时丢掉——工作区被改过时会出现，此时证据与快照不符。"""
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "libs" / "rule.py"), 9999))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_a_hit_in_a_vanished_file_drops_out(repo: Path) -> None:
    """文件读不到时丢这一条，不抛——只读假设被违反时不该让整轮检测挂掉。"""
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(repo / "gone.py"), 1))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_a_huge_file_is_not_read(repo: Path) -> None:
    """超大文件不读：一个几十 MB 的生成文件会把内存吃掉，而它对"相关实现"的
    判断也没有价值。
    """
    big = repo / "libs" / "generated.js"
    big.write_text("x = 1\n" * 400_000)
    assert big.stat().st_size > 2 * 1024 * 1024
    cli = FakeCli(**{"rg.search": _rg(_rg_match(str(big), 3))})
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


# ---------------------------------------------------------------------------
# 六、git 后端：同形的 locator，且不把不可信数据拼进 argv
# ---------------------------------------------------------------------------


def _git_cli(*grep_lines: str, show: str | None = None) -> FakeCli:
    return FakeCli(
        **{
            "rg.search": None,
            "git.grep": CliResult.success("\n".join(grep_lines)),
            "git.show": CliResult.success(
                show if show is not None else "\n".join(f"g{i}" for i in range(1, 11))
            ),
        }
    )


async def test_the_git_backend_pins_the_snapshot_tree(repo: Path) -> None:
    """``git grep <sha>`` 搜的是被钉住的那棵树，上下文也走 ``git show <sha>:<path>``。

    这比 rg 更正确一点：rg 搜工作区，而"只读浅克隆"是个会被违反的假设——一旦违反，
    locator 里的 ``@sha`` 就指着一份与证据行号不符的内容。
    """
    cli = _git_cli(f"{SHA}:libs/rule.py:4:x")
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert cli.keys("git.grep")[0]["treeish"] == SHA
    assert cli.keys("git.show")[0]["obj"] == f"{SHA}:libs/rule.py"
    assert out[0].commit_sha == SHA


async def test_the_git_backend_narrows_by_cwd_not_by_pathspec(repo: Path) -> None:
    """检索范围靠 **cwd** 收窄，不靠 pathspec。

    pathspec 要把不可信的 ``subdir`` 拼进 argv；cwd 走的是已经过 realpath 越界校验
    的那个路径，少一条注入路径。
    """
    cli = _git_cli()
    search = RepoSearch(cli)  # type: ignore[arg-type]

    await search.search(_query(str(repo), targets=(RepoTarget(repo="r", subdir="libs"),)))

    key, args, cwd = next(c for c in cli.calls if c[0] == "git.grep")
    assert cwd == os.path.realpath(str(repo / "libs"))
    assert not any("libs" in str(v) for v in args.values()), f"subdir 进了 argv：{args}"


async def test_the_git_backend_filters_globs_in_process(repo: Path) -> None:
    """``git grep`` 没有 ``--glob``，排除在进程内做。

    否则排除模式要拼进 pathspec——又一条不可信数据进 argv 的路。而排除是为了证据
    质量（命中一个 ``.min.js`` 或 lock 文件毫无价值却会挤掉真实命中），事后筛够用。
    """
    cli = _git_cli(
        f"{SHA}:node_modules/a/index.js:1:x",
        f"{SHA}:dist/bundle.min.js:1:x",
        f"{SHA}:libs/rule.py:1:x",
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert [e.path for e in out] == ["libs/rule.py"], f"垃圾命中挤进来了：{[e.path for e in out]}"


async def test_the_git_backend_ignores_lines_of_another_treeish(repo: Path) -> None:
    """不带本次 sha 前缀的行不要。混进来的话证据的 commit 与内容不是一套。

    行号出自那棵树，而上下文会从 ``git show <本次 sha>:<path>`` 读，``commit_sha``
    也填本次 sha。拼出来的 locator 每一段都合法、正则也过，只是指错了地方——
    这种错只在有人真去点开核对时才会暴露。
    """
    # 另一个等长 sha 的行。长度要等长才有意义：前缀是按长度切掉的，短行切完就空了，
    # 于是"顺手"就被下游的行号校验挡住——真正能钻进来的恰恰是同形的那种。
    cli = _git_cli(
        f"{'f' * 40}:libs/rule.py:8:x",
        f"{SHA}:libs/rule.py:2:x",
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert len(out) == 1, f"非本次快照的行混进来了：{[e.locator for e in out]}"
    assert out[0].line_start <= 2 <= out[0].line_end


@pytest.mark.parametrize("bad", ["", "no-prefix", "0123:libs/rule.py", "x"])
async def test_the_git_backend_skips_unparsable_lines(repo: Path, bad: str) -> None:
    cli = _git_cli(bad)
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_the_git_backend_rejects_a_non_numeric_line_number(repo: Path) -> None:
    cli = _git_cli(f"{SHA}:libs/rule.py:abc:x", f"{SHA}:libs/rule.py:0:x")
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_the_git_backend_caches_one_show_per_file(repo: Path) -> None:
    """同一文件多条命中只 ``git show`` 一次。

    一次搜索内同一文件常有十几条命中，每条都 fork 一次 git 是纯浪费——而代码搜索的
    耗时直接决定它能不能在一次群聊里给出答复。
    """
    cli = _git_cli(*(f"{SHA}:libs/rule.py:{i}:x" for i in (2, 3, 4, 5)))
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo)))

    assert len(out) == 4
    assert len(cli.keys("git.show")) == 1, f"同一文件 show 了 {len(cli.keys('git.show'))} 次"


async def test_a_failed_show_only_loses_that_evidence(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``git show`` 失败丢这一条证据，不废整轮——但要留下一条 WARNING。

    静默丢弃的话，一个损坏的快照与"这些行里真没有命中"在上层看来完全一样。丢证据
    可以，丢掉"我丢了证据"这件事不行。
    """
    cli = FakeCli(
        **{
            "rg.search": None,
            "git.grep": CliResult.success(f"{SHA}:libs/rule.py:2:x"),
            "git.show": CliResult.failure(CliErrorCode.NOT_FOUND),
        }
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        assert await search.search(_query(str(repo))) == []

    assert any(
        "git show" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ), f"证据被静默丢掉了：{[r.getMessage() for r in caplog.records]}"


async def test_a_failed_git_grep_only_loses_that_pattern(repo: Path) -> None:
    cli = FakeCli(
        **{"rg.search": None, "git.grep": CliResult.failure(CliErrorCode.UNKNOWN)}
    )
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_the_git_backend_drops_a_hit_past_the_end_of_the_blob(repo: Path) -> None:
    """行号超出 ``git show`` 拿到的内容时丢掉这条命中。

    不丢的话切片会得到空串，而区间是 ``47-10``——起始行大于结束行的 locator。它过不了
    人的核对，但能过 ``CODE_LOCATOR_RE``，所以只会在有人点进去的时候才暴露。
    """
    cli = _git_cli(f"{SHA}:libs/rule.py:50:x")  # show 只有 10 行
    search = RepoSearch(cli)  # type: ignore[arg-type]

    assert await search.search(_query(str(repo))) == []


async def test_the_git_backend_excerpt_matches_the_reported_range(repo: Path) -> None:
    """git 后端的摘录同样要与行号区间逐行对应（与 rg 后端同一条主张）。"""
    cli = _git_cli(f"{SHA}:libs/rule.py:5:x")
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(_query(str(repo), context_lines=2))

    ev = out[0]
    assert (ev.line_start, ev.line_end) == (3, 7)
    assert ev.excerpt.splitlines() == ["g3", "g4", "g5", "g6", "g7"]


async def test_the_git_backend_locator_has_the_same_shape(repo: Path) -> None:
    """两个后端产出的 locator 必须完全同形，``repo`` 取路由给的那个名字。

    locator 的第一段是仓名，下游靠它找回哪个仓。换后端换了形状，就等于"证据能不能
    跳过去"取决于这台机器装没装 ripgrep。
    """
    cli = _git_cli(f"{SHA}:libs/rule.py:5:x")
    search = RepoSearch(cli)  # type: ignore[arg-type]

    out = await search.search(
        _query(str(repo), targets=(RepoTarget(repo="core", subdir="libs"),))
    )

    ev = out[0]
    assert ev.repo == "core"
    assert ev.locator == f"core:libs/rule.py:4-6@{SHA[:12]}"
    assert CODE_LOCATOR_RE.match(ev.locator)
