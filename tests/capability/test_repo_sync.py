"""代码仓同步与快照固定（``app/capability/integration/repo/sync.py``）。

**一次检测固定一个 commit sha**，这是 G3 的前提：locator 里的 ``@sha`` 必须对应证据
被读取时的那个代码状态。所以本文件盯的是两件事——快照要么带一个形态合法的 sha，
要么根本不产生（抛 ``PortUnavailable``）；以及这个只读仓真的只读。

带着不合法 sha 的快照是最贵的失败：它能一路走到产出，烧掉一次全仓检索和一次语义
调用，直到最后才被 ``CODE_LOCATOR_RE`` 拦下。所以拦点必须在 ``snapshot()`` 里。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.capability.integration.repo.sync import RepoSync
from app.contracts.errors import PortUnavailable
from app.contracts.ids import WorkspaceId
from app.contracts.ports import WorkspaceConfig
from app.platform.cli.result import CliErrorCode, CliResult

SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeCli:
    """按命令 key 回放。``calls`` 留下全部调用，"只读仓"这条主张只能靠它证明。"""

    def __init__(self, **by_key: Any) -> None:
        self._by_key = by_key
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def run(
        self, key: str, args: dict[str, Any], *, cwd: str | None = None, **kw: Any
    ) -> CliResult[Any]:
        self.calls.append((key, dict(args), cwd))
        canned = self._by_key.get(key)
        if canned is None:
            return CliResult.failure(CliErrorCode.BINARY_MISSING)
        return canned

    def keys(self, key: str) -> list[dict[str, Any]]:
        return [a for k, a, _ in self.calls if k == key]


def _cli(*, sha: str | CliResult[Any] = SHA, fetch: CliResult[Any] | None = None) -> FakeCli:
    return FakeCli(
        **{
            "git.rev_parse": sha if isinstance(sha, CliResult) else CliResult.success(sha),
            "git.fetch": fetch if fetch is not None else CliResult.success(""),
        }
    )


def _ws(root: str | None, *, branch: str = "main") -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_id=WorkspaceId("alpha"), repo_root=root, repo_branch=branch
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个看起来像 git 仓的目录。``.git/HEAD`` 的 mtime 就是"这份拷贝有多旧"。"""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"{SHA}\n")
    return tmp_path


def _age(path: Path, hours: float) -> None:
    ts = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# 一、没有仓就没有快照
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", [None, "", "/nope/not/here"])
async def test_a_missing_repo_root_is_a_port_failure(root: str | None) -> None:
    """仓库根不存在时抛，而不是返回一个 ``root=None`` 的快照。

    R4 里 ``REPO_ROOT`` 今天可能压根没配。返回空快照会让代码搜索"搜了个空目录、
    没有命中"——一个未配置被说成一条结论。
    """
    cli = _cli()
    with pytest.raises(PortUnavailable) as e:
        await RepoSync(cli).snapshot(_ws(root))  # type: ignore[arg-type]

    # 诊断必须说对是哪一种：一个没配的 REPO_ROOT 被报成"存在但不是 git 仓库"，
    # 会把排查方向从"去配一下"引到"这个目录怎么坏了"。
    assert "根目录不存在" in e.value.reason, e.value.reason
    assert cli.calls == [], "仓都没有还去 fork 了 git"


async def test_a_directory_that_is_not_a_git_repo_is_rejected(tmp_path: Path) -> None:
    """存在但不是 git 仓也要抛——没有 ``.git`` 就没有 sha，没有 sha 就没有 locator。"""
    with pytest.raises(PortUnavailable) as e:
        await RepoSync(_cli()).snapshot(_ws(str(tmp_path)))  # type: ignore[arg-type]
    assert "不是 git 仓库" in e.value.reason


# ---------------------------------------------------------------------------
# 二、要么带合法 sha，要么不产生快照
# ---------------------------------------------------------------------------


async def test_the_snapshot_carries_the_head_sha(repo: Path) -> None:
    cli = _cli()
    snap = await RepoSync(cli).snapshot(_ws(str(repo), branch="release/1.2"))  # type: ignore[arg-type]

    assert snap.commit_sha == SHA
    assert snap.root == str(repo)
    assert snap.branch == "release/1.2", "分支没带进快照，locator 说不清是哪条线的代码"
    assert cli.calls[0][0] == "git.rev_parse"
    assert cli.calls[0][2] == str(repo), "rev-parse 没在仓目录里跑"


