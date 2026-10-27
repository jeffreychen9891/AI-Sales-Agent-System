"""`GET /profiles/releases/{id}` 的投影：**给了哪些、没给哪些，逐键钉住**。

## 它防的那一种

ADR-0143 §二 裁定这条 route 只返回商家那半，并点名**不给**四项：
`policy_bindings`、`confidence_gate_policy_ref`、`manipulation_context_refs`、
`golden_manifest`。理由是它们是平台的实现细节 ——
**一个被看见的内部字段就不再是内部字段**。

那条裁定的形状是「**不给某些东西**」，而这种形状**最容易被下一个人顺手改掉**：
往投影里多加一个键，schema 过得去、前端读得到、200 照常返回，
**只有「这一次取舍发生过」这件事消失了**。正是 `CLAUDE.md` §三 点的那类。

## 为什么不是「断言那四个不在」就够了

因为那样的断言**会在 schema 长出第 16 个顶层键的那天说谎**：
新键既不在给的清单里、也不在那四个里，于是**它是什么都没人问过**。
所以这里的判据是**把 15 个顶层键全部分类**，三个集合的并集必须
**恰好等于** schema 的顶层键集合 —— 多一个少一个都红，
而红的时候错误信息会指着这个文件说「来分类」。

⚠️ **裁定没点名的那五个**（`strategies`、`outcome_rules`、
`annotation_taxonomy`、`assist_policy`、`closing_frame_ref`）
既不在「给」也不在「明确不给」里 —— 它们是**还没裁**，登记 T500。
分开写而不是混进「不给」，是因为这两件事的后续动作不同：
一个是已经有人想过并决定了，另一个是**还欠一次决定**。

## 它拿真 package 调产出方，不自造文档

自造一份 profile 夹具会把「schema 今天长什么样」换成「我以为它长什么样」
—— 本仓记过那种形状的代价。所以两支都从 `profiles/` 里挑真包。

⚠️ **挑的时候按性质挑，不按名字和版本号挑**（见 `_release` 的 docstring）：
第一版写死了 `("internal-demo-sales", "1.2.4")`，而主作者同一天把那个包
抬成了 1.2.5 —— **两边各自的门禁都绿，合并树上红了三条**。
`profiles/` 下的 demo 包不归这条用例管，它的版本号也就不是这条用例能钉的东西。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 先 import `dump_openapi` 把环境备齐，理由见 `test_projection_shapes.py`。
sys.path.insert(0, str(ROOT / "scripts"))
import dump_openapi  # noqa: E402,F401
from fastagent_v2.api.app import _release_document_projection  # noqa: E402
from fastagent_v2.api.responses import ProfileReleaseDocument  # noqa: E402
from fastagent_v2.profiles.protocol import ProfileRelease  # noqa: E402
from fastagent_v2.profiles.registry import ProfileRegistry  # noqa: E402

#: ADR-0143 §二 点名**给**的六项。
GIVEN = {
    "identity",
    "target_frames",
    "proactivity_policy",
    "memory_fields",
    "knowledge_bindings",
    "content_material_texts",
}

#: ADR-0143 §二 点名**不给**的四项 —— 平台的实现细节。
WITHHELD_AS_PLATFORM_DETAIL = {
    "policy_bindings",
    "confidence_gate_policy_ref",
    "manipulation_context_refs",
    "golden_manifest",
}

#: **还没裁的五项**（T500）。它们不在投影里，而那不是「被拒了」。
NOT_YET_RULED = {
    "strategies",
    "outcome_rules",
    "annotation_taxonomy",
    "assist_policy",
    "closing_frame_ref",
}

#: `identity` 在投影里摊平成的那四个键。
IDENTITY_FIELDS = {"profile_id", "release_id", "version", "content_hash"}


def _schema() -> dict[str, Any]:
    path = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


#: 两个在 schema 里**可选**的顶层键。这一族用例要的就是「都有」与「都没有」
#: 各一个真 package。
OPTIONAL_KEYS = frozenset({"memory_fields", "content_material_texts"})


def _release(*, with_optional_keys: bool) -> ProfileRelease:
    """按**这条用例需要的性质**挑一个真 package，**不按版本号挑**。

    ## 为什么不写死 `("internal-demo-sales", "1.2.4")`

    ⚠️ **第一版就是那么写的，而它在合并树上红了三条**（2026-09-20）：
    主作者同一天把那个包抬成了 **1.2.5**（`profiles/` 下的 demo 包归他，
    而本仓的规矩是「改内容必须抬版本」）。两边各自的门禁都绿，
    **合起来才红** —— 因为我的夹具钉在了一个别人随时会动的数上。

    把 `1.2.4` 改成 `1.2.5` 只是把同一颗雷往后挪一次。
    这里改成**按性质选**：这一族用例从头到尾没有一条关心版本号，
    它们关心的是「这个包有没有那两个可选键」。

    **选不到就红，并说出选不到什么** —— 一个静悄悄退回默认包的挑法，
    会让「今天没有这种包了」表现成一条断言莫名其妙地不成立。
    """

    from uuid import UUID

    registry = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
    wanted = OPTIONAL_KEYS if with_optional_keys else frozenset()
    for package in registry.packages():
        if OPTIONAL_KEYS & set(package.document) == wanted:
            document = package.document
            break
    else:
        raise AssertionError(
            f"`profiles/` 里没有一个包的可选键集合恰好是 {sorted(wanted)}"
            f"（可选键指 {sorted(OPTIONAL_KEYS)}）。"
            "**要的是「两个都有」与「两个都没有」各一个**，"
            "只有其中一个的包两边都不算 —— 这一族用例靠这两支分出"
            "「缺席时给空值」与「有的时候原样带出去」。"
        )

    identity = document["identity"]
    return ProfileRelease(
        tenant_id=UUID(int=1),
        release_id=UUID(str(identity["release_id"])),
        profile_id=str(identity["profile_id"]),
        version=str(identity["version"]),
        content_hash=str(identity["content_hash"]),
        document=document,
    )


def test_every_top_level_key_is_classified() -> None:
    """**三个集合的并集必须恰好是 schema 的顶层键。**

    多出一个 —— schema 长了一个键，而**没有人决定过商家该不该看见它**。
    这条红的时候不要去改并集那一行：去 ADR-0143 §二 的三类里挑一类，
    把新键写进去，那一刻正是该想一想的时刻。

    少一个 —— 某个集合里写着一个 schema 已经没有的键，
    那个名字正在保护一个不存在的东西。
    """

    declared = GIVEN | WITHHELD_AS_PLATFORM_DETAIL | NOT_YET_RULED
    actual = set(_schema()["properties"])

    assert declared == actual, (
        "merchant-profile 的顶层键与这里的分类对不上。"
        f"\n  schema 有而这里没分类的：{sorted(actual - declared)}"
        f"\n  这里写了而 schema 没有的：{sorted(declared - actual)}"
        "\n请按 ADR-0143 §二 的三类（给 / 明确不给 / 还没裁）归一类，"
        "不要直接改并集。"
    )
    assert not (GIVEN & WITHHELD_AS_PLATFORM_DETAIL)
    assert not (GIVEN & NOT_YET_RULED)
    assert not (WITHHELD_AS_PLATFORM_DETAIL & NOT_YET_RULED)


def test_identity_is_exactly_the_four_keys_the_projection_flattens() -> None:
    """`identity` 摊平成四个键这件事，只在它**正好是**那四个时才成立。

    投影不返回 `identity` 对象，而是把它摊成 `release_id` / `profile_id` /
    `version` / `content_hash` —— 与列表 route 那一行逐字同名。
    `identity` 一旦长出第五个字段，**那个字段会安静地掉在摊平的缝里**：
    它既不在投影里，也不会有人发现它不在。
    """

    identity = _schema()["properties"]["identity"]
    assert set(identity["properties"]) == IDENTITY_FIELDS, (
        "identity 的字段集合变了，而投影是按那四个摊平的 —— "
        "要么把新字段也摊出去，要么明写它为什么不给（ADR-0143 §二）。"
    )
    assert identity.get("additionalProperties") is False


@pytest.mark.parametrize(
    "with_optional_keys", [True, False], ids=["两个可选键都有", "两个都没有"]
)
def test_the_projection_gives_exactly_the_model_fields(
    with_optional_keys: bool,
) -> None:
    """产出方真实返回的键集合 == 响应模型声明的键集合（ADR-0142）。

    **两支都比**：可选键缺席时投影仍要给出那个键（空值），
    否则模型上那个字段在一半的租户身上是假的。
    """

    projected = _release_document_projection(_release(with_optional_keys=with_optional_keys))
    assert set(projected) == set(ProfileReleaseDocument.model_fields)


@pytest.mark.parametrize(
    "with_optional_keys", [True, False], ids=["两个可选键都有", "两个都没有"]
)
def test_no_withheld_key_appears_anywhere_in_the_projection(
    with_optional_keys: bool,
) -> None:
    """那四项**在任何一层**都不许出现，不只是顶层。

    裁定说的是「不给」，而投影是按顶层键挑的 —— 如果哪天
    `policy_bindings` 挪进了 `target_frames[]` 里，
    一条只看顶层的断言会继续绿着，而那四项已经发出去了。
    """

    projected = _release_document_projection(_release(with_optional_keys=with_optional_keys))
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                seen.add(str(key))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(projected)
    leaked = seen & WITHHELD_AS_PLATFORM_DETAIL
    assert leaked == set(), (
        f"ADR-0143 §二 点名不给的字段出现在响应里：{sorted(leaked)}。"
        "它们是平台的实现细节 —— 一个被看见的内部字段就不再是内部字段。"
    )
    assert seen & NOT_YET_RULED == set(), (
        f"还没裁的字段出现在响应里：{sorted(seen & NOT_YET_RULED)}（T500）。"
    )


def test_the_optional_keys_are_present_and_empty_when_the_package_has_none() -> None:
    """缺席时给空值，而不是省略键。

    一个时有时无的键，会让「这个商家没有素材」与「这一版的响应漏了素材」
    在前端变成分不出的两件事 —— 而前者要显示一句空态文案，
    后者是一个要报的错。
    """

    document = _release(with_optional_keys=False).document
    assert "memory_fields" not in document
    assert "content_material_texts" not in document

    projected = _release_document_projection(_release(with_optional_keys=False))
    assert projected["memory_fields"] == []
    assert projected["content_material_texts"] == {}


def test_the_optional_keys_carry_through_when_the_package_has_them() -> None:
    """有的时候原样带出去 —— 上一条单独存在会被一个恒回空值的投影骗过。"""

    release = _release(with_optional_keys=True)
    projected = _release_document_projection(release)

    assert projected["memory_fields"] == release.document["memory_fields"]
    assert projected["memory_fields"] != []
    assert projected["content_material_texts"] == release.document["content_material_texts"]
    assert projected["content_material_texts"] != {}
