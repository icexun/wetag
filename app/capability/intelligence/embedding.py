"""文本向量化：文档快照的 ``embedding`` 列。

两个实现：

* :class:`GatewayEmbedder`——OpenAI 兼容端点，语义向量，配了 ``EMBEDDING_MODEL`` 才用；
* :class:`LexicalEmbedder`——字符 n-gram 词法向量，不联网、确定性可复现。它捞得回
  共享词面的改述，捞不回换词的改述，所以装配报告里 ``impl`` 写明 ``lexical_ngram``。

**用 blake2b 而不是内置 ``hash()``**：内置哈希按 ``PYTHONHASHSEED`` 每进程随机化，
写入方与查询方会落在两个不同的向量空间，且余弦照样算得出看似合理的分数。

**``dim`` 是契约的一部分**：混用两个维度不同的 embedder 是静默错误，调用方比较前
必须先对齐维度；本模块只保证"我产出的向量恒为 ``dim`` 维"。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

# 词法向量的维度。512 桶对上百字的标题（约数十个 n-gram）碰撞率可接受，且
# ``real[]`` 里每行多存 512 个 float 在百条量级上无关紧要。
LEXICAL_DIM = 512

# 连续 CJK 片段：在其中取 unigram + bigram。
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
# 拉丁/数字词：整词入桶（``withdraw`` 不该被切成字母）。
_WORD = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> list[str]:
    """切出用于入桶的词法单元。

    中文按字切，并额外取相邻二字组：bigram 才是"提现"这类词的载体，只有 unigram
    的话「提现额度」和「额度提升」会因为共用 提/额/度 而虚高。英文整词入桶——代码
    里的标识符（``withdraw``、``cash_out``）是最强的跨端同义线索之一。
    """
    out: list[str] = []
    for run in _CJK.findall(text):
        out.extend(run)
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(m.group(0).lower() for m in _WORD.finditer(text))
    return out


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """把词映射到 ``(桶号, 符号)``。

    带符号哈希（signed hashing / hashing trick）：让碰撞的两个词有一半概率互相
    抵消而不是一律叠加。不带符号时，桶越少碰撞越倾向于把所有向量一起推高，余弦
    普遍偏大——表现为"什么都有点像"，相似度失去区分度。
    """
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    return n % dim, 1.0 if (n >> 63) & 1 else -1.0


@runtime_checkable
class Embedder(Protocol):
    name: str

    async def embed(self, text: str) -> Sequence[float]: ...


class LexicalEmbedder:
    """离线确定性的字符 n-gram 向量。满足 ``Embedder`` 协议。

    它**不是语义向量**，见模块文档串「词法向量不是语义向量」。装配时请把
    ``name`` 放进 ``PortStatus.impl``，别让它冒充语义向量。
    """

    name = "lexical_ngram"

    def __init__(self, *, dim: int = LEXICAL_DIM) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正：{dim}")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        """返回 L2 归一化后的 ``dim`` 维向量；空文本返回 ``[]``。

        空文本返回 ``[]`` 而不是全零向量：调用方可据空值直接短路，不必做无意义的比较。
        """
        toks = _tokens(text or "")
        if not toks:
            return []
        vec = [0.0] * self._dim
        for t in toks:
            i, sign = _bucket(t, self._dim)
            vec[i] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            # 罕见但可能：碰撞的词恰好符号相反、两两抵消。此时没有可用信号，
            # 返回 [] 走短路，而不是把一个零向量交给余弦。
            return []
        return [x / norm for x in vec]


class GatewayEmbedder:
    """走 OpenAI 兼容端点的远程向量化。满足 ``Embedder`` 协议。

    今天这把 ``API_KEY`` 对 embedding 无权（见模块文档串），所以它默认**不装配**，
    只在显式配了 ``EMBEDDING_MODEL`` 时才由 :func:`build_embedder` 选中。等权限下来
    改一行配置即可，不必改代码——这是它今天就存在的理由。
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        dim: int,
        semaphore: Any = None,
    ) -> None:
        self._client = client
        self._model = model
        self._dim = dim
        self._sem = semaphore

    @property
    def name(self) -> str:
        return f"gateway:{self._model}"

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        """一次调用一条文本。失败**向上抛**。

        不在这里降级成 ``[]``：要不要因向量失败影响结果，由调用方决定（如
        ``refresh_index`` 记日志后以无向量落库）。本层吞掉，上层就无从得知。
        """
        if not (text or "").strip():
            return []
        if self._sem is not None:
            async with self._sem:
                vec = await self._call(text)
        else:
            vec = await self._call(text)
        if len(vec) != self._dim:
            # 维度与声明不符就说出来。静默接受的话，索引里会混进两种维度的行，
            # 而余弦截断不报错——症状是相似度变成噪音，没有任何日志。
            logger.warning(
                "网关返回 %d 维，与声明的 %d 维不符（model=%s）："
                "索引里若已有另一维度的行，相似度将不可比",
                len(vec),
                self._dim,
                self._model,
            )
        return vec

    async def _call(self, text: str) -> list[float]:
        resp = await self._client.embeddings.create(model=self._model, input=text)
        return [float(x) for x in resp.data[0].embedding]


def build_embedder(*, model: str | None, dim: int, timeout_s: float) -> Any:
    """按配置选一个 embedder；**永不返回 None**。

    配了 ``EMBEDDING_MODEL`` 就走网关，没配就退到词法——退化的**形状**由装配报告写明
    （调用方把 ``embedder.name`` 放进 ``PortStatus.impl``）。
    """
    if not (model or "").strip():
        return LexicalEmbedder()
    from openai import AsyncOpenAI

    from app.capability.agents.factory import llm_semaphore
    from config.properties import ModelProperties

    mp = ModelProperties.instance()
    client = AsyncOpenAI(
        api_key=mp.api_key or "default",
        base_url=mp.base_url or None,
        timeout=timeout_s,
    )
    # 与语义节点共用 ``LLM_MAX_CONCURRENCY``：embedding 和 chat 打的是同一个网关、
    # 同一份额度，各建信号量等于没有上限（见 ``agents/factory.py`` 的纪律 1）。
    #
    # 代价是一条**嵌套死锁**的隐患：若将来有人在某个语义节点持有信号量期间去
    # embed，两者互等即全进程挂住。今天唯一的 embed 点（doc_update.refresh_index）
    # 在 chat 之外——但下一个想在 chat 里做向量检索的人会踩到，故记在此处。
    return GatewayEmbedder(
        client, model=model.strip(), dim=dim, semaphore=llm_semaphore()
    )


__all__ = ["LEXICAL_DIM", "Embedder", "GatewayEmbedder", "LexicalEmbedder", "build_embedder"]
