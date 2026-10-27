"""`material:` / `knowledge:` 的三段语法，以及封版路径上四个面都真的在检。

**它先前没有规格。** 仓里 169 处真实引用（去重 164 个值）用的是同一个形状，
而**没有任何东西约束过它**：`merchant-profile.schema.json` 对
`content_materials` 的裸引用只说 `{"type":"string","minLength":1}`，
对 `knowledge_bindings` 与 `return_confirmation.tone_material_ref`
都只说 `{"type":"string"}`。

**「四个面」是独立审查（2026-09-11，P1-1）改对的，第一版写的是三个面**，
而这个模块的文字自己就带着反证：**164 是四个面的并集**
（`content_materials` 160 + `tone_material_ref` 8 + `knowledge_bindings` 1
= 169 次出现，去重 164；`materials.json` 的 151 个键全都已经出现在某个帧里，
对去重计数没有贡献）。**只检三个面时，那三个面的并集是 161。**
于是「164 个引用一个都不被拒」这句证据**覆盖的面比守卫访问的面多一个**——
它是真的，但不是为它声称的理由真的。这正是本仓那条
「探针绿了不等于守卫有用」在**证据**一侧的同型。

**这一批只加规则，不改任何现有引用**——实测 `profiles/` 下四个面上的
164 个引用**一个都不会被新语法拒掉**，三份在架的包封版照旧。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles import ProfilePackageError
from fastagent_v2.profiles.registry import MATERIAL_TEXTS_KEY, load_package
from fastagent_v2.runtime.references import (
    REFERENCE_KINDS,
    Reference,
    parse_reference,
    reference_refusal,
)

# **复用封版助手而不是再写一个**：`_seal` 里那条「戳是对合并后的文档取的」
# 是本仓唯一的封版规则，第二份实现会在它变的那天悄悄落后。
from test_profile_materials_are_trusted import (  # noqa: E402
    _base_document,
    _seal,
    scratch_repository,  # noqa: F401 —— fixture，按名字被 pytest 取用
)

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 语法本身
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "material:demo-opening:v1",
        "knowledge:carrier-terms:v1",
        "material:a:v1",  # 实测最短的 slug 就是一个字符
        "material:demo-buy-meeting-i-cant-book:v1",  # 实测最长那一类
        "knowledge:kb-001:v12",
    ],
)
def test_a_well_formed_reference_is_accepted(reference: str) -> None:
    assert reference_refusal(reference) is None


@pytest.mark.parametrize(
    ("reference", "because"),
    [
        ("material:demo-opening", "三段"),
        ("material:demo-opening:v1:extra", "三段"),
        ("stuff:demo-opening:v1", "不是已知类别"),
        ("material:Demo-Opening:v1", "合法 slug"),
        ("material:demo_opening:v1", "合法 slug"),
        ("material:-demo:v1", "合法 slug"),
        ("material:demo-:v1", "合法 slug"),
        ("material:demo--opening:v1", "合法 slug"),
        ("material:demo-opening:1", "合法版本"),
        ("material:demo-opening:v0", "合法版本"),
        ("material:demo-opening:v01", "合法版本"),
        ("", "空字符串"),
    ],
)
def test_a_malformed_reference_is_refused_and_says_which_segment(
    reference: str, because: str
) -> None:
    """**拒绝理由要指出是哪一段**，不是一个布尔。

    沿用 `credentials_mode_refusal` 的形状（ADR-0113 §五 自己写着
    「规则被提成纯函数，这是本条最该被继承的部分」）：
    第二段有大写字母和第三段写成 `v0`，是两件要分别去改的事。
    """

    refusal = reference_refusal(reference)
    assert refusal is not None, f"{reference!r} 应当被拒"
    assert because in refusal, f"{reference!r} 的理由没说清是哪一段：{refusal}"


def test_a_reference_that_is_not_a_string_is_refused_rather_than_crashing() -> None:
    """`content_materials` 的条目可以是对象，而对象里取不到 `ref` 时是 `None`。

    **那一路必须是一条拒绝，不是 `AttributeError`**——封版路径上抛出一个
    未处理的异常，作者看到的是堆栈而不是「哪一条写错了」。
    """

    assert reference_refusal(None) is not None
    assert reference_refusal(123) is not None
    assert "字符串" in str(reference_refusal(None))


def test_two_segments_says_the_version_is_probably_missing() -> None:
    """最常见的写错就是漏了版本段，所以这一路单独给一句话。"""

    refusal = reference_refusal("material:demo-opening")
    assert refusal is not None and "版本段" in refusal


def test_the_expected_kind_catches_a_material_where_knowledge_belongs() -> None:
    """`knowledge_bindings` 里出现一个 `material:` 引用是个真实的错误。

    **不传 `expected_kind` 就看不出来**——两类都是合法引用，
    错的是它出现的位置。
    """

    assert reference_refusal("material:x:v1") is None
    refusal = reference_refusal("material:x:v1", expected_kind="knowledge")
    assert refusal is not None and "knowledge" in refusal


def test_the_kind_vocabulary_is_closed() -> None:
    """加一类不是加一个字符串。

    加进来的那一类要同时回答「它和素材冲突时谁赢」——
    生成步拿到两种来源时必须知道听谁的（那一格的裁定四：素材优先）。

    **2026-09-12 加了第三类 `asset`**（E3 资料，ADR-0130 / 并行安全性分析 Y1）。
    它的入会答案是「**不必裁**」：素材决定说什么话，asset 决定要不要附文件，
    两者不争同一个位置。**「不必裁」是一个答案，不是答不出**——
    分界线是「模型看不看得见」，`asset:` 看得见、`policy:` 看不见。
    """

    assert REFERENCE_KINDS == frozenset({"material", "knowledge", "asset"})


def test_policy_is_still_out_after_asset_came_in() -> None:
    """**加一类之后，词表不能因此变松。**

    `policy:` 与本语法三段逐字同形，今天仍必须被拒。
    **这一条与上一条不是一回事**：上一条钉的是集合等于什么，
    本条钉的是**那个集合真的被用来拒东西**——
    一个只被断言、从不被读的常量，改错了也不会有人知道。
    """

    refusal = reference_refusal("policy:authorization:v1")
    assert refusal is not None
    assert "policy" in refusal


def test_an_asset_ref_is_refused_where_a_material_is_expected() -> None:
    """**这一条是本批真正的守卫，其余都是它的注脚。**

    `asset:price-list:v1` **本身是完全合法的三段**——三段齐、slug 合法、版本合法。
    **所以拒掉它的只可能是 `expected_kind`**，不可能是语法。
    **换句话说：这条用例红的时候，红的一定是「素材位被别的类混进来了」，
    而不会是别的原因。** 这正是加一类进闭合词表时唯一真正危险的那件事。
    """

    # 先证明它本身合法——否则下面那条断言可能因为语法错而绿，那就证明不了任何事
    assert reference_refusal("asset:price-list:v1") is None

    refusal = reference_refusal("asset:price-list:v1", expected_kind="material")
    assert refusal is not None
    assert "material" in refusal and "asset" in refusal

    # 反向也要成立：素材不能混进 asset 位
    assert reference_refusal("material:demo-x:v1", expected_kind="asset") is not None


def test_an_asset_reference_parses_like_the_other_two() -> None:
    """三段语法对三类是同一套，`asset` 不例外。"""

    assert parse_reference("asset:price-list:v1", expected_kind="asset") == Reference(
        kind="asset", slug="price-list", version=1, raw="asset:price-list:v1"
    )


def test_parse_shares_the_rule_rather_than_judging_again() -> None:
    """`parse_reference` 走的就是 `reference_refusal`，不另判一遍。

    **两个判断点就是两条规则**，而它们漂开的那一刻没有任何东西会红。
    """

    parsed = parse_reference("knowledge:kb-007:v3")
    assert parsed == Reference(
        kind="knowledge", slug="kb-007", version=3, raw="knowledge:kb-007:v3"
    )

    with pytest.raises(ValueError) as caught:
        parse_reference("material:Bad:v1")
    # 同一句话，不是另写的一句
    assert reference_refusal("material:Bad:v1") in str(caught.value)


# --------------------------------------------------------------------------
# 封版路径：四个面都要真的在检
# --------------------------------------------------------------------------


def test_every_shipped_package_still_publishes() -> None:
    """**这一条是这一批的安全带。**

    计划步固定底那一批的教训：一条新的 admission 规则如果拒掉在架的包，
    失败方向是「封不了版」，而那会在演示当天才被发现。

    实测 `profiles/` 下**四个面**上的 164 个引用**一个都不被新语法拒掉**，
    这一条把它钉住。**第四个面加进来之后这条仍然绿**，而这不是运气：
    在架的 8 个 `tone_material_ref` 值（3 个不同值，零空串）作者与审查方
    各逐条验过一遍语法。
    """

    published = [path for path in sorted((ROOT / "profiles").glob("*/profile.json"))]
    assert published, "一个包都没有，这条用例就什么也没证明"
    for path in published:
        load_package(path)


def _package_with(directory: Path, mutate: Any) -> Path:
    document = _base_document()
    materials = {"material:demo-x:v1": "一段话术。"}
    # **其余帧的声明一律清空**：基底包里别的帧各自声明了素材而本用例不给它们正文，
    # 不清的话覆盖检查会先红——而那就不是这几条用例在证明的事。
    for frame in document["target_frames"]:
        frame["content_materials"] = []
    document["target_frames"][0]["content_materials"] = ["material:demo-x:v1"]
    document["knowledge_bindings"] = ["knowledge:kb-001:v1"]
    mutate(document, materials)
    return _seal(directory, document, materials)


def test_a_bad_ref_in_content_materials_is_refused_at_admission(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """面一：帧声明的那一处。"""

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_materials"] = ["material:Demo-X:v1"]
        materials.clear()
        materials["material:Demo-X:v1"] = "一段话术。"

    path = _package_with(scratch_repository / "frame", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    assert "content_materials" in str(caught.value)
    assert "合法 slug" in str(caught.value)


def test_an_asset_ref_in_content_materials_is_refused_at_the_sealing_face(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**面一，但问的是另一个问题：封版那一面真的在管类别，不只在管形状。**

    上面那条同名的用例用的是一个**语法坏掉**的 ref（`material:Demo-X:v1`，大写），
    所以它证明的是「形状被检了」。**本条用的 ref 形状完全合法**，
    坏的只有类别——**于是它证明的是「类别也被检了」**。

    **这两件不是同一件，而 2026-09-12 加 `asset` 之前，
    仓里没有任何一条用例把后一件钉住过**：那之前只有两类，
    而两类的 `expected_kind` 从没在封版路径上被一个合法 ref 试过。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_materials"] = ["asset:price-list:v1"]
        materials.clear()
        materials["asset:price-list:v1"] = "一段话术。"

    path = _package_with(scratch_repository / "assetinmaterials", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "content_materials" in message
    # 拒绝理由要说到点子上：不是「语法不对」，是「类别不对」
    assert "material" in message and "asset" in message
    assert "合法 slug" not in message, "拒错了理由：这个 ref 的 slug 是合法的"


def test_an_ungated_on_request_asset_is_refused(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**裁定六 §2 变成门禁的那一条，本批的主守卫。**

    素材那一侧，`on_request` 不带 `topics` 是**有意保留的**不设闸兜底
    （ADR-0125 §二）。**同一个拼写在文件上必须被拒**：不设闸的文件投送
    失败方向是「发错东西给客户且撤不回」，而本格既不做撤回也不做发前人工确认。

    **这条用例红的时候，红的一定是「文件那侧的兜底被放行了」**——
    因为同一份包里那个 ref 语法完全合法，`availability` 也是合法枚举值。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_assets"] = [
            {"ref": "asset:price-list:v1", "availability": "on_request"}
        ]

    path = _package_with(scratch_repository / "ungatedasset", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "on_request" in message and "topics" in message
    assert "asset:price-list:v1" in message


def test_the_same_spelling_is_still_allowed_for_a_material(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**阴性对照，而它是上一条的全部意义所在。**

    如果不设闸的 `on_request` 对素材也被拒，上一条就只是证明了
    「这个规则对所有东西都生效」——**而那会悄悄拒掉在架包**，
    因为素材那条兜底是 ADR-0125 §二 有意保留、用户 2026-09-11 亲自裁过的。

    **两条一起才说明「本批只收窄了文件那一侧」。**
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_materials"] = [
            {"ref": "material:demo-x:v1", "availability": "on_request"}
        ]

    path = _package_with(scratch_repository / "ungatedmaterial", mutate)
    load_package(path)  # 不抛


def test_an_asset_ref_of_the_wrong_kind_is_refused_at_the_assets_face(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """第五个面真的在检类别，不只是在检形状。

    **这一面在架包里一个值都没有**，所以「在架包全过」对它什么也证明不了
    （`registry.py` 那段注释把 P1-1 的教训反向记了一次）。本条是它唯一的证据。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_assets"] = [
            {"ref": "material:demo-x:v1", "availability": "proactive"}
        ]

    path = _package_with(scratch_repository / "wrongkindasset", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "content_assets" in message
    assert "asset" in message and "material" in message
    assert "合法 slug" not in message, "拒错了理由：这个 ref 的 slug 是合法的"


def test_a_gated_asset_publishes(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**安全带**：**真的设了闸**的文件声明必须能封版。

    没有这一条，上面几条可以靠「拒绝一切 content_assets」同时变绿——
    而那是一条会拒掉所有将来的包的 admission。

    **本条的夹具被独立审查 2026-09-12 P1-1 换过**：第一版写的是
    `{"availability": "proactive"}` 而**不带 `stages`**，并把它命名为「gated」。
    那时那种声明确实能封版——**于是这条用例把缺口钉住了，而不是当安全带**：
    下一个写 E3-5 选取的人读到这条绿用例，会判定「不设闸的 `proactive` 资料
    是合法的、选取层要支持它」。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        frame = document["target_frames"][0]
        frame["material_stages"] = [{"name": "报价", "after": []}]
        # 帧一旦声明阶段，素材侧也要求每条素材点名 availability（既有规则）
        frame["content_materials"] = [
            {"ref": "material:demo-x:v1", "availability": "proactive", "stages": ["报价"]}
        ]
        frame["content_assets"] = [
            {
                "ref": "asset:price-list:v1",
                "availability": "proactive",
                "stages": ["报价"],
            }
        ]

    path = _package_with(scratch_repository / "gatedasset", mutate)
    load_package(path)  # 不抛


def test_a_proactive_asset_without_stages_is_refused(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """**独立审查 P1-1**：`proactive` 那一档原本一道闸都不带也能封版。

    门槛取「必须点名 `stages`」而不是素材那侧的「帧声明了阶段才要求」——
    素材那侧漏网的那一种由**字节预算**接住，**而资料不占提示词字节，接不住**。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["content_assets"] = [
            {"ref": "asset:price-list:v1", "availability": "proactive"}
        ]

    path = _package_with(scratch_repository / "ungatedproactive", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    assert "stages" in str(caught.value)


@pytest.mark.parametrize(
    ("availability", "extra", "value"),
    [
        ("proactive", "topics", ["价格"]),
        ("on_request", "stages", ["报价"]),
        ("timer", "topics", ["价格"]),
    ],
)
def test_a_gate_written_on_the_wrong_field_is_refused(
    scratch_repository: Path,  # noqa: F811
    availability: str,
    extra: str,
    value: Any,
) -> None:
    """**独立审查 P2-1**：闸写在不被读的字段上，作者以为设了闸而其实没有。

    最难看见的是 `proactive` + `topics`：选取语义里 `proactive` 分支
    **从头到尾不看 `topics`**，于是那个文件每个回合都递出去，而包封版是绿的。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        frame = document["target_frames"][0]
        frame["material_stages"] = [{"name": "报价", "after": []}]
        # 帧一旦声明阶段，素材侧也要求每条素材点名 availability（既有规则）
        frame["content_materials"] = [
            {"ref": "material:demo-x:v1", "availability": "proactive", "stages": ["报价"]}
        ]
        frame["content_assets"] = [
            {
                "ref": "asset:price-list:v1",
                "availability": availability,
                extra: value,
            }
        ]

    path = _package_with(scratch_repository / f"wrongfield{availability}{extra}", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert extra in message


def test_a_bad_key_in_the_materials_file_is_refused_at_admission(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """面二：正文文件的键。

    **它与面一是两条独立的入口**——先前任何一个写错都没有东西看得见。
    这里让帧那一侧合法、只把文件里的键写坏，所以红的只可能是这一面。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        materials.clear()
        materials["material:demo-x:v1"] = "一段话术。"
        # **写坏的那个键只在文件里，不在帧里。** 两边都写的话，帧那一面会先拦住它，
        # 这条用例就变成了帧那一面的第二条——**探针 C（抬掉文件那一面）第一版全绿，
        # 正是因为这个**。
        materials["material:demo-x:v01"] = "另一段。"

    path = _package_with(scratch_repository / "texts", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    assert "合法版本" in str(caught.value)


def test_a_bad_ref_in_knowledge_bindings_is_refused_at_admission(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """面三：知识绑定。

    **这一面今天零读者**（`knowledge_bindings` 是 schema 里的必填字段，
    而全仓没有一行代码读它），所以写错了**什么都不会发生**——
    正是最该在封版时拦住的那一类。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["knowledge_bindings"] = ["material:kb-001:v1"]

    path = _package_with(scratch_repository / "bindings", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    assert "knowledge_bindings" in str(caught.value)


def test_a_bad_tone_material_ref_is_refused_at_admission(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """面四：帧的回归确认语气。**这一面是独立审查（P1-1）量出来的漏项。**

    它和面三是同一类失败——**今天零生产读者**。生产读的是
    `return_confirmation["policy"]`（`target_stack.py:132/678`、`pipeline.py:694`），
    **没有一处读 profile 的 `tone_material_ref`**，所以写错了什么都不会发生。

    **写坏的值只放在这一面**，与面二同样的理由：同时写进 `content_materials`
    的话，面一会先拦住它，这条用例就变成了面一的第二条而它自己什么也没证明。
    断言点名 `tone_material_ref`，**验的是「红在正确的那一条上」**，
    不是「红了」——审查方的反向探针（抬掉这一面，同一个值照收）已经证过
    第一版这里是通的。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        document["target_frames"][0]["return_confirmation"]["tone_material_ref"] = (
            "MATERIAL::Bad::vONE::extra"
        )

    path = _package_with(scratch_repository / "tone", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "tone_material_ref" in message
    # 面一没有被写坏，所以它不该是报出来的那一个。
    assert "content_materials" not in message


def test_a_tone_material_ref_no_longer_seals(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """`tone_material_ref` 2026-09-24 整键收掉 —— 带着它的包封版即拒。

    ## 这条用例的前身

    它原来叫 `test_an_empty_tone_material_ref_is_skipped_not_refused`，
    钉的是「空串表示没有语气素材，调用点要跳过不检」那条不对称 ——
    **那个调用点连同字段一起删了**（用户裁定：回归确认那句话由模型按
    上下文自己写，与静默唤醒同一条哲学；此前它三包悬空 + 运行期零消费）。

    现在钉相反的事：`additionalProperties: false` 使这个键**回不来** ——
    谁把它加回包里而不解决「谁来消费它」，封版当场红。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        for frame in document["target_frames"]:
            frame["return_confirmation"]["tone_material_ref"] = "material:neutral-return:v1"

    path = _package_with(scratch_repository / "tone-back", mutate)
    with pytest.raises(Exception, match="tone_material_ref"):
        load_package(path)


def test_a_clean_package_built_the_same_way_still_publishes(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """上面四条的反面。

    没有这一条，一个「拒绝一切」的实现会让那四条全绿——
    而它们要证明的是「这个闸在选择」，不是「这个闸在拦」。

    基底包是 `synthetic-third`（1.0.3 起不再带 `tone_material_ref` ——
    那个键 2026-09-24 整键收掉了）。
    """

    path = _package_with(scratch_repository / "clean", lambda document, materials: None)
    package = load_package(path)
    assert MATERIAL_TEXTS_KEY in package.document


def test_the_grammar_check_runs_before_the_coverage_check(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """顺序是有意的，所以钉住它。

    覆盖检查比的是两个集合相不相等。一个写坏的键会让它报
    「有素材没文本」——**那句话是对的，但指错了地方**：
    作者会去补一段本来就存在、只是键写错了的文本。
    """

    def mutate(document: dict[str, Any], materials: dict[str, str]) -> None:
        materials.clear()
        materials["material:demo-x:v1"] = "一段话术。"
        # 帧里声明一个语法就不合法的引用：两道闸都会不满意，
        # 而先说话的必须是语法那一道。
        document["target_frames"][0]["content_materials"] = ["material:demo-x:vONE"]

    path = _package_with(scratch_repository / "order", mutate)
    with pytest.raises(ProfilePackageError) as caught:
        load_package(path)
    message = str(caught.value)
    assert "合法版本" in message, f"先说话的应当是语法那一道：{message}"
    assert "no text in" not in message


def test_the_json_schema_deliberately_does_not_carry_the_grammar() -> None:
    """**语法只有一个定义点，这一条钉住那个选择。**

    往 schema 里塞一个 `pattern` 会让同一条规则有两处定义，
    而它们漂开的那一刻没有任何东西会红。schema 管形状（是不是字符串、空不空），
    `references.py` 管规则（三段各是什么、为什么）。
    """

    schema = json.loads(
        (ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    bindings = schema["properties"]["knowledge_bindings"]["items"]
    assert "pattern" not in bindings, (
        "schema 里出现了 pattern：语法现在有两个定义点，"
        "而 `references.py` 是那唯一的一个"
    )
    materials = schema["properties"]["target_frames"]["items"]["properties"]["content_materials"]
    assert "pattern" not in json.dumps(materials), "同上"


def test_the_shipped_refs_are_the_shape_this_grammar_describes() -> None:
    """语法不是凭空定的：它是**量出来的**。

    在架包里每一个真实引用都是三段、slug 全小写、版本全是 `v1`。
    （条数会随配包增删而变，所以这里不写死数字 —— **写死的数字会在
    下一次产品形态变化时变成假的**，而 2026-09-17 那次正是如此。）
    **这一条在有人写下第一个不合形状的真实引用时变红**，
    那时要么改那个引用，要么改语法——但不会是「悄悄两种写法并存」。
    """

    seen = 0
    for path in sorted((ROOT / "profiles").glob("*/profile.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        references = [
            item if isinstance(item, str) else (item or {}).get("ref")
            for frame in document.get("target_frames", [])
            for item in frame.get("content_materials", [])
        ] + list(document.get("knowledge_bindings", []))
        for reference in references:
            expected = "knowledge" if str(reference).startswith("knowledge:") else "material"
            assert reference_refusal(reference, expected_kind=expected) is None, reference
            seen += 1
    # **下界 2026-09-17 从 100 降到 20，而降的理由不是「让它过」。**
    #
    # 用户当天裁定「素材只做关键节点，其余交给模型发挥」，在架那份包
    # 因此从 147 段收到 18 段 —— 这个下界原本是照着三百多段的语料定的。
    #
    # **它守的从来不是「语料要多大」，是「别让这条用例在包被清空时静静地过」。**
    # 20 仍然守得住那件事（在架包今天 33 个引用），而 100 会把一条
    # 已经裁定过的产品形态判成失败。
    assert seen >= 20, f"只看到 {seen} 个引用，样本太小，这条用例证明不了什么"


def test_a_copied_document_is_not_mutated_by_the_check(
    scratch_repository: Path,  # noqa: F811
) -> None:
    """封版路径上的检查**只读**。

    `load_package` 会把文档交出去，一道检查如果顺手改了它，
    改动会静默地进入 `content_hash` 之后的那一份。
    """

    path = _package_with(scratch_repository / "readonly", lambda document, materials: None)
    before = json.loads(path.read_text(encoding="utf-8"))
    load_package(path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert before == after
    assert copy.deepcopy(before) == after
