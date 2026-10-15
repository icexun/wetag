"""代码侧的真实链路：ripgrep → ``CodeEvidence`` → locator → ``@commit``。

"locator 能不能对回真实命中"这件事**只有真工具能证明**：行号取自 ripgrep 的输出、
commit 取自 ``git rev-parse``，两者中间隔着 JSON 解析、上下文切片、路径拼接。

所以这里把本仓当被检索的代码仓，跑通整条链：``RepoSync.snapshot`` 取真 HEAD，
``RepoSearch`` 跑真 ``rg --json``。用例会在缺 ``rg`` 或 ``git`` 时自动跳过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.capability.integration.repo.service import RepoService
from app.contracts.ports import CODE_LOCATOR_RE
from app.contracts.errors import PortUnavailable
from app.contracts.ids import WorkspaceId
from app.contracts.ports import CodeQuery, RepoTarget, WorkspaceConfig
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import ALL_COMMANDS

REPO = Path(__file__).resolve().parents[2]


def _bin(name: str) -> str | None:
    """找一个**真二进制**。

    ``WETAG_<NAME>_BIN`` 可显式覆盖：有些开发机上 ``rg`` 是个 shell 函数（编辑器或
    CLI 工具包装的），此时 ``which`` 找不到东西，而 ``CliExecutor`` 也确实用不了它
    ——它一律以 argv 数组 spawn，不经过 shell（禁 ``shell=True`` 是注入防线）。
    所以这里的探测口径必须和执行器一致，否则会出现"我这儿 rg 能用"但生产里
    直接 ``BINARY_MISSING`` 的偏差。
    """
    return os.environ.get(f"WETAG_{name.upper()}_BIN") or shutil.which(name)


RG, GIT = _bin("rg"), _bin("git")

pytestmark = pytest.mark.skipif(
    not (RG and GIT and (REPO / ".git").is_dir()),
    reason="需要真实的 rg + git 二进制与一个 git 工作区（可用 WETAG_RG_BIN 指定）",
)


@pytest.fixture(scope="module")
def code() -> RepoService:
    cli = CliExecutor(
        workspace_id="alpha",
        binaries={"rg": RG or "rg", "git": GIT or "git"},
        secrets={},
        specs=ALL_COMMANDS,
        timeout_s=30.0,
    )
    # fetch_enabled=False：单测绝不碰网络，也绝不改动开发者的工作区。
    return RepoService(cli, repo_root=str(REPO), fetch_enabled=False)


@pytest.fixture(scope="module")
def ws() -> WorkspaceConfig:
    return WorkspaceConfig(workspace_id=WorkspaceId("alpha"), repo_root=str(REPO))


async def test_snapshot_carries_a_real_head_sha(code: RepoService, ws: WorkspaceConfig) -> None:
    """快照的 sha 必须是真 HEAD，且不是空串。

    裁决 #14：浅克隆降级掉的只是 ``git blame`` 级归因，不是 commit 本身。sha 留空
    会让每一条 locator 都过不了 G3 正则，而那时错误出现在链路末端，排查要走一圈。
    """
    snap = await code.sync(ws)

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert snap.commit_sha == expected
    assert len(snap.commit_sha) >= 12
    assert snap.age_hours >= 0.0


async def test_real_ripgrep_hits_produce_g3_locators(
    code: RepoService, ws: WorkspaceConfig
) -> None:
    """真检索的每一条证据都要过 G3 正则，且 commit 与本次快照一致。

    这是整条链路唯一的端到端证明：行号来自 rg 的 JSON、commit 来自 git，两者要在
    ``locator`` 里拼成一个能回到那一行的定位。
    """
    snap = await code.sync(ws)
    targets = [RepoTarget(repo=REPO.name, subdir="app/capability")]

    hits = await code.search(
        CodeQuery(snapshot=snap, targets=tuple(targets), patterns=("PortUnavailable",))
    )

    assert hits, "本仓 app/capability 里 PortUnavailable 一定搜得到"
    for h in hits:
        assert CODE_LOCATOR_RE.match(h.locator), h.locator
        assert h.commit_sha[:12] == snap.commit_sha[:12]
        assert h.line_start >= 1 and h.line_end >= h.line_start
        assert h.excerpt.strip()


async def test_hit_line_numbers_point_at_the_actual_matching_line(
    code: RepoService, ws: WorkspaceConfig
) -> None:
    """行号必须真的指向命中行。

    这条是上一条的加强版：正则过了只说明形状对，不说明数字对。这里拿 locator 里的
    行号回文件里读，断言那一行确实含检索词——off-by-one 会让用户点开跳到隔壁一行，
    而这种错在 Fake 里永远不会暴露。
    """
    snap = await code.sync(ws)
    targets = [RepoTarget(repo=REPO.name, subdir="app/contracts")]
    hits = await code.search(
        CodeQuery(
            snapshot=snap,
            targets=tuple(targets),
            patterns=("CODE_LOCATOR_RE",),
            context_lines=2,
        )
    )
    assert hits

    checked = 0
    for h in hits:
        lines = (REPO / h.path).read_text(encoding="utf-8").splitlines()
        window = lines[h.line_start - 1 : h.line_end]
        assert any("CODE_LOCATOR_RE" in l for l in window), (
            f"{h.locator} 指向的 {h.line_start}-{h.line_end} 行里没有检索词"
        )
        checked += 1
    assert checked


async def _search_in(code: RepoService, ws: WorkspaceConfig, subdir: str) -> list:
    snap = await code.sync(ws)
    return await code.search(
        CodeQuery(
            snapshot=snap,
            targets=(RepoTarget(repo=REPO.name, subdir=subdir),),
            patterns=("import",),
        )
    )


async def test_search_skips_a_subdir_that_does_not_exist(
    code: RepoService, ws: WorkspaceConfig
) -> None:
    """写错的路径 ⇒ 显式失败，而不是空命中或退化成全仓检索。"""
    with pytest.raises(PortUnavailable):
        await _search_in(code, ws, "app/does/not/exist")


async def test_search_cannot_escape_the_repo_root(
    code: RepoService, ws: WorkspaceConfig
) -> None:
    """``subdir`` 不得穿越出仓库根目录。

    subdir 是不可信数据，而它在这里被当作路径拼接。每个路径都指向一个**真实存在**的
    仓外目录——否则"不存在"就会替越界检查把用例染绿。
    """
    outside = ["/etc", "/", "..", "../..", "app/capability/../../.."]
    for evil in outside:
        resolved = os.path.realpath(os.path.join(str(REPO), evil))
        assert os.path.isdir(resolved), f"{evil} 必须指向真实存在的目录，否则本用例是空的"
        assert not resolved.startswith(str(REPO.resolve()) + os.sep), evil
        with pytest.raises(PortUnavailable):
            await _search_in(code, ws, evil)


async def test_search_still_accepts_a_legitimate_subdir(
    code: RepoService, ws: WorkspaceConfig
) -> None:
    """越界防线不能宽到把正常子目录也拒掉。"""
    hits = await _search_in(code, ws, "app/capability")
    assert hits
    assert all(h.path.startswith("app/capability/") for h in hits)