async def test_a_failing_rev_parse_never_yields_a_snapshot(repo: Path) -> None:
    """取不到 sha 必须抛，且把原始错误码带上。

    返回一个空 sha 的快照会让检测继续跑完全仓检索和语义判断，最后才被 locator 正则
    拦下——白烧一轮，而且错误原因已经丢在三层之下了。
    """
    cli = _cli(sha=CliResult.failure(CliErrorCode.BINARY_MISSING))
    with pytest.raises(PortUnavailable) as e:
        await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert e.value.error_code is CliErrorCode.BINARY_MISSING
    assert "HEAD sha" in e.value.reason


@pytest.mark.parametrize("bad", ["", "   ", "\n", "abc", "0123456789a", "not-a-sha"])
async def test_a_malformed_sha_is_refused_instead_of_shipped(repo: Path, bad: str) -> None:
    """形态不对的 sha 一律拒绝。

    ``abc`` 拼出来的 locator 是 ``repo:path:1-3@abc``——过不了 ``CODE_LOCATOR_RE``，
    但要等到产出那一刻才知道。裁决 #14 说的是"降级掉的只是归因，不是 commit 本身"，
    所以这里不能放水成"给个短的也行"。
    """
    with pytest.raises(PortUnavailable) as e:
        await RepoSync(_cli(sha=bad)).snapshot(_ws(str(repo)))  # type: ignore[arg-type]
    assert e.value.error_code is CliErrorCode.OUTPUT_UNPARSABLE


@pytest.mark.parametrize(
    "raw",
    [
        f"  {SHA}  \nrefs/heads/main\n",
        f"{SHA}\n",
        # 前导空行：先整体 strip 再切第一行，顺序反了就切出一个空串，于是一个好端端
        # 的 sha 被报成"形态异常"。
        f"\n{SHA}\n",
        f"\n\n  {SHA}\n\n",
    ],
)
async def test_extra_output_lines_do_not_end_up_in_the_sha(repo: Path, raw: str) -> None:
    """``rev-parse`` 多吐几行时只取第一行，并去掉首尾空白。

    sha 里混进一个 ``\\n`` 就是一个过不了正则的 locator，而它长度足够、看着正常。
    """
    snap = await RepoSync(_cli(sha=raw)).snapshot(_ws(str(repo)))  # type: ignore[arg-type]
    assert snap.commit_sha == SHA


# ---------------------------------------------------------------------------
# 三、"这份拷贝有多旧"取的是本地 mtime，不是 commit 时间
# ---------------------------------------------------------------------------


async def test_the_age_comes_from_the_fetch_head_mtime(repo: Path) -> None:
    """``age_hours`` 量的是"我们本地这份拷贝有多旧"。

    不解析 commit 时间：那是作者提交的时刻，可能是三个月前，于是一个刚 clone 的仓库
    会被判成快照过期，代码搜索直接以 ``snapshot_expired`` 收尾。
    """
    (repo / ".git" / "FETCH_HEAD").write_text("x\n")
    _age(repo / ".git" / "FETCH_HEAD", 3.0)

    snap = await RepoSync(_cli(), fetch_enabled=False).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert 2.9 < snap.age_hours < 3.1
    assert snap.synced_at.tzinfo is not None, "无时区的时间戳一跨机器就变成另一个时刻"
    # 两个字段必须描述同一个时刻。不然拿 synced_at 渲染"最近同步于"的地方会显示
    # "刚刚"，而 age 说是 3 小时前——同一张卡片上自相矛盾。
    drift = (datetime.now(UTC) - snap.synced_at).total_seconds() / 3600 - snap.age_hours
    assert abs(drift) < 0.05, f"synced_at 与 age_hours 不是一回事：偏差 {drift}h"


async def test_fetch_head_wins_over_head(repo: Path) -> None:
    """有 ``FETCH_HEAD`` 就用它——``HEAD`` 的 mtime 会被本地操作改，不代表同步时间。"""
    (repo / ".git" / "FETCH_HEAD").write_text("x\n")
    _age(repo / ".git" / "FETCH_HEAD", 10.0)
    _age(repo / ".git" / "HEAD", 0.1)

    snap = await RepoSync(_cli(), fetch_enabled=False).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert snap.age_hours > 9.0, "用了 HEAD 的 mtime，把一个 10 小时前的快照当成刚同步"


