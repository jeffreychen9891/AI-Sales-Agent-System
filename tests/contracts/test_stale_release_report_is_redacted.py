"""那个前置检查的输出里**不许出现商家正文** —— 而它曾经出现过。

## 这份文件的来由是一次真实泄露（2026-09-16）

`scripts/check_stored_releases.py` 的 docstring 写着
「**不打正文是有意的**：release 里有商家的话术，而这个脚本的输出多半会被贴进部署记录」，
**而同一份文件里那一行打的是 `error.message[:160]`**。

jsonschema 在 `maxLength` 上的消息体**就是那个超长的值本身**
（`'<整段话术>' is too long`），于是那次真跑把两行 FAIL 各约 160 字
商家 goal 原文打进了部署记录。**截断救不了**：被截掉的是尾巴，留下的是正文。

**一句写在 docstring 里的承诺，和一行做相反事情的代码** ——
而本仓对这种组合有一条硬规则（`CLAUDE.md` 第二节）：断言要配一条会红的守卫。
这份文件就是那条守卫。

## 它为什么逐个关键字地试

泄露不是 `maxLength` 独有的：`enum` / `type` / `pattern` / `const` 的消息体
**同样带着被拒的那个值**。所以这里不挑一个关键字试，
**而是把契约里真实会触发的那几类各来一次**，每一次都问同一个问题：
**那段哨兵字符串出现在输出里了吗。**

## 判据是「哨兵不出现」+「话还说得出来」

只验第一半会得到一条把输出改成空字符串就能过的用例 ——
而那样的话，拿着 URL 的人看到一行 `FAIL <租户> <id>@<版本>` 却不知道哪儿错了。
所以同时验它**仍然指得出是哪个关键字、哪一格**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_stored_releases.py"

#: 一段**只可能来自商家配包**的字符串。用中文长句而不是 `secret` 这种词：
#: 真正会泄露的东西长这样，而短哨兵可能被某个 `[:160]` 恰好截掉，
#: 于是一条本该红的用例照绿。
SENTINEL = "先让他说出一件团队正在发生的损耗，再讲对应的能力，最后才碰疑虑，他没提就不要主动提"


def _module() -> Any:
    if "check_stored_releases" in sys.modules:
        return sys.modules["check_stored_releases"]
    spec = importlib.util.spec_from_file_location("check_stored_releases", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_stored_releases"] = module
    spec.loader.exec_module(module)
    return module


#: `(关键字, schema, 会触发它的值)` —— 每一条的值里都埋着哨兵。
CASES = [
    ("maxLength", {"type": "string", "maxLength": 10}, SENTINEL),
    ("enum", {"enum": ["same_turn", "awaiting_customer"]}, SENTINEL),
    ("type", {"type": "integer"}, SENTINEL),
    ("pattern", {"type": "string", "pattern": "^[a-z]+$"}, SENTINEL),
    ("const", {"const": "fixed"}, SENTINEL),
    (
        "additionalProperties",
        {"type": "object", "additionalProperties": False, "properties": {}},
        {SENTINEL: "x"},
    ),
]


@pytest.mark.parametrize(("keyword", "schema", "value"), CASES, ids=[c[0] for c in CASES])
def test_no_keyword_leaks_the_offending_value(keyword: str, schema: dict, value: Any) -> None:
    """**六个关键字，一个都不许把被拒的那个值打出来。**

    这一条在修复之前是红的（实测 2026-09-16）：`maxLength` 那一格原样打出了
    整段哨兵。它红的时候要读作「输出里有商家正文」，而不是「格式变了」。
    """

    module = _module()
    errors = list(Draft202012Validator(schema).iter_errors(value))
    assert errors, f"{keyword}：这个输入没有触发校验失败，用例的前提就不成立"

    described = module.describe(errors[0])

    assert SENTINEL not in described, (
        f"{keyword} 那一格把商家正文打出来了：{described!r}。"
        " 这个脚本的输出会被贴进部署记录 —— 它的 docstring 写着不打正文。"
    )
    # **不是「不含完整哨兵就算过」**：截断过的正文仍然是正文，
    # 而第一版的缺陷恰恰是 `[:160]`。任何一段连续 12 字都不许出现。
    for start in range(0, len(SENTINEL) - 12):
        fragment = SENTINEL[start : start + 12]
        assert fragment not in described, (
            f"{keyword} 那一格打出了正文的一段：{fragment!r} 在 {described!r} 里"
        )


@pytest.mark.parametrize(("keyword", "schema", "value"), CASES, ids=[c[0] for c in CASES])
def test_it_still_says_enough_to_act_on(keyword: str, schema: dict, value: Any) -> None:
    """**脱敏不等于沉默** —— 它仍然要说得出是哪个关键字。

    只验「哨兵不出现」的话，把输出改成空串也能过，
    而那时拿着 URL 的人看到一行 `FAIL` 却不知道哪儿错了。
    """

    module = _module()
    errors = list(Draft202012Validator(schema).iter_errors(value))
    described = module.describe(errors[0])

    assert keyword in described, f"输出里没说是哪个关键字：{described!r}"
    assert len(described) >= len(keyword) + 4, f"输出短得说明不了任何事：{described!r}"


def test_the_promise_and_the_code_are_in_the_same_file() -> None:
    """**那句承诺还在 docstring 里，而它现在有执行者了。**

    这一条钉的是「两句话不能同时为真」那个形状本身：
    docstring 里那句「不打正文」与代码里那行 `error.message` 曾经并存了一整批。

    判据是**代码侧**不再出现 `error.message`（那是唯一确定带正文的那个字段），
    而不是「docstring 里有没有那句话」—— 后者是文本扫描，换个写法就看不见。
    """

    source = SCRIPT.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # docstring 里会引用这个名字来讲那次泄露，所以只看**赋值/取值**的写法。
    assert "error.message" not in body.replace("`error.message`", "").replace(
        "`error.message[:160]`", ""
    ), "`error.message` 又回到代码里了 —— 它就是那个带正文的字段"
