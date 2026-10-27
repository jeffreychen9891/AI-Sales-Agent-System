"""`goal` 的长度上限写在两份契约里，而它是**一个数**（2026-09-22）。

## 这条守卫是被一次真实的分家逼出来的

`target_frames[].goal`（配包侧，admission 收包时校验）与
`target_stack[].goal`（聚合侧，`pipeline` 写帧时校验）**是同一个字符串的两次校验** ——
`pipeline._initial_aggregate` 那一行是 `str(definition["goal"])`，原样抄、不截断。
两份契约 2026-09-15（批 2，`2c9a9410`）同时加界，同时是 1,200。

**2026-09-20（`5b17f16f`）把配包侧抬到 1,500，而聚合侧留在 1,200。**
后果正是配包侧那段 description 自己写下来防的那件事的镜像：

> bounding only `conversation-aggregate` would let admission accept an
> oversized package and fail later, when a frame is written.

演示配包的 `introduce` goal 自拆帧那批（`3d04ebf7`，984 → 1,380 字）起就超过 1,200，
于是**每一次开新会话**都在 `pipeline.py` 的
`validate_contract("conversation-aggregate", ...)` 抛 `ValidationError`。
**而门禁 3,925 条全绿**，因为全仓没有任何一条用例把在架配包送进 `_initial_aggregate`
—— 唯一走那条路的 `test_contracts.py::profile()` 读的是 `synthetic-third`，
它的第一帧 goal 是 54 字。

## 所以这里是两条，缺一条都挡不住下一次

1. `test_the_two_schemas_declare_the_same_goal_bound` —— **两个数必须相等**。
   它挡的是「只抬一边」这个动作本身，**在任何配包超限之前就红**；
2. `test_every_shipped_package_can_open_a_conversation` —— **在架配包真的走一遍那条路**。
   它挡的是「两个数都对，而某一份包超了」，也是今天这个洞唯一的直接证人。

第 1 条红的时候第 2 条可能还是绿的（没有配包用到那段区间），
第 2 条红的时候第 1 条可能也是绿的（两边同数而包超了）——
**两条各挡一半，不是一条的两种写法。**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.worker.pipeline import _initial_aggregate
from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "packages" / "contracts" / "schemas"
PROFILES = ROOT / "profiles"

TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROFILE_RELEASE = UUID("11111111-1111-4111-8111-111111111111")


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _source_bound() -> int:
    """配包侧：admission 收包时校验的那一个。"""

    schema = _schema("merchant-profile")
    return schema["properties"]["target_frames"]["items"]["properties"]["goal"]["maxLength"]


def _runtime_bound() -> int:
    """聚合侧：`pipeline` 写帧时校验的那一个。"""

    schema = _schema("conversation-aggregate")
    return schema["properties"]["target_stack"]["items"]["properties"]["goal"]["maxLength"]


def _packages() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for directory in sorted(PROFILES.iterdir()):
        path = directory / "profile.json"
        if path.exists():
            out[directory.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def test_the_two_schemas_declare_the_same_goal_bound() -> None:
    """**抬一边就要抬另一边。**

    报错把**两个角色**分别念出来，而不是说「两处不一致」 ——
    读到它的人要知道哪一边是收包的闸、哪一边是写帧的闸，
    因为分家之后先撞上的永远是写帧那一边，而它离改动最远。
    """

    source = _source_bound()
    runtime = _runtime_bound()
    assert source == runtime, (
        f"goal 的长度上限分家了：配包侧（`merchant-profile.target_frames[].goal`，"
        f"admission 收包时校验）是 {source}，"
        f"聚合侧（`conversation-aggregate.target_stack[].goal`，"
        f"`pipeline._initial_aggregate` 写帧时校验）是 {runtime}。"
        f"\n它们是同一个字符串的两次校验，必须是同一个数。"
        f"\n成因（复数，不排他）：只抬了源头那一半而没抬聚合那一半（2026-09-20 就是这样）；"
        f"只收紧了聚合那一半而没收紧源头。"
        f"\n两边不同数时，配包会通过 admission 而在开新会话时抛 ValidationError —— "
        f"那条路上没有别的东西会先说话。"
    )


def test_the_bound_is_actually_enforced_on_the_write_path() -> None:
    """**上一条只比两个数；这一条证明那个数在写帧那条路上真的被执行。**

    两个数可以一致地错 —— 契约里写着 1,500，而校验器没跑，
    上一条照样绿（`AGENTS.md`：唯一定义点要配一条不依赖它的检查）。
    所以这里造一个**恰好超一个字**的 goal，要求它被拒，
    **并且要求理由码指向 `goal` 那一项**，不是随便红一下。
    """

    bound = _runtime_bound()
    document = _packages()["synthetic-third"]
    document["target_frames"][0]["goal"] = "目" * (bound + 1)

    aggregate = _initial_aggregate(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        profile_release_id=PROFILE_RELEASE,
        profile=document,
    )

    with pytest.raises(ValidationError) as refused:
        validate_contract("conversation-aggregate", aggregate)

    assert refused.value.validator == "maxLength", (
        f"拒是拒了，而拒它的不是长度那一项，是 {refused.value.validator!r} —— "
        f"这条用例要的是「超一个字就被拒」，红在别的项上证明不了它。"
    )
    assert list(refused.value.absolute_path)[:1] == ["target_stack"], (
        f"红在 {list(refused.value.absolute_path)} —— 不是 target_stack 那一支。"
    )

    document["target_frames"][0]["goal"] = "目" * bound
    on_the_line = _initial_aggregate(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        profile_release_id=PROFILE_RELEASE,
        profile=document,
    )
    validate_contract("conversation-aggregate", on_the_line)


@pytest.mark.parametrize("name", sorted(_packages()))
def test_every_shipped_package_can_open_a_conversation(name: str) -> None:
    """**在架的每一份配包，都要能走通「开新会话」那一步。**

    这一条喂的不是自造夹具，是 `profiles/` 下真的那几份 ——
    `AGENTS.md`「那条用例喂的东西必须是生产里会出现的形状」。
    走的也是生产那两行：`_initial_aggregate`（`pipeline.py:1538`）
    造聚合，`validate_contract`（`pipeline.py:2846` 那一行）校验它。

    **删掉这一条，2026-09-22 那个洞就没有任何证人** ——
    在架配包超出聚合契约上限时，门禁不会有任何反应。
    """

    document = _packages()[name]
    first = document["target_frames"][0]

    aggregate = _initial_aggregate(
        tenant_id=TENANT,
        conversation_id=uuid4(),
        profile_release_id=PROFILE_RELEASE,
        profile=document,
    )

    # 先确认这条路真的把 goal 搬过去了 —— 否则下面那句校验是在验一个空字段，
    # 而一个恒空的字段永远不会超限（`AGENTS.md`：它等的那个条件根本不会发生）。
    assert aggregate["target_stack"][0]["goal"] == first["goal"], (
        f"{name}：`_initial_aggregate` 没有把第一帧的 goal 原样搬进栈 —— "
        f"本条下面那句校验因此验不到 goal 的长度。"
    )

    try:
        validate_contract("conversation-aggregate", aggregate)
    except ValidationError as refused:
        pytest.fail(
            f"在架配包 `{name}` 开不了新会话：`pipeline.py` 写聚合那一行会抛 —— "
            f"{refused.message}\n"
            f"第一帧是 `{first['id']}`，goal {len(first['goal'])} 字，"
            f"聚合契约上限 {_runtime_bound()}，配包契约上限 {_source_bound()}。\n"
            f"成因（复数，不排他）：两份契约的上限分家了（先看上面那条钉死相等的用例）；"
            f"配包的 goal 越过了两边共同的上限（那就该削 goal 或由用户裁定抬上限）。"
        )
