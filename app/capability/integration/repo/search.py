"""ripgrep 检索 → ``CodeEvidence``（G3 的证据来源）。

``rg --json`` 输出的是 **JSON Lines**，逐行解析 ``type == "match"``。每条 match
自带 ``path``、``line_number`` 和匹配文本，再按 ``context_lines`` 定向读上下文，
拼成带 ``行号@commit`` 的 locator。

**行号与 commit 都由代码填，模型碰不到。** 模型只能引用某一条命中，所以它无法
编造一个能通过 ``CODE_LOCATOR_RE`` 的定位。这是"证据可核验"在实现层的落法。

``rg`` 不可用（``BINARY_MISSING``）时自动切到 ``git grep`` 后端。这不只是兜底，
它还**更正确一点**：``git grep <sha>`` 检索的是被钉住的那个快照树，而 ``rg`` 搜的是
工作区。二者在只读浅克隆里一致，但"只读"是个会被违反的假设，而一旦违反，locator
里的 ``@sha`` 就指着一份与证据行号不符的内容。git 后端从源头消除了这个偏差——
上下文也走 ``git show <sha>:<path>``，而不是读磁盘。

上下文（rg 后端）从**磁盘直接读**，不用 ``rg -C``：rg 的上下文行会混在 JSON Lines 里作为
``type == "context"``，与 match 的配对关系要靠顺序推断，而文件内容本来就在本地
且已经固定在一个 commit 上，直接按行号切一刀更简单也更准。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from typing import Iterable, Sequence

from app.contracts.errors import PortUnavailable
from app.contracts.ports import CodeEvidence, CodeQuery, RepoTarget
from app.platform.cli.executor import CliExecutor
from app.platform.cli.result import CliErrorCode

logger = logging.getLogger(__name__)

# 代码检索的文件类型白名单。不是为了性能，是为了证据质量：
# 命中一个 .min.js 或 lock 文件对代码检索毫无价值，却会挤掉真实命中。
_DEFAULT_GLOBS = (
    "!**/node_modules/**",
    "!**/.git/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/vendor/**",
    "!**/*.min.js",
    "!**/*.map",
    "!**/*.lock",
    "!**/*.snap",
)
# 单文件读取上限。防一个几十 MB 的生成文件把内存吃掉。
_MAX_FILE_BYTES = 2 * 1024 * 1024


class RepoSearch:
    """ripgrep 检索器。"""

    def __init__(self, executor: CliExecutor, *, max_hits_per_pattern: int = 20) -> None:
        self._cli = executor
        self._max_per_pattern = max_hits_per_pattern
        # 后端一旦确定就锁定：不要每个 pattern 都重试一次缺失的 rg，
        # 那会让每轮检索多出 N 次注定失败的进程启动。
        self._backend: str | None = None
        # git show 的内容缓存，键 (treeish, path)。一次检测内同一文件常有多条命中，
        # 每条都 fork 一次 git 是纯浪费。检测结束随对象一起回收。
        self._blob_cache: dict[tuple[str, str], list[str] | None] = {}

    async def search(self, q: CodeQuery) -> list[CodeEvidence]:
        """按 ``patterns × targets`` 检索。

        逐个 pattern 调一次 rg 而不是一次传全部 ``-e``：一次传全部拿不到"哪个词
        命中的"，而这个信息决定了证据的相关性排序。代价是多几次进程启动，
        在本地文件系统上可忽略。
        """
        snap = q.snapshot
        out: list[CodeEvidence] = []
        seen: set[tuple[str, int]] = set()
        searched_any = False

        for target in q.targets or (RepoTarget(repo=os.path.basename(snap.root)),):
            search_root = _resolve_root(snap.root, target)
            if search_root is None:
                logger.warning("子仓路径不存在，跳过：%s/%s", target.repo, target.subdir)
                continue
            for pattern in q.patterns:
                searched_any = True
                hits = await self._run_one(q, target, search_root, pattern)
                for h in hits:
                    key = (h.path, h.line_start)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(h)

        if not searched_any:
            # 所有子仓路径都不存在。这不是"无命中"，是根本没搜——
            # 必须让上层看出区别，否则会把配置错误报成"代码里没有"。
            raise PortUnavailable(
                "repo.search",
                CliErrorCode.BINARY_MISSING,
                "所有路由子仓路径都不存在，未执行任何检索",
            )
        return out[: q.max_hits]

    # -- 内部 -------------------------------------------------------------
    async def _run_one(
        self, q: CodeQuery, target: RepoTarget, search_root: str, pattern: str
    ) -> list[CodeEvidence]:
        globs = list(_DEFAULT_GLOBS) + [g for g in q.globs if g]

        if self._backend == "git":
            return await self._run_git(q, target, search_root, pattern, globs)

        result = await self._cli.run(
            "rg.search",
            {
                "regexp": [pattern],
                "glob": globs,
                "max_count": self._max_per_pattern,
                "context": 0,  # 上下文自己读，见模块 docstring
                "ignore_case": False,
                "path": search_root,
            },
        )
        if not result.ok:
            if result.error_code is CliErrorCode.BINARY_MISSING:
                # ripgrep 没装。这是"换实现"的信号，不是"没有命中"的信号——
                # 后者会让搜索悄悄退化成永远报没有。
                logger.warning("ripgrep 不可用，本进程切换到 git grep 后端")
                self._backend = "git"
                return await self._run_git(q, target, search_root, pattern, globs)
            # 单个 pattern 检索失败不该中断整轮：其他 pattern 可能有命中。
            logger.warning("rg 检索失败 pattern=%r：%s", pattern, result.error_code)
            return []
        self._backend = "rg"

        raw = (result.data if isinstance(result.data, str) else "")
        return list(
            self._parse(raw, q, target, search_root)
        )

    async def _run_git(
        self,
        q: CodeQuery,
        target: RepoTarget,
        search_root: str,
        pattern: str,
        globs: Sequence[str],
    ) -> list[CodeEvidence]:
        """``git grep`` 后端。

        检索范围靠 **cwd** 收窄（``search_root`` 已经过 realpath 越界校验），
        不靠 pathspec——少一处需要把不可信的 ``subdir`` 拼进 argv 的地方。
        ``--full-name`` 保证路径仍相对仓库根，于是 locator 与 rg 后端完全同形。
        """
        treeish = q.snapshot.commit_sha
        result = await self._cli.run(
            "git.grep",
            {
                "pattern": pattern,
                "treeish": treeish,
                "max_count": self._max_per_pattern,
                "fixed_strings": True,
                "ignore_case": False,
            },
            cwd=search_root,
        )
        if not result.ok:
            logger.warning("git grep 失败 pattern=%r：%s", pattern, result.error_code)
            return []

        prefix = f"{treeish}:"
        out: list[CodeEvidence] = []
        for raw_line in ((result.data if isinstance(result.data, str) else "")).splitlines():
            if not raw_line.startswith(prefix):
                continue
            rest = raw_line[len(prefix):]
            # path 可能含 ':'（罕见但合法），所以从**右**边切出行号
            head, sep, _text = rest.partition(":")
            if not sep:
                continue
            path, lineno = head, None
            tail = rest[len(head) + 1:]
            num, sep2, _ = tail.partition(":")
            if not sep2 or not num.isdigit():
                continue
            lineno = int(num)
            if lineno <= 0 or not _glob_ok(path, globs):
                continue

            lines = await self._blob(treeish, path)
            if lines is None:
                continue
            excerpt, l1, l2 = _slice_context(lines, lineno, q.context_lines)
            if excerpt is None:
                continue
            out.append(
                CodeEvidence(
                    repo=target.repo,
                    path=path,
                    line_start=l1,
                    line_end=l2,
                    commit_sha=treeish,
                    excerpt=excerpt,
                    symbol=_guess_symbol(excerpt, lineno - l1),
                )
            )
        return out

    async def _blob(self, treeish: str, path: str) -> list[str] | None:
        """读快照里的文件内容（按行）。失败返回 None——丢这一条证据，不废整轮。"""
        key = (treeish, path)
        if key in self._blob_cache:
            return self._blob_cache[key]
        result = await self._cli.run("git.show", {"obj": f"{treeish}:{path}"})
        lines = ((result.data if isinstance(result.data, str) else "")).splitlines() if result.ok else None
        if lines is None:
            logger.warning("git show 失败：%s:%s（%s）", treeish[:12], path, result.error_code)
        self._blob_cache[key] = lines
        return lines

    def _parse(
        self, raw: str, q: CodeQuery, target: RepoTarget, search_root: str
    ) -> Iterable[CodeEvidence]:
        snap = q.snapshot
        for line in raw.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # rg 的 JSON Lines 里出现坏行：跳过这一行，不废掉整轮。
                continue
            if obj.get("type") != "match":
                continue
            data = obj.get("data") or {}
            path = _text_of(data.get("path"))
            lineno = data.get("line_number")
            if not path or not isinstance(lineno, int) or lineno <= 0:
                continue

            rel = os.path.relpath(path, snap.root)
            excerpt, l1, l2 = _read_context(path, lineno, q.context_lines)
            if excerpt is None:
                continue
            yield CodeEvidence(
                repo=target.repo,
                path=rel,
                line_start=l1,
                line_end=l2,
                commit_sha=snap.commit_sha,
                excerpt=excerpt,
                symbol=_guess_symbol(excerpt, lineno - l1),
            )


def _glob_ok(path: str, globs: Sequence[str]) -> bool:
    """按 ``!`` 排除模式过滤。

    git grep 没有 rg 的 ``--glob``，排除只能靠 pathspec ``:(exclude)``——那要把
    模式串拼进 argv。改成拿到结果后在进程内筛：同样的效果，少一条不可信数据
    进 argv 的路径。排除是为了证据质量（见 ``_DEFAULT_GLOBS``），不是为了性能，
    所以事后筛完全够。
    """
    for g in globs:
        if not g.startswith("!"):
            continue
        pat = g[1:]
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, pat):
            return False
    return True


def _slice_context(
    lines: Sequence[str], lineno: int, context: int
) -> tuple[str | None, int, int]:
    """从已读好的行列表里切上下文。行号是 1-based。"""
    if lineno > len(lines):
        return None, lineno, lineno
    l1 = max(1, lineno - context)
    l2 = min(len(lines), lineno + context)
    return "\n".join(lines[l1 - 1 : l2]), l1, l2


def _resolve_root(root: str, target: RepoTarget) -> str | None:
    """解析子仓的实际检索路径，并确保它**没有逃出仓库根目录**。

    ``subdir`` 可能来自模型输出或需求字段——即不可信数据。一个 ``../../etc`` 的 subdir 会让 rg 去搜仓库外的文件，
    把结果当作"代码证据"。这里用 realpath 前缀比对挡掉。
    """
    base = os.path.realpath(root)
    if not target.subdir:
        return base
    candidate = os.path.realpath(os.path.join(base, target.subdir))
    if candidate != base and not candidate.startswith(base + os.sep):
        logger.error("子仓路径逃出仓库根目录，拒绝检索：%r", target.subdir)
        return None
    return candidate if os.path.isdir(candidate) else None


def _read_context(path: str, lineno: int, context: int) -> tuple[str | None, int, int]:
    """读 ``lineno`` 周围 ``context`` 行。返回 ``(文本, 起始行, 结束行)``。

    行号是 1-based（与 rg 和编辑器一致），locator 里写的就是这个。
    """
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return None, 0, 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None, 0, 0
    if not lines or lineno > len(lines):
        return None, 0, 0
    l1 = max(1, lineno - context)
    l2 = min(len(lines), lineno + context)
    return "".join(lines[l1 - 1 : l2]).rstrip("\n"), l1, l2


def _guess_symbol(excerpt: str, offset: int) -> str | None:
    """从上下文里猜个函数/类名，纯展示用。

    猜不到就返回 ``None``——``symbol`` 只进卡片的标题位，不参与 locator，
    所以猜错的代价是展示不精确，不影响证据可核验性。
    """
    import re

    pat = re.compile(
        r"^\s*(?:async\s+)?(?:def|class|func|function|fn|public|private|protected)\s+"
        r"([A-Za-z_]\w*)"
    )
    lines = excerpt.splitlines()
    for i in range(min(offset, len(lines) - 1), -1, -1):
        m = pat.match(lines[i])
        if m:
            return m.group(1)
    return None


def _text_of(node: object) -> str:
    """rg 的 path/lines 字段是 ``{"text": ...}`` 或 ``{"bytes": base64}``。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        b = node.get("bytes")
        if isinstance(b, str):
            import base64

            try:
                return base64.b64decode(b).decode("utf-8", errors="replace")
            except Exception:
                return ""
    return ""


__all__ = ["RepoSearch"]