async def test_head_is_the_fallback_when_there_is_no_fetch_head(repo: Path) -> None:
    """浅克隆之后还没 fetch 过的仓没有 ``FETCH_HEAD``，退到 ``HEAD``。"""
    _age(repo / ".git" / "HEAD", 5.0)

    snap = await RepoSync(_cli(), fetch_enabled=False).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert 4.9 < snap.age_hours < 5.1


async def test_neither_file_means_just_synced_not_infinitely_old(repo: Path) -> None:
    """两个文件都读不到时按"刚同步"算，而不是让 age 变成一个巨大的数。

    判成极旧会让每次检测都先 fetch 一次——高频群聊里就是把 git 服务端打满。
    """
    (repo / ".git" / "HEAD").unlink()

    snap = await RepoSync(_cli()).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert snap.age_hours < 0.1


async def test_a_clock_in_the_future_does_not_make_the_age_negative(repo: Path) -> None:
    """mtime 在未来（时钟偏移、挂载的文件系统）时 age 夹到 0，不是负数。

    负的 age 会让 ``age_hours > 24`` 这个判断永远为假，fetch 从此不再发生——一个
    悄悄冻结的快照，而它看起来一切正常。
    """
    _age(repo / ".git" / "HEAD", -48.0)

    snap = await RepoSync(_cli()).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert snap.age_hours == 0.0


# ---------------------------------------------------------------------------
# 四、日级 fetch：拉与不拉都要有理由
# ---------------------------------------------------------------------------


async def test_a_stale_repo_is_fetched_shallow_and_single_branch(repo: Path) -> None:
    """超过一天才拉，且带 ``depth=1`` 与显式分支。

    不限定分支会去拉默认 refspec，拿到的 HEAD 可能不是本业务线配置的那条线
    （P0 #8 兜底的正是浅克隆单分支）。
    """
    _age(repo / ".git" / "HEAD", 30.0)
    cli = _cli()

    await RepoSync(cli).snapshot(_ws(str(repo), branch="release/1.2"))  # type: ignore[arg-type]

    args = cli.keys("git.fetch")
    assert len(args) == 1, f"fetch 了 {len(args)} 次"
    assert args[0]["depth"] == 1
    assert args[0]["branch"] == "release/1.2"
    assert cli.calls[0][2] == str(repo), "fetch 没在仓目录里跑，拉的是别处的仓"
    assert cli.calls[0][0] == "git.fetch", "先取了 sha 再 fetch——快照会是 fetch 前的那个"


async def test_a_fresh_repo_is_not_fetched(repo: Path) -> None:
    """一天内同步过就不拉。

    每次搜索都 fetch 会让快照在两次搜索之间不断变化：今天给出的命中，明天
    指向的行号已经漂了。
    """
    _age(repo / ".git" / "HEAD", 2.0)
    cli = _cli()

    await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert cli.keys("git.fetch") == []


async def test_fetch_can_be_switched_off_entirely(repo: Path) -> None:
    """``fetch_enabled=False`` 时即使很旧也不拉——离线/受限网络下的开关。"""
    _age(repo / ".git" / "HEAD", 100.0)
    cli = _cli()

    snap = await RepoSync(cli, fetch_enabled=False).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert cli.keys("git.fetch") == []
    assert snap.age_hours > 99.0, "没拉却把 age 报成新的，上层就不会判 snapshot_expired"


async def test_the_age_is_recomputed_after_a_successful_fetch(repo: Path) -> None:
    """拉完要重新算 age，否则刚同步的仓仍带着 30 小时的 age。

    ``conflict_code`` 用 age 判 ``snapshot_expired``——不重算就等于拉了也白拉。
    """
    _age(repo / ".git" / "HEAD", 30.0)

    class Fetching(FakeCli):
        async def run(self, key: str, args: dict[str, Any], **kw: Any) -> CliResult[Any]:
            if key == "git.fetch":
                (repo / ".git" / "FETCH_HEAD").write_text("fetched\n")
            return await super().run(key, args, **kw)

    cli = Fetching(
        **{"git.rev_parse": CliResult.success(SHA), "git.fetch": CliResult.success("")}
    )
    snap = await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert snap.age_hours < 0.1, f"fetch 后 age 没重算：{snap.age_hours}"


