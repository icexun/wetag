"""代码大仓同步与快照固定。

**一次检测固定一个 commit sha。** 这是 G3 的前提：locator 里的 ``@sha`` 必须对应
证据被读取时的那个代码状态。如果检测期间仓库被 fetch 更新了，行号就会漂——用户
点开 locator 看到的是另一段代码。所以 ``snapshot()`` 取一次 HEAD 后，同一次检测的
所有检索都复用它。

浅克隆 ``--depth=1`` 单分支（P0 #8 兜底）。代价是拿不到 ``git blame`` 级归因
（A.4 已接受降级），但**快照 sha 照旧有**——裁决 #14 明确：降级掉的只是归因，
不是 commit 本身。禁止因为"归因做不了"就把 locator 里的 sha 留空。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from app.contracts.errors import PortUnavailable
from app.contracts.ports import RepoSnapshot, WorkspaceConfig
from app.platform.cli.executor import CliExecutor
from app.platform.cli.result import CliErrorCode

logger = logging.getLogger(__name__)


class RepoSync:
    """仓库同步器。只读仓：不 checkout、不 merge、不写工作区。"""

    def __init__(self, executor: CliExecutor, *, fetch_enabled: bool = True) -> None:
        self._cli = executor
        self._fetch_enabled = fetch_enabled

    async def snapshot(self, ws: WorkspaceConfig) -> RepoSnapshot:
        """固定一次快照。

        :raises PortUnavailable: 仓库根目录不存在，或取不到 HEAD sha。取不到 sha
            必须抛而不是返回空 sha 的快照——后者会让代码搜索带着一个不合法的
            locator 一路走到产出，直到最后才被 ``CODE_LOCATOR_RE`` 拦下，
            白烧掉一次检索和一次语义调用。
        """
        root = ws.repo_root
        self._assert_repo(root)
        assert root is not None  # _assert_repo 已排除

        synced_at = await self._last_fetch_time(root)
        age_hours = max(0.0, (datetime.now(UTC) - synced_at).total_seconds() / 3600)

        # 日级 fetch：超过阈值才拉。每次检测都 fetch 会让快照在检测间不断变化，
        # 且在高频群聊场景里把 git 服务端打满。
        if self._fetch_enabled and age_hours > 24:
            await self._fetch(root, ws.repo_branch)
            synced_at = await self._last_fetch_time(root)
            age_hours = max(0.0, (datetime.now(UTC) - synced_at).total_seconds() / 3600)

        sha = await self._head_sha(root)
        return RepoSnapshot(
            root=root,
            branch=ws.repo_branch,
            commit_sha=sha,
            synced_at=synced_at,
            age_hours=age_hours,
        )

    # -- 内部 -------------------------------------------------------------
    async def last_synced_at(self, root: str | None) -> datetime | None:
        """最近一次成功 fetch 的时刻；从未同步过返回 ``None``。

        与 ``snapshot()`` 的口径差一处：这里**不做**"取不到就当作现在"的兜底——
        那个兜底只是为了让 ``age_hours`` 有值可算，而本方法的调用方
        （``repo_sync`` Job）恰恰要靠 ``None`` 区分"从没同步过"。
        """
        self._assert_repo(root)
        assert root is not None  # _assert_repo 已排除
        for name in ("FETCH_HEAD", "HEAD"):
            path = os.path.join(root, ".git", name)
            try:
                return datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
            except OSError:
                continue
        return None

    def _assert_repo(self, root: str | None) -> None:
        if not root or not os.path.isdir(root):
            raise PortUnavailable(
                "repo.sync",
                CliErrorCode.BINARY_MISSING,
                f"代码仓根目录不存在：{root!r}",
            )
        if not os.path.isdir(os.path.join(root, ".git")):
            raise PortUnavailable(
                "repo.sync", CliErrorCode.BINARY_MISSING, f"{root} 不是 git 仓库"
            )

    async def _head_sha(self, root: str) -> str:
        result = await self._cli.run(
            "git.rev_parse", {"rev": "HEAD", "verify": True}, cwd=root
        )
        if not result.ok:
            raise PortUnavailable(
                "repo.sync",
                result.error_code or CliErrorCode.UNKNOWN,
                "取不到 HEAD sha，无法生成可核验的代码定位",
            )
        # 只取 data：``git.rev_parse`` 注册为 ``json_output=False``，data 就是 stdout
        # 原文。**不要回退到 ``stdout_digest``**——那是掩码并截断过的审计摘要，
        # 拿它当 sha 会产出一个长度够、形态像、却指不到任何 commit 的 locator。
        # 拿不到就让下面的形态校验抛，把"取不到 sha"如实报出去。
        sha = (result.data or "").strip() if isinstance(result.data, str) else ""
        sha = sha.split("\n")[0].strip()
        if len(sha) < 12:
            raise PortUnavailable(
                "repo.sync", CliErrorCode.OUTPUT_UNPARSABLE, f"HEAD sha 形态异常：{sha!r}"
            )
        return sha

    async def _fetch(self, root: str, branch: str) -> None:
        """拉取更新。失败**不抛**——旧快照仍然可用，只是 age 偏大。

        让 fetch 失败直接中断检索是过度反应：用一个 20 小时前的快照检索，结论依然
        成立（locator 指向的就是那个 sha）；快照年龄随 ``age_hours`` 一并返回。
        """
        result = await self._cli.run(
            "git.fetch",
            {"depth": 1, "branch": branch},
            cwd=root,
        )
        if not result.ok:
            logger.warning(
                "git fetch 失败，继续使用现有快照：%s", result.error_code
            )

    async def _last_fetch_time(self, root: str) -> datetime:
        """用 ``.git/FETCH_HEAD``（无则 ``HEAD``）的 mtime 当最近同步时间。

        不解析 commit 时间：commit 时间是**作者**提交的时刻，可能是三个月前，
        而我们要问的是"我们本地这份拷贝有多旧"。两者混淆会让一个刚 clone 的仓库
        被判成快照过期。
        """
        for name in ("FETCH_HEAD", "HEAD"):
            path = os.path.join(root, ".git", name)
            try:
                return datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
            except OSError:
                continue
        return datetime.now(UTC)


__all__ = ["RepoSync"]
