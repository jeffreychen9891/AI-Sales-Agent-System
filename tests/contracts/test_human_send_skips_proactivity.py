"""人工发送**不过主动性闸** —— 而这条用例是那次裁定唯一会红的地方。

## 它为什么必须存在

ADR-0143 §一（用户 2026-09-20）裁定：`worker/proactivity.py` 那四道
（活跃窗口 / 再触达上限 / 静默时段 / 退出信号）对**由人发起**的出站消息
一律不生效。代价写明并被接受：半夜发得出去；客户说过「别再发了」之后
也发得出去。

**那条裁定的形状是「不做某件事」，而这种形状最容易被下一个人顺手改掉。**
在出站路径上加一句 `evaluate_proactivity(...)`，看起来永远像是在修一个
漏洞 —— 它能建成、能跑过，而且**没有任何东西会说那是一次裁定的反转**。

所以 ADR 写死了一条前置，逐字：

> 批 1 落地时**同批交一条用例**，钉住「人工发送路径不调用主动性评估」，
> 红的时候指着本条说「这是裁定，不是遗漏」。
> **没有那条用例的批 1 不许合。**

这就是那条用例。

## 判据有两条，缺一不可

1. **那个 handler 里没有任何一处调主动性**（AST，按被调名字）；
2. **它写进 `outbox_messages.command` 的那个字面量里没有 `proactivity` 键**。

第 1 条防「有人加了一句调用」；**第 2 条防一种更安静的走法**：
不调评估，但顺手把一个 `proactivity` 块塞进 `command`。那会让
`proactivity_deadline` 从 `None` 变成一个真实时刻 ——
**于是闸在 dispatcher 那一侧重新生效，而 api 这边一行调用都没有。**

## 它不查什么

不查「这条裁定对不对」。那是 ADR 的事；这里只保证它**没有被悄悄改掉**。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "api"
    / "src"
    / "fastagent_v2"
    / "api"
    / "app.py"
)

#: 出站命令里那个块的键名。**从 worker 那边逐字取，不在这里写第二遍** ——
#: 两处各写一个字符串，改名那天这条用例会安静地失效。
from fastagent_v2.worker.pipeline import PROACTIVITY_COMMAND_KEY  # noqa: E402

#: 任何一个名字出现在那个 handler 里，都说明主动性被重新接上了。
#: **按名字匹配而不是按 import 匹配**：`from ... import evaluate as x`
#: 之后的 `x(...)` 这里看不见，而那一种是刻意绕，不是顺手改。
_PROACTIVITY_NAMES = {
    "evaluate_proactivity",
    "proactivity_knobs",
    "proactivity_deadline",
    "proactivity_window_lifts_at",
    "_proactivity_verdict",
}


def _handler() -> ast.FunctionDef:
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"), filename=str(APP_SOURCE))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "send_message":
            return node
    pytest.fail(
        "app.py 里找不到 `send_message` —— 这条用例守的那条 route 不见了。"
        "若它是被改名而不是被删，改这里的名字并在 ADR-0143 §一 留一句。"
    )


def _called_names(fn: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            found.add(func.attr)
    return found


def _outbox_command_keys(fn: ast.AST) -> set[str] | None:
    """那条 `INSERT INTO outbox_messages` 里 `Jsonb({...})` 的键。

    读不出来返回 `None` —— 而调用方把「读不出来」当成失败，
    因为这条用例的价值全在于它真的看到了那个字面量。
    """

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Jsonb"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        keys = {
            k.value
            for k in node.args[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        # 那一条的特征键：出站命令必有 `audience`。
        if "audience" in keys:
            return keys
    return None


def test_the_human_send_path_does_not_consult_proactivity() -> None:
    """判据一：那个 handler 里一处主动性调用都没有。"""

    used = _called_names(_handler()) & _PROACTIVITY_NAMES
    assert not used, (
        f"`send_message` 调了 {sorted(used)} —— 而 ADR-0143 §一 裁定"
        "人工发送**四道闸全不拦**。\n"
        "这不是一个漏洞：静默时段与退出信号被绕过是**写明并被接受的代价**。\n"
        "要改它，回到 ADR-0143 §一，不要在这里加闸。"
    )


def test_the_queued_command_carries_no_proactivity_block() -> None:
    """判据二：它写进队列的那条命令里没有那个块。

    **这一条防的是比加一句调用更安静的走法**：不调评估，但把一个
    `proactivity` 块塞进 `command` —— `proactivity_deadline` 于是从 `None`
    变成一个真实时刻，闸在 dispatcher 那一侧重新生效，
    **而 api 这边一行调用都没有**。
    """

    keys = _outbox_command_keys(_handler())
    assert keys is not None, (
        "读不出 `send_message` 写进 outbox 的那个命令字面量 —— "
        "本用例因此什么都没验。若那一处改成了变量拼装，判据要跟着改。"
    )
    assert PROACTIVITY_COMMAND_KEY not in keys, (
        f"排进队列的命令里有 `{PROACTIVITY_COMMAND_KEY}` 块。"
        "它会让 dispatcher 的 `proactivity_deadline` 从 `None` 变成一个真实"
        "时刻 —— 闸就这样在另一侧重新生效了。见 ADR-0143 §一。"
    )
    # 顺带钉住它**确实**是一条给客户的文字消息，而不是别的什么。
    assert keys == {"audience", "content"}


def test_the_detector_would_notice_either_way_back() -> None:
    """**守卫自己不能是空的。**

    上面两条今天绿着，而一条绿着的守卫永远不显示它的报错 ——
    它可能是因为「没有违规」绿，也可能是因为**它什么都没检查**。
    这里喂它两份已知有违规的源码，确认两条判据各自认得出来。
    """

    with_call = ast.parse(
        "def send_message():\n"
        "    verdict = evaluate_proactivity(knobs, now)\n"
        "    return verdict\n"
    )
    assert _called_names(with_call) & _PROACTIVITY_NAMES == {"evaluate_proactivity"}

    with_block = ast.parse(
        "def send_message():\n"
        "    connection.execute(sql, (Jsonb({'audience': 'customer',\n"
        "                                    'content': {}, 'proactivity': {}}),))\n"
    )
    keys = _outbox_command_keys(with_block)
    assert keys is not None and PROACTIVITY_COMMAND_KEY in keys

    clean = ast.parse("def send_message():\n    return 1\n")
    assert not (_called_names(clean) & _PROACTIVITY_NAMES)
    assert _outbox_command_keys(clean) is None
