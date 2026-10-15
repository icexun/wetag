"""向量化：跨进程确定性、维度契约、以及"失败要抛出去"。

最重要的一条是
:func:`test_vectors_are_identical_across_processes_with_different_hash_seeds`：
写入快照与查询可能在不同进程里算向量。只要入桶用的是 Python 内置 ``hash()``，
两边的桶分配就因 ``PYTHONHASHSEED`` 随机化而不同——**两个不同的向量空间**，
而余弦照样算得出一个看起来合理的分数。同一进程内 ``hash()`` 本来就稳定，所以这条
用例真的去开子进程，并给两个不同的 ``PYTHONHASHSEED``。

其余几条钉的是同一类"改了不报错"的决定：

* **维度是契约**。混用两个维度不同的 embedder 是静默错误，``dim`` 必须由实现声明，
  网关返回的维度对不上必须留下 WARN。
* **失败向上抛**。降级由调用方决定；本层吞掉异常，调用方就无从知道向量缺失。
* **bigram 与整词**。只取 unigram 的话「提现额度」与「额度提现」完全相同；把英文
  按字母切的话 ``withdraw`` 与它的字母重排完全相同。两者都会让相似度虚高。
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
from typing import Any

import pytest

from app.capability.intelligence.embedding import (
    LEXICAL_DIM,
    GatewayEmbedder,
    LexicalEmbedder,
    build_embedder,
)


def _cos(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if not n:
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(a[i] * b[i] for i in range(n)) / (na * nb)


# --- 跨进程确定性：本文件的核心 ---------------------------------------------

_SUBPROC = """
import asyncio, json, sys
from app.capability.intelligence.embedding import LexicalEmbedder
v = asyncio.run(LexicalEmbedder().embed("提现额度上限从5000调到10000 withdraw"))
json.dump([round(x, 12) for x in v], sys.stdout)
"""


def _embed_in_subprocess(seed: str) -> list[float]:
    out = subprocess.run(
        [sys.executable, "-c", _SUBPROC],
        capture_output=True,
        text=True,
        cwd=".",
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        timeout=120,
        check=True,
    )
    return json.loads(out.stdout)


def test_vectors_are_identical_across_processes_with_different_hash_seeds() -> None:
    """两个 ``PYTHONHASHSEED`` 不同的进程必须算出**逐位相同**的向量。

    这是 ``blake2b`` 那个选择唯一的守门人。换回内置 ``hash()`` 时，模块照样
    import、单元测试照样绿、余弦照样算得出分数——只有这条用例会红。

    断言"逐位相同"而不是"余弦≈1"：桶换了以后余弦本来就会掉到 0 附近，但更要紧的是
    这里要的性质是**同一个向量空间**，而不是"相似"。
    """
    a = _embed_in_subprocess("0")
    b = _embed_in_subprocess("12345")

    assert a and b, "子进程没算出向量"
    assert a == b, "不同 PYTHONHASHSEED 下向量不同：入桶用了进程内随机化的 hash()"


# --- 词法向量的信号 ---------------------------------------------------------


async def test_reworded_requirement_scores_far_above_an_unrelated_one() -> None:
    """改述的同义需求分高，不相关的需求分≈0——语义向量存在的全部理由。

    数值留了很宽的余量：这里断言的是"可区分"，不是某个具体分数。把阈值钉死会让
    分词策略的任何合理调整都变成假红。
    """
    e = LexicalEmbedder()
    q = await e.embed("提现额度上限从5000调到10000")
    same = await e.embed("调整提现单笔额度上限")
    other = await e.embed("首页banner改版")

    assert _cos(q, same) > 0.3, _cos(q, same)
    assert _cos(q, other) < 0.1, _cos(q, other)
    assert _cos(q, same) > _cos(q, other) + 0.3


async def test_bigrams_are_used_not_just_single_characters() -> None:
    """「提现额度」与「额度提现」必须不同。

    两者的字集完全相同，只有 bigram 能区分。忘了 bigram 的实现会让这两个向量**完全
    相等**（余弦 1.0），于是任何共用字的中文标题都互相"高度相似"——向量退化成
    随机噪音发生器。
    """
    e = LexicalEmbedder()
    a = await e.embed("提现额度")
    b = await e.embed("额度提现")

    assert _cos(a, b) < 0.99, f"字序不同却算出同一个向量：cos={_cos(a, b)}"


async def test_latin_words_enter_as_whole_words_not_letters() -> None:
    """英文按整词入桶，不按字母切。

    ``withdraw`` 与它的字母重排若算出同一个向量，说明切到了字母级。代码标识符
    （``withdraw`` / ``cash_out``）是跨端同义最强的线索之一，按字母切等于把这条
    线索换成了"英文字母频率相似度"。
    """
    e = LexicalEmbedder()
    a = await e.embed("withdraw")
    b = await e.embed("wardhtiw")

    assert _cos(a, b) == pytest.approx(0.0), f"英文被切成字母了：cos={_cos(a, b)}"


async def test_a_single_shared_character_still_scores_above_zero() -> None:
    """「提现」与「取现」只共用一个「现」字，相似度必须 > 0。

    这是 unigram 买到的唯一东西，也是向量最想接住的那一半问题——**换词**的改述。
    只取 bigram 的实现会让这两个向量正交（两者没有任何共同二字组），于是"同一个模块
    在群里叫提现、文档里叫取现"被当成毫不相关。

    顺带钉住另一件事：只取 bigram 时单字标题连一个 token 都切不出来，``embed("现")``
    会返回 ``[]``——一整类短标题静默拿不到向量。
    """
    e = LexicalEmbedder()
    assert _cos(await e.embed("提现"), await e.embed("取现")) > 0.0
    assert await e.embed("现") != [], "单字文本切不出 token：unigram 没了"


async def test_fully_cancelling_buckets_return_empty_not_a_zero_vector() -> None:
    """桶内符号两两抵消时返回 ``[]``，不是全零向量。

    带符号入桶让这件事变得可能（模块里写的"罕见但可能"）。返回全零向量的结果虽然
    一样（余弦在模长为 0 时给 0.0），但代价不一样：调用方 ``if not v`` 的短路会失效，
    白跑一趟全表比较，只为给每一行算一个必然是 0 的分数。

    ``dim=1`` 把"同桶"变成必然，于是这条罕见分支可以被确定性地构造出来：``b`` 与
    ``a`` 在 ``blake2b`` 下符号相反。
    """
    assert await LexicalEmbedder(dim=1).embed("b a") == []


async def test_latin_case_is_folded() -> None:
    """``Withdraw`` 与 ``withdraw`` 必须落同一个桶。

    文档标题里写 ``Withdraw``、代码里写 ``withdraw`` 是常态。不折大小写就是两个桶，
    于是这条最强的跨端同义线索在最常见的写法差异上直接失效——而失效是静默的。
    """
    e = LexicalEmbedder()
    assert await e.embed("Withdraw 额度") == await e.embed("withdraw 额度")
    assert _cos(await e.embed("WITHDRAW"), await e.embed("withdraw")) == pytest.approx(1.0)


async def test_signed_hashing_produces_negative_components() -> None:
    """入桶带符号：向量里必须出现负分量。

    不带符号时碰撞只会一律叠加，把所有向量一起推高，余弦普遍偏大——表现为"什么都
    有点像"，而相似度的取舍恰恰依赖分数的区分度。这条断言是那半行 ``sign`` 的唯一
    守门人。
    """
    v = await LexicalEmbedder().embed("提现额度上限调整 withdraw limit cash_out 账务")
    assert any(x < 0 for x in v), "没有负分量：入桶没带符号"


async def test_vector_is_l2_normalised() -> None:
    """模长为 1。

    余弦计算自己会归一，所以这条不是为了算得对，是为了**可比**：索引侧存的是
    归一向量，长文本和短文本才不会因为词多就系统性地占优。
    """
    for text in ("提现", "提现额度上限从5000调到10000，涉及iOS与Android两端，需灰度"):
        v = await LexicalEmbedder().embed(text)
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)


# --- 维度契约 ---------------------------------------------------------------


async def test_dim_is_declared_and_honoured() -> None:
    """``dim`` 是声明出来的契约，产出必须恰好这么长。

    调用方用长度判断"是否同一个向量空间"。产出长度随文本变动的实现会让同一个
    embedder 的向量互相不可比，而症状只是"相似的找不到"。
    """
    e = LexicalEmbedder(dim=64)
    assert e.dim == 64
    for text in ("提现", "a", "提现额度上限从5000调到10000 withdraw cash_out 账务 支付"):
        assert len(await e.embed(text)) == 64
    assert LexicalEmbedder().dim == LEXICAL_DIM


def test_nonpositive_dim_is_rejected_at_construction() -> None:
    """``dim<=0`` 直接拒绝，不留到运行时。

    ``dim=0`` 的实现每次都返回 ``[]``，而 ``[]`` 是合法的"没有向量"信号——于是
    一个配错的维度会表现成"向量静默缺失"，而不是一条报错。
    """
    for bad in (0, -1):
        with pytest.raises(ValueError):
            LexicalEmbedder(dim=bad)


# --- 空输入 -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\t\n", "！？。，", "###"])
async def test_text_without_any_token_returns_empty_not_a_zero_vector(text: str) -> None:
    """切不出词的文本返回 ``[]``，不是全零向量。

    全零向量会一路走到余弦计算，在那里因模长为 0 返回 0.0——同样的结果，却白跑
    一趟全表比较。调用方 ``if not v`` 的短路只认 ``[]``。
    """
    assert await LexicalEmbedder().embed(text) == []


# --- 网关实现 ---------------------------------------------------------------


class _FakeEmbeddings:
    def __init__(self, dim: int = 4, *, boom: Exception | None = None) -> None:
        self._dim = dim
        self._boom = boom
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._boom is not None:
            raise self._boom

        class _R:
            data = [type("D", (), {"embedding": [0.5] * self._dim})()]

        return _R()


class _FakeClient:
    def __init__(self, **kw: Any) -> None:
        self.embeddings = _FakeEmbeddings(**kw)


class _CountingSem:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *a: Any) -> None:
        return None


async def test_gateway_sends_the_configured_model_and_the_text() -> None:
    c = _FakeClient(dim=4)
    out = await GatewayEmbedder(c, model="text-embedding-v4", dim=4).embed("提现")

    assert out == [0.5, 0.5, 0.5, 0.5]
    assert c.embeddings.calls == [{"model": "text-embedding-v4", "input": "提现"}]


async def test_gateway_failure_propagates_instead_of_degrading_silently() -> None:
    """网关报错**向上抛**。

    API_KEY 对 embedding 无权时真实返回就是 401。在这里吞掉的话，调用方无从知道
    向量缺失，降级说明永远不会出现。
    """
    c = _FakeClient(boom=RuntimeError("401 Unauthorized token for requested model"))
    with pytest.raises(RuntimeError, match="Unauthorized"):
        await GatewayEmbedder(c, model="text-embedding-v4", dim=1024).embed("提现")


async def test_gateway_warns_when_the_returned_dimension_contradicts_config() -> None:
    """返回维度与声明不符 ⇒ 留下 WARN。

    静默接受的话索引里会混进两种维度的行，而余弦截断不报错——症状是相似度
    打回一堆噪音，没有任何线索指向那行配错的 ``EMBEDDING_DIM``。
    """
    c = _FakeClient(dim=8)
    with caplog_at("app.capability.intelligence.embedding") as records:
        out = await GatewayEmbedder(c, model="m", dim=1024).embed("提现")

    assert len(out) == 8, "WARN 之外不该篡改返回值"
    assert any("1024" in r.getMessage() and "8" in r.getMessage() for r in records), [
        r.getMessage() for r in records
    ]


async def test_gateway_blank_text_costs_nothing() -> None:
    """空白文本不发请求。每次调用都是网关额度与用户等待。"""
    c = _FakeClient()
    assert await GatewayEmbedder(c, model="m", dim=4).embed("   ") == []
    assert c.embeddings.calls == []


async def test_gateway_holds_the_shared_semaphore_for_the_call() -> None:
    """走信号量。

    embedding 和 chat 打的是同一个网关、同一份额度（``agents/factory.py`` 纪律 1）。
    绕开信号量等于没有上限，而额度打爆的后果不是慢——是**所有**语义节点一起失败，
    每个任务都变成 ``completed=False``。
    """
    sem = _CountingSem()
    await GatewayEmbedder(_FakeClient(), model="m", dim=4, semaphore=sem).embed("提现")
    assert sem.entered == 1


# --- 装配选择 ---------------------------------------------------------------


@pytest.mark.parametrize("model", [None, "", "   "])
def test_build_falls_back_to_lexical_and_never_returns_none(model: str | None) -> None:
    """没配模型名就退到词法向量，**不返回 None**。

    返回 None 会让向量静默缺失，而"没有向量"在链路尽头长得和"没有相似项"一模一样。
    退化是允许的，但退化必须有**形状**——装配报告里那个 ``lexical_ngram``。
    """
    e = build_embedder(model=model, dim=1024, timeout_s=20.0)
    assert e is not None
    assert isinstance(e, LexicalEmbedder)
    assert e.name == "lexical_ngram"


def test_build_picks_the_gateway_when_a_model_is_configured() -> None:
    """配了模型名就走网关，且 ``name`` 带上模型名。

    ``name`` 进 ``PortStatus.impl``，是覆盖范围里"这次用的是哪种向量"的唯一凭据。
    两种实现共用一个名字，装配报告就又替一条自己不了解的路作保了（d739947）。
    """
    e = build_embedder(model=" text-embedding-v4 ", dim=1024, timeout_s=5.0)
    assert isinstance(e, GatewayEmbedder)
    assert e.dim == 1024
    assert e.name == "gateway:text-embedding-v4", e.name


def test_the_two_implementations_do_not_share_a_name() -> None:
    """两种向量的 ``name`` 必须可区分——覆盖范围靠它讲真话。"""
    assert LexicalEmbedder().name != GatewayEmbedder(
        _FakeClient(), model="m", dim=4
    ).name


# --- Protocol 契约 ----------------------------------------------------------


def test_both_satisfy_the_embedder_protocol() -> None:
    """两个实现都满足 ``Embedder`` 协议，签名漂移在这里停住。"""
    from app.capability.intelligence.embedding import Embedder

    assert isinstance(LexicalEmbedder(), Embedder)
    assert isinstance(GatewayEmbedder(_FakeClient(), model="m", dim=4), Embedder)


# --- 工具 -------------------------------------------------------------------


class caplog_at:
    """极小的 caplog 替身：只收指定 logger 的记录。

    用它而不是 pytest 的 ``caplog`` fixture，是因为这里要断言 WARN 的**内容**，而
    ``caplog`` 在 ``-p no:logging`` 等配置下行为不一致；自己挂 handler 没有歧义。
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)
        self._records: list[logging.LogRecord] = []

    def __enter__(self) -> list[logging.LogRecord]:
        records = self._records

        class _H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        self._h = _H()
        self._logger.addHandler(self._h)
        self._prev = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        return records

    def __exit__(self, *a: Any) -> None:
        self._logger.removeHandler(self._h)
        self._logger.setLevel(self._prev)
