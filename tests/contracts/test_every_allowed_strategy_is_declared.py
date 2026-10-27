"""帧允许的每一个策略，`strategies` 里都得真的有它（运维缺陷 13，2026-09-23）。

## 它补的是一条**被点名说「不是本条」、而那条本条不存在**的检查

`registry._check_strategy_required_slots` 的 docstring 逐字：

> 一个策略可以被多个帧允许，而槽位是**帧**的属性 …… 那个帧本来就不该允许它，
> **而那是另一条检查（`allowed_strategy_set` 的语义），不是本条。**

**那条「另一条检查」在 2026-09-23 之前不存在。**

## 而它漏掉的东西，与它自己描述的症状逐字相同

同一段 docstring：

> 在帧里永远不存在，于是永远为空，于是**那条策略从此每一轮都被拒** ——
> 而拒绝的理由码……**指向模型，不指向那个拼错的字**。
> 「永久被拒 + 错误信息指向错误的原因」是最坏的组合。

stage 实测：演示包三个帧的 `allowed_strategy_set` 里都有 `offer_material`，
而 `strategies` 里**根本没有它**。于是：

```
"reason_code": "STRATEGY_NOT_IN_PROFILE_REGISTRY",
"detail": "'offer_material' is not registered by the profile",
"fallback_strategy": "ask_clarification"
```

整通对话 `send_material` 意图 **0 个**、非文本出站 **0 行** ——
**「发资料」这条功能从上线起一次都没生效过**，而客户收到的是一句多余的确认。

## ⚠️ 为什么原来那条检查自己抓不到

它的循环是 `for name, body in strategies.items()` ——
**一个只出现在 `allowed_strategy_set`、从未出现在 `strategies` 的名字，
它一次都不会访问到。** 遍历方向决定了它看不见这一类。

## 契约对它没有任何约束

`allowed_strategy_set` 是 `{"type": "array", "items": {"type": "string"}}` ——
**任何字符串都过**。封版期不判，就没有任何地方会判。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_every_allowed_strategy_is_declared,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = sorted((REPOSITORY_ROOT / "profiles").glob("*/profile.json"))
HERE = Path("profiles/internal-demo-sales/profile.json")


def _document() -> dict[str, Any]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.mark.parametrize("path", PACKAGES, ids=lambda p: p.parent.name)
def test_every_shipped_package_declares_what_its_frames_allow(path: Path) -> None:
    """**在架三个包，现在都过得了这一条。**

    2026-09-23 之前 `internal-demo-sales` 过不了 —— 而没有任何东西会说。
    """

    _check_every_allowed_strategy_is_declared(
        json.loads(path.read_text(encoding="utf-8")), path
    )


def test_a_frame_allowing_an_undeclared_strategy_is_refused() -> None:
    """⚠️ **这一条就是 stage 上那次静默失效。**

    报错要点名是哪个帧、哪张表、哪个名字 —— 否则商家拿到的是
    「你的包有问题」，而他要改的是某一行里的一个词。
    """

    document = _document()
    document["target_frames"][0]["allowed_strategy_set"].append("offer_material_typo")

    with pytest.raises(ProfilePackageError) as raised:
        _check_every_allowed_strategy_is_declared(document, HERE)

    message = str(raised.value)
    assert "offer_material_typo" in message, "报里没有那个名字，商家不知道改哪个词"
    assert "introduce" in message, "报里没有帧 id"
    assert "allowed_strategy_set" in message, "报里没说是哪张表"


def test_the_time_rule_table_is_checked_too() -> None:
    """⚠️ **反侧：两张表都点策略名，漏判一张等于漏判一半。**

    时间规则那张漏一个的后果一样：那条定时规则到点之后选不中任何打法。
    没有这一条，一个只查 `allowed_strategy_set` 的实现会让上面那条绿。
    """

    document = _document()
    frame = document["target_frames"][0]
    frame.setdefault("time_rule_strategy_set", []).append("never_declared")

    with pytest.raises(ProfilePackageError) as raised:
        _check_every_allowed_strategy_is_declared(document, HERE)

    assert "time_rule_strategy_set" in str(raised.value)
    assert "never_declared" in str(raised.value)


def test_a_declared_strategy_no_frame_allows_is_not_an_offence() -> None:
    """⚠️ **另一个反侧：多声明一条没人允许的策略，本条不该管。**

    那是「零调用方」那两问的事，而**它的后果与本条相反**：
    一条没人允许的策略选不中，仅此而已；
    而一条被允许却没声明的策略，**每一轮都被拒并把理由指向模型**。

    没有这一条，一个按「两张表必须相等」写的实现会让商家为了过闸
    去删掉一条他可能马上要用的声明。
    """

    document = _document()
    document["strategies"]["declared_but_unused"] = {
        "required_slots": [],
        "allowed_intent_kinds": ["send_message"],
        "generation_constraints": [],
    }

    _check_every_allowed_strategy_is_declared(document, HERE)


def test_offer_material_can_actually_send_a_file() -> None:
    """⚠️ **补定义时差点补出一条仍然发不了文件的策略。**

    `synthetic-carrier` 里有现成的 `offer_material`，而它只允许 `send_message` ——
    照抄它就会让这条策略「存在」但**仍然发不出任何文件**，
    而症状与修之前一模一样：客户问要资料，收到的是一句话。

    真正发文件那条路是 `send_material`（`pipeline.GENERATION_WRITE_PATHS`）。
    **这条用例钉的是「这个定义真的能发文件」，不是「这个名字存在」。**
    """

    strategy = _document()["strategies"]["offer_material"]
    assert "send_material" in strategy["allowed_intent_kinds"], (
        "`offer_material` 不允许 `send_material` —— 它存在，但发不出文件"
    )


def test_the_check_runs_before_the_required_slots_one() -> None:
    """**顺序是承重的。**

    反过来的话，一个根本没声明的策略会在 `_check_strategy_required_slots` 里
    走到「没有任何帧允许它 → 跳过不判」那一支 —— **两条都不报**，
    而那正是 2026-09-23 那次静默失效的形状。
    """

    source = (
        REPOSITORY_ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
    ).read_text(encoding="utf-8")
    mine = source.index("    _check_every_allowed_strategy_is_declared(document, path)")
    theirs = source.index("    _check_strategy_required_slots(document, path)")
    assert mine < theirs, "本条跑在必填槽位那条之后了 —— 顺序反了，两条都会不报"


def test_the_contract_still_lets_any_string_through() -> None:
    """**留痕：为什么这条必须在封版期判，而不是交给契约。**

    `allowed_strategy_set` 的 schema 今天是 `{"type": "array", "items":
    {"type": "string"}}` —— 任何字符串都过。这条用例钉住那个事实，
    **好让下一个人知道「加个 enum 就行」为什么做不到**：
    策略名是**商家自己的**词表，平台列不出它的合法取值。
    """

    schema = json.loads(
        (
            REPOSITORY_ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
        ).read_text(encoding="utf-8")
    )
    field = schema["properties"]["target_frames"]["items"]["properties"][
        "allowed_strategy_set"
    ]
    assert field["items"] == {"type": "string"}, (
        "契约收紧了 —— 那这条封版期检查的理由要重写"
    )
    assert "enum" not in field["items"]


def test_the_healthy_package_is_not_mutated_by_the_check() -> None:
    """检查不许改它读的东西 —— 它跑在封版路径上，改了就写进了包。"""

    document = _document()
    before = deepcopy(document)
    _check_every_allowed_strategy_is_declared(document, HERE)
    assert document == before