async def test_a_failed_fetch_still_yields_the_old_snapshot(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """fetch 失败不抛：一个 20 小时前的快照做出的结论依然成立。

    locator 指向的就是那个 sha，证据是可核验的。真正超龄时 ``conflict_code`` 会以
    ``snapshot_expired`` 收尾——那才是该报未完成的地方，而不是在这里把整条链路掀掉。
    但失败要留一条 WARNING，否则"我们用的是旧快照"这件事没人知道。
    """
    _age(repo / ".git" / "HEAD", 30.0)
    cli = _cli(fetch=CliResult.failure(CliErrorCode.RATE_LIMITED))

    with caplog.at_level("WARNING"):
        snap = await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    assert snap.commit_sha == SHA
    assert snap.age_hours > 29.0
    assert any(
        "fetch" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ), "拉失败了却一声不响"


# ---------------------------------------------------------------------------
# 五、只读仓
# ---------------------------------------------------------------------------


async def test_nothing_touches_the_working_tree(repo: Path) -> None:
    """不 checkout、不 merge、不 pull、不 reset。

    检测是只读链路。一旦动了工作区，就有两个后果：检测期间行号会漂（locator 指错），
    以及一个共享仓上的检测会互相踩。
    """
    _age(repo / ".git" / "HEAD", 30.0)
    cli = _cli()

    await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    used = {k for k, _, _ in cli.calls}
    assert used <= {"git.fetch", "git.rev_parse"}, f"动了工作区：{used}"


async def test_the_head_is_read_with_verify(repo: Path) -> None:
    """``rev-parse --verify``：没有这个开关，``rev-parse`` 会把无法解析的名字原样吐回来。

    于是 ``HEAD`` 在一个没有任何 commit 的仓里会"成功"返回字符串 ``HEAD``——长度不够
    12，会被形态校验挡下，但错误原因就从"仓里没有 commit"变成了"sha 形态异常"。
    """
    cli = _cli()
    await RepoSync(cli).snapshot(_ws(str(repo)))  # type: ignore[arg-type]

    args = cli.keys("git.rev_parse")[0]
    assert args["verify"] is True
    assert args["rev"] == "HEAD"


# ---------------------------------------------------------------------------
# 六、last_synced_at：repo_sync Job 的新鲜度来源（US3-11）
# ---------------------------------------------------------------------------


async def test_last_synced_at_reads_fetch_head_mtime(repo: Path) -> None:
    """与 ``snapshot()`` 同口径：``FETCH_HEAD`` 优先于 ``HEAD``。"""
    (repo / ".git" / "FETCH_HEAD").write_text("x\n")
    _age(repo / ".git" / "FETCH_HEAD", 6.0)
    _age(repo / ".git" / "HEAD", 0.1)

    ts = await RepoSync(_cli()).last_synced_at(str(repo))

    assert ts is not None and ts.tzinfo is not None
    age_h = (datetime.now(UTC) - ts).total_seconds() / 3600
    assert 5.9 < age_h < 6.1


async def test_last_synced_at_returns_none_when_never_synced(repo: Path) -> None:
    """两个文件都读不到 = 从没同步过。

    这里刻意**不**套用 ``snapshot()`` 的"当作刚同步"兜底——Job 要靠 ``None``
    区分"从没同步过"，兜底会把这件事抹平。
    """
    (repo / ".git" / "HEAD").unlink()

    assert await RepoSync(_cli()).last_synced_at(str(repo)) is None


@pytest.mark.parametrize("root", [None, "", "/nope/not/here"])
async def test_last_synced_at_missing_root_is_a_port_failure(root: str | None) -> None:
    """仓根没配/不存在是装配缺口，抛 ``PortUnavailable``，不是"没同步过"。"""
    with pytest.raises(PortUnavailable) as e:
        await RepoSync(_cli()).last_synced_at(root)
    assert "根目录不存在" in e.value.reason


async def test_repo_service_exposes_last_synced_at(repo: Path) -> None:
    """``RepoService`` 把构造时的 ``repo_root`` 带上，Job 调用不需要再传仓。"""
    from app.capability.integration.repo.service import RepoService

    _age(repo / ".git" / "HEAD", 2.0)
    service = RepoService(_cli(), repo_root=str(repo))

    ts = await service.last_synced_at()

    assert ts is not None
    assert 1.9 < (datetime.now(UTC) - ts).total_seconds() / 3600 < 2.1
