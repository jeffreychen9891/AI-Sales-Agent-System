"""`ENFORCED_CONSTRAINTS` 与 `_validate_constraints` 的分支必须一一对应。

## 这一条守的是哪个方向

配包那一侧的差集是下一件事（T333 第二步）。**本文件只管代码内部这一对**：

* 表里有一个码，而实现里没有读它的分支 → **红**（那个码不会拦任何东西，
  而它在表里宣告自己会）；
* 实现里多了一条分支，而表里没有那个码 → **红**。

**第二个方向是这张表唯一会悄悄过期的方向**，而它已经发生过一次：
2026-09-10（`2812ee80`）加 `must_quote_material` 时，它是第五个有执行者的码，
**当时没有任何东西记下这件事** —— 而 T235 的到期条件写的正是那一刻
（「新增第五个可识别的约束名之前必须裁清…」）。那条到期条件是散文、没有检测方，
**于是条件打响之后四天没人知道**。本文件就是那个检测方。

## 为什么要解析源码，以及那样做的代价

判据只能从 `_validate_constraints` 的源码里读：那五条分支形状各异
（有的循环 claims、有的看 `safety_markers`、有的比对 payload 引用），
**没有一个现成的注册表可以数**。让它们改成按表 dispatch 是更强的形状，
但那是一次重构，而重构与「先把漂移变成可发现」不是同一件事 ——
**先有检测方，再谈要不要重构。**

解析源码的代价是**解析器自己可能什么都没读到**（本仓付过账：一条用写死七个名字
的正则去迁移里找那七个名字的「守卫」，它在钉它自己）。
所以下面第二条用例只问一件事：**这个解析器真的在读文本吗。**

## ⚠️ 这个解析器的已知盲区：它只认一种写法

正则是 `"([a-z_]+)" in constraints` —— 它认的是
`if "x" in constraints:` **这一种写法**。
换一种写法加第六条执行分支，**解析器读不到它，于是两个方向都不会红**：

* `if code in {"x", "y"}:` 这样的集合成员判断；
* `match` 语句；
* 从别处导入一张表再循环它。

**后果对加分支的人最不利**：他加了执行分支、本文件照绿，
**而那个码在配包侧那条检查里仍然「不认识」** ——
配包写下它会被判成拼错。

**所以加执行分支的人要按 `if "x" in constraints:` 写，或者连本文件一起改。**
（这不是一条能靠自己变红的要求，所以它写在这里，而不是假装它有守卫。）

**它与下面那条「解析器真的读到了源」是一对**：
那一条防**什么都没读到**（返回常量、正则写废），**它有守卫**；
这个盲区是**读到了，但只读了一部分**，
**而一个只读了一部分的解析器与一个读全了的解析器，在今天的仓里输出完全相同** ——
所以它只能靠这段话说清，不能靠用例。

## 探针（2026-09-13 实测，范围 `pytest tests/contracts/`，基线 1558 passed）

* **V1** 表里删掉 `must_quote_material` → **1 红**（对应关系那条）
  —— 这正是 2026-09-10 那次漂移的形状。
* **V2** 删掉 `one_question` 的执行分支 → **2 红**：对应关系那条，
  外加闸二那批里数问号那条（**后者不算本批的证据**）。
* **V3 对照组** 解析器改成返回常量 → **1 红**，
  红的是「解析器真的读到了源」那条，**而对应关系那条照绿**。

**V3 能支持什么结论，不能支持什么结论**：
✅ 它证明「解析器返回常量」这一种作弊会被发现；
⚠️ **它不证明解析器读全了分支** —— 那个常量恰好等于今天的表，
所以对应关系那条用例在它下面是绿的。**上面那段盲区说的就是这件事。**
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastagent_v2.runtime.generation_constraints import (
    ADVISORY_CONSTRAINTS,
    ENFORCED_CONSTRAINTS,
    KNOWN_CONSTRAINTS,
    is_enforced,
    unknown_constraints,
)

#: `if "X" in constraints` —— `_validate_constraints` 读一个码的唯一写法。
_BRANCH = re.compile(r'"([a-z_]+)" in constraints')

_VALIDATION = (
    Path(__file__).resolve().parents[2]
    / "services/worker/src/fastagent_v2/worker/inference_validation.py"
)


def _codes_with_a_branch(source: str) -> set[str]:
    """源码里有执行分支的那些码。"""

    return set(_BRANCH.findall(source))


def test_the_parser_reads_the_source_and_not_a_constant() -> None:
    """**解析器的守卫。**

    一个什么都没读到的解析器会让下面那条用例恒绿 —— 空集与空集相等。
    所以先喂它一段**仓里不存在的**源码，要求它把那个编出来的码读出来：
    读到了，说明它在读文本；读不到，说明它在返回别的东西。
    """

    fabricated = '    if "a_code_that_exists_nowhere" in constraints:\n        pass\n'
    assert _codes_with_a_branch(fabricated) == {"a_code_that_exists_nowhere"}

    assert not _codes_with_a_branch("if something_else:\n    pass\n"), (
        "这个正则连一段不含约束判断的代码都要报出东西来 —— 那它报的数都不算数"
    )


def test_every_enforced_code_has_a_branch_and_every_branch_is_in_the_table() -> None:
    """两个方向都断言，**而不是断言一个上界**。

    「表 ⊆ 分支」单独成立时，实现里多一条分支不会红；
    反过来也一样。写成相等，两个方向各自会报出差在哪一边。
    """

    branches = _codes_with_a_branch(_VALIDATION.read_text(encoding="utf-8"))

    missing_branch = sorted(ENFORCED_CONSTRAINTS - branches)
    assert not missing_branch, (
        f"这些码在 ENFORCED_CONSTRAINTS 里，而 _validate_constraints 里没有读它们的分支："
        f"{missing_branch}。\n"
        "一个在「会被执行」那张表里而没有执行者的码，比不在表里更糟："
        "配包作者会按「它会拦东西」来写配包。\n"
        "常见成因：分支被删掉或改名而表没跟着；或表里先写下了一个还没实现的码"
        "（那种码不该进这张表，让配包侧那条检查红着更好）。"
    )

    missing_table = sorted(branches - ENFORCED_CONSTRAINTS)
    assert not missing_table, (
        f"_validate_constraints 里有读这些码的分支，而 ENFORCED_CONSTRAINTS 里没有它们："
        f"{missing_table}。\n"
        "新加一条执行分支就是给平台新增了一个「会被执行」的码，"
        "而那张表是配包侧那条检查的唯一依据。\n"
        "常见成因：新加了执行分支而没更新表（2026-09-10 加 must_quote_material 那次"
        "就是这个形状，而当时没有本条用例）；或码名在两处拼得不一样。"
    )


#: 那条准入条件的**第一个实例**：本该有执行者而还没写的码。
#:
#: 规则说「这种码不进嘱咐档」；**实例说「就是这一个」** —— 两者作用不一样，
#: 而没有实例的规则在第一次被考验时会输给最省力的修法：
#: 规则要读者自己完成「我手上这个码属不属于这种」的判断，
#: **而把它加进 `ADVISORY_CONSTRAINTS` 是一个不需要判断的动作。**
_NOT_ADVISORY_BECAUSE_IT_SHOULD_BE_ENFORCED = "approved_material_only"


def test_the_code_that_needs_an_executor_is_in_neither_table() -> None:
    """**垃圾桶不是一次性变成的，是从第一个「看起来该放进去」的码开始的。**

    `approved_material_only` 就是那第一个：它写于 2026-09-03（建包那一笔），
    而 2026-09-10 落地的 `must_quote_material` 就是同一个意图的实现版
    —— 所以它**不是「今天不可机器判」，也不是「要限制的东西还不存在」**，
    它是**有替代品、该改过去**的那一类（T333 ③）。

    所以在它被改掉之前，配包侧那条检查**应当红着**，
    而本条断言拦住那个最省力的修法：把它加进嘱咐档。

    **③ 落地之后本条不删**：那时这个码不再出现在任何配包里，
    诱惑也就消失了 —— 但它会随着「下一份配包又写了它」而回来，
    而本条在那时仍然是对的。
    """

    code = _NOT_ADVISORY_BECAUSE_IT_SHOULD_BE_ENFORCED
    assert code not in ADVISORY_CONSTRAINTS, (
        f"{code!r} 被加进了 ADVISORY_CONSTRAINTS（嘱咐档）。\n"
        "它不属于嘱咐档：嘱咐档只收「今天不可机器判」或「要限制的东西还不存在」的码，"
        f"而 {code!r} 有实现版 —— `must_quote_material`（2026-09-10 落地，"
        "检查 payload 里引了哪些素材），它只是没人回头改 2026-09-03 写的那份配包。\n"
        "**配包侧那条检查红在它上面是预期状态，不是待修复的状态。**\n"
        "正确的修法有两条：把配包里的这个码改成 must_quote_material（T333 ③，"
        "两轴那一处改完这条红自然消失 —— 全仓只有它声明这个码）；或者给这个码写一条执行分支并进 "
        "ENFORCED_CONSTRAINTS。\n"
        "加进嘱咐档不是修法：那把「配包里的码没人执行」换成了"
        "「这个码被归到嘱咐档所以不用执行」——同一个洞，而且这次是绿的。"
    )
    assert code not in ENFORCED_CONSTRAINTS, (
        f"{code!r} 被加进了 ENFORCED_CONSTRAINTS，而 `_validate_constraints` 里"
        "并没有读它的分支 —— 上面那条对应关系的用例会同时红。\n"
        "这张表的成员资格是「实现里真有一条分支读它」，不是「我们希望它被执行」。"
    )


#: 在架配包的目录。**读真配包，不读夹具** —— 本条问的是
#: 「今天发出去的那几份包里，谁声明了什么」，而夹具回答不了那一问。
_PROFILES = Path(__file__).resolve().parents[2] / "profiles"

#: **反方向今天的值，写死。空集是健康状态。**
#:
#: 2026-09-13：`must_quote_material` 曾经在这个集合里（它 2026-09-10 落地，
#: 而三份在架配包一个都没声明它），T333 ③ 把两轴那份的
#: `approved_material_only` 改成它之后，这个集合空了。
_ENFORCED_BUT_NOBODY_DECLARES: frozenset[str] = frozenset()


def _codes_declared_by_shipped_profiles() -> set[str]:
    """三份在架配包为任何策略声明过的全部码。"""

    declared: set[str] = set()
    for path in sorted(_PROFILES.glob("*/profile.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for body in (document.get("strategies") or {}).values():
            declared.update(str(code) for code in (body or {}).get("generation_constraints", []))
    return declared


def test_the_shipped_profiles_are_the_source_and_not_a_fixture() -> None:
    """**上一条的守卫**：那个扫描器真的读到了配包。

    它返回空集时，下面那条会因为「空集 ⊆ 任何集合」而恒绿 ——
    所以先断言它读出了一些**只可能来自真配包**的东西。
    """

    declared = _codes_declared_by_shipped_profiles()
    assert len(declared) >= 5, f"只从配包里读出 {len(declared)} 个码 —— 扫描器大概没读到文件"
    assert "no_new_commitment" in declared, "三份配包都声明它，读不到说明扫的不是这些文件"


def test_the_codes_nobody_declares_are_exactly_the_recorded_ones() -> None:
    """有执行者、而**没有任何在架配包声明**它的码 —— 今天应当一个都没有。

    **这个方向不做成封版闸，是有理由的**：封版期检查的对象是**一份配包**，
    而「实现有执行者而没人声明」是**平台侧与全部配包之间的关系** ——
    做成封版闸的话，**每一份配包都要为「别人没声明」负责**。

    **空集是健康状态。非空时本条红，而红本身不能告诉你该做什么** ——
    那个码落在两种完全相反的情形之一：

    * **能力先到**：执行者写好了，等着第一份配包来用它
      （`must_quote_material` 在 2026-09-10 到 2026-09-13 之间就是这样）——
      处置是**等，或者去给配包加声明**；
    * **有人删了声明**：配包里那一处被改掉或改错了，而执行者还在 ——
      处置是**把声明补回去**。

    **两种处置相反，所以本条要求人判断，而不是替他判断。**
    （一条能自己区分这两种情形的检查今天写不出来：它要知道「有没有人打算用它」，
    而那不在任何一份文件里。）

    ## ⚠️ 它读的是 JSON 文件本身，**不是 `load_package` 之后的包** —— 这是设计，不是省事

    **实测（2026-09-13）**：给这条用例做探针时把两轴那份的 `must_quote_material`
    换掉，内容哈希随之过期，**那份包于是装不进去了** ——
    而本条**仍然答得出来**，因为它 `json.loads` 那个文件。

    **这是一个属性，不是巧合**：**配包坏了的时候，本条仍然有效。**
    而「有执行者而没人声明」正是那种要在**配包出问题时**也能问的问题 ——
    一份哈希过期、装不进去的包，它声明了什么仍然是一个事实。

    **下一个人会想把它改成「先 `load_package` 再检查」，因为那样看起来更干净** ——
    那一改会悄悄丢掉这个属性：包装不进去时本条不再回答，而是跟着一起红，
    **于是它从「一条独立的事实检查」变成「加载守卫的一个附属品」。**
    要改的话，先答一句「包坏了的时候谁来回答这一问」。
    """

    enforced_and_unused = sorted(ENFORCED_CONSTRAINTS - _codes_declared_by_shipped_profiles())
    assert enforced_and_unused == sorted(_ENFORCED_BUT_NOBODY_DECLARES), (
        f"有执行者而没有任何在架配包声明的码变了：现在是 {enforced_and_unused}，"
        f"记录的是 {sorted(_ENFORCED_BUT_NOBODY_DECLARES)}。\n"
        "空集是健康状态；这个集合非空说明有一个码的执行者在空转。\n"
        "**本条不替你判该怎么办**，因为两种成因的处置相反：能力先到（执行者写好了"
        "等第一份配包来用，处置是等或去加声明）；有人删了声明（配包那一处被改掉"
        "而执行者还在，处置是把声明补回去）。\n"
        "判断完之后，把 _ENFORCED_BUT_NOBODY_DECLARES 改成新的事实，"
        "并在这里留一句为什么。"
    )


def test_the_two_tiers_do_not_overlap() -> None:
    """一个码不能既会被执行又只是嘱咐 —— 那样「它会不会拦东西」就没有答案了。"""

    both = sorted(ENFORCED_CONSTRAINTS & ADVISORY_CONSTRAINTS)
    assert not both, f"这些码同时在两张表里：{both}"
    assert KNOWN_CONSTRAINTS == ENFORCED_CONSTRAINTS | ADVISORY_CONSTRAINTS


def test_an_advisory_code_is_known_but_not_enforced() -> None:
    """嘱咐档的成员**要被认识**（不然配包侧那条检查会把它当拼错），**而不被执行**。"""

    for code in ADVISORY_CONSTRAINTS:
        assert code in KNOWN_CONSTRAINTS, f"{code!r} 不在 KNOWN_CONSTRAINTS 里"
        assert not is_enforced(code), f"{code!r} 在嘱咐档里，却被当成会执行的码"


def test_unknown_constraints_answers_for_a_hand_edited_profile() -> None:
    """`unknown_constraints` 读的是配包里的值，所以它对不合形状的输入也要有答案。

    **拼错一个字母是这条检查存在的全部理由**，所以它单独断言一次：
    `no_new_commitmnet` 必须被报出来，而 `no_new_commitment` 不能。
    """

    assert unknown_constraints(["no_new_commitmnet"]) == ("no_new_commitmnet",)
    assert unknown_constraints(["no_new_commitment"]) == ()
    assert unknown_constraints(["current_goal_only"]) == (), "嘱咐档的码不算不认识"
    assert unknown_constraints([]) == ()
    assert unknown_constraints(None) == (), "不是数组时答「没有不认识的」，而不是抛"
    assert unknown_constraints(["b", "a", "b"]) == ("a", "b"), "去重且排序，好让报错稳定"


def test_the_admission_message_names_both_tiers_and_computes_them() -> None:
    """封版闸那条报错**把两档算出来给人看**，而不是写死「五个 / 三个」。

    **这一条是 `_enforced_codes()` / `_advisory_codes()` 的消费方走通** ——
    它们是 2026-09-14 为接上 `is_enforced()` 加的（回查 C-1）：
    那个函数自称「给读代码的人一个比 `in` 更明确的问法」，
    而封版那条判据要的是**集合运算**（差集），单点判定接不进去；
    **报错文案要的恰好是逐码判定**。

    **为什么这条用例必须存在**：报错**只在失败时出现**，
    而它初版写着「-- five of them are enforced, three are advisory」——
    **两个写死的数，是「今天的表长什么样」的断言**，
    表一变它们就在撒谎，而**没有任何东西会发现**
    （本仓那条「报错文案是最容易长期带错的东西」）。

    断言的是**内容**而不是措辞：两档的成员各自出现在文案里，
    且**与那两张表一致** —— 所以它不会因为改一句话而红，
    只会因为「文案里的清单与表对不上」而红。
    """

    from fastagent_v2.profiles.registry import (  # noqa: PLC0415
        ProfilePackageError,
        _check_generation_constraints_are_known,
    )

    document = {
        "strategies": {"offer_material": {"generation_constraints": ["no_new_commitmnet"]}}
    }
    with pytest.raises(ProfilePackageError) as refusal:
        _check_generation_constraints_are_known(document, Path("profiles/fake/profile.json"))

    message = str(refusal.value)
    assert "no_new_commitmnet" in message, "先点名那个写错的码"
    for code in sorted(ENFORCED_CONSTRAINTS):
        assert code in message, f"会被执行的 {code!r} 该出现在文案里"
    for code in sorted(ADVISORY_CONSTRAINTS):
        assert code in message, f"只是嘱咐的 {code!r} 该出现在文案里"
    assert "five of them" not in message, (
        "「五个 / 三个」那种写死的数不该回来 —— 它是一条没人守的断言"
    )
