"""提示词资产。**随代码走的固定文本**（§3.2）。

与 ``skills/`` 的分界很明确：``skills/`` 下的规则集可被 Loop-1 提案修改，
这里的提示词不行。原因是提示词直接决定语义节点的输出结构，改错一个字就可能让
schema 校验全面失败（表现为所有检测 ``completed=False``），这种风险不该放进
自动优化回路。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """读一份提示词。名字不含 ``.md``。

    只允许纯标识符名：这个函数的入参最终会拼进路径，虽然目前全是代码里写死的
    常量，但拒绝 ``..`` 和分隔符是零成本的，而漏掉它的代价是路径穿越。
    """
    if not name.replace("_", "").isalnum():
        raise ValueError(f"非法提示词名：{name!r}")
    path = _DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"提示词缺失 {path}：{exc}") from exc


__all__ = ["load"]
