"""E4-5：记下来的那几件事，**回到提示词里**。

## 在这一批之前，记忆是一条断掉的路

表（`054`）、迁移、RLS、控制面路由、`remember` 写入路径、
封闭词表的闸（`_declared_memory_fields`）—— **六样都在**，
而值从来没有回到过任何一个提示词。

于是「记住了什么」对模型等于没发生过：下一轮它照样不知道这个客户是做什么的。
**两头都在、中间没接**，而这一次连读取函数的 docstring 都把它写成了一条原则
（「只取键不取值」）—— 那句话在写入闸的语境里是对的，
**而整条路上除了那道闸没有第二个读者**。

## 它同时结掉 T300 的三件

T300 的到期条件写死在本批：**份额、由份额反推的条数、schema `maxItems`，
三者只能一起定。** 本文件的 `test_the_three_numbers_of_t300_agree`
把三个数绑在一起 —— 单改一个就红。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.memory_prompt import (
    ELLIPSIS,
    REMEMBERED_BYTES,
    REMEMBERED_VALUE_BYTES,
    memories_for_prompt,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)
SCHEMA = json.loads(
    (
        REPOSITORY_ROOT
        / "packages"
        / "contracts"
        / "schemas"
        / "merchant-profile.schema.json"
    ).read_text(encoding="utf-8")
)

#: 一份商家的声明。**顺序就是这个商家的优先级** —— 平台不重排。
#:
#: ⚠️ **这三个名字刻意不是字典序**：第一版写的是
#: `industry / pain_point / team_size`，**而那正好就是字典序** ——
#: 于是「按声明顺序」那条用例在「改成按字典序排」的变异下照样全绿。
#: **一个恰好与错误答案相同的夹具，证明不了正确答案。**
DECLARED = [
    {"key": "pain_point"},
    {"key": "industry"},
    {"key": "team_size"},
]


def _pipeline_source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _body(name: str) -> str:
    source = _pipeline_source()
    start = source.index(f"def {name}(")
    return source[start : source.index("\n    @staticmethod", start)]


# ---------------------------------------------------------------------------
# 一 · 值真的到得了两个提示词
# ---------------------------------------------------------------------------


def test_both_judging_steps_carry_the_values() -> None:
    """**理解步与计划步都带**，而不是只接上一个。

    只接一个是本仓「两头都在、中间没接」那一族的形状 ——
    `last_outbound` 那一批就在这里栽过：两个 `understand` 调用点只接了一个。
    """

    shown, hidden = memories_for_prompt(DECLARED, {"industry": "服装批发"})

    understand = request_shape.understand_prompt(
        goal="g", information_slots=[], known_memories=shown, memories_not_shown=hidden
    )
    plan = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        known_memories=shown,
        memories_not_shown=hidden,
    )

    assert understand["known_memories"] == [{"field": "industry", "value": "服装批发"}]
    assert plan["known_memories"] == understand["known_memories"], "两步看见的不是同一份"
    assert understand["memories_not_shown"] == plan["memories_not_shown"] == 0


def test_an_empty_memory_is_still_sent() -> None:
    """**空着也发。**

    「这个客户我们还什么都不知道」与「没人告诉你有记忆这回事」
    不该在模型眼里长得一样 —— 与 `last_outbound` / `content_assets` 同一条理由。
    """

    document = request_shape.understand_prompt(goal="g", information_slots=[])

    assert document["known_memories"] == []
    assert document["memories_not_shown"] == 0


# ---------------------------------------------------------------------------
# 二 · 顺序、词表、界
# ---------------------------------------------------------------------------


def test_the_order_is_the_merchants_not_the_platforms() -> None:
    """**按声明顺序，不按字典序、不按库里的顺序。**

    schema 自己写着理由：珠宝店要戒指尺寸与纪念日，航司要证件号与舱位偏好，
    补习班要孩子的年级与上课时间 —— **三套没有交集，平台永远猜不出下一个行业**。
    平台猜不出哪一条更重要，**而商家把它写在了声明的顺序里**。
    """

    shown, _ = memories_for_prompt(
        DECLARED,
        # 故意按相反顺序给，模拟库里 `SELECT` 回来的任意次序
        {"team_size": "12 人", "pain_point": "夜里没人接", "industry": "服装批发"},
    )

    assert [entry["field"] for entry in shown] == ["pain_point", "industry", "team_size"]
    assert [entry["field"] for entry in shown] != sorted(
        entry["field"] for entry in shown
    ), "夹具又变成字典序了 —— 那样这条用例就什么都不守了"


def test_a_field_the_merchant_no_longer_declares_is_not_shown() -> None:
    """**与写入闸同一份清单。**

    商家把一个字段从 `memory_fields` 里删掉，就是说「这件事我不再想记」。
    库里那行还在（它有 TTL、有留痕，删不删是另一条路上的事），
    **而它不该再出现在提示词里** —— 两处用同一个判据，不会一处认一处不认。
    """

    shown, hidden = memories_for_prompt(
        DECLARED, {"industry": "服装批发", "blood_type": "O"}
    )

    assert [entry["field"] for entry in shown] == ["industry"]
    assert hidden == 0, "没声明的不算「装不下」，它压根不该被算进来"


def test_a_profile_that_declares_nothing_shows_nothing() -> None:
    """没声明 = 一个都不带，**而不是「没说所以随便」**（与 `_declared_memory_fields` 同源）。"""

    assert memories_for_prompt(None, {"industry": "服装批发"}) == ([], 0)
    assert memories_for_prompt([], {"industry": "服装批发"}) == ([], 0)


def test_what_does_not_fit_is_counted_not_silently_dropped() -> None:
    """**装不下的要数出来。**

    一次安静的截断读起来与「这个客户没有记忆」一模一样，
    而这两件事对模型该做的事完全不同。
    """

    declared = [{"key": f"field_{index}"} for index in range(8)]
    values = {f"field_{index}": "运" * 100 for index in range(8)}

    shown, hidden = memories_for_prompt(declared, values)

    assert shown, "一条都没带上"
    assert len(shown) + hidden == 8, f"账对不上：带了 {len(shown)}，报了 {hidden}"
    assert hidden > 0, "这个夹具本来就该装不下 —— 它不再是那个夹具了"


def test_it_stops_at_the_first_one_that_does_not_fit() -> None:
    """**停下来，不跳过大的去捡小的。**

    跳过等于「按大小重排优先级」，而优先级是商家用声明顺序定的。
    可预测比多塞一条值钱。
    """

    declared = [{"key": "big"}, {"key": "small"}]
    shown, hidden = memories_for_prompt(
        declared, {"big": "运" * 100, "small": "一"}, budget=200
    )

    assert [entry["field"] for entry in shown] == [], "第一条就装不下，后面不该再挑"
    assert hidden == 2


def test_a_long_value_is_clipped_where_the_model_can_see_it() -> None:
    """截断留一个**看得见的记号** —— 模型要能分得出「这就是全部」与「后面还有」。"""

    shown, _ = memories_for_prompt([{"key": "industry"}], {"industry": "运" * 500})

    value = shown[0]["value"]
    assert value.endswith(ELLIPSIS)
    assert len(value.encode("utf-8")) <= REMEMBERED_VALUE_BYTES + len(
        ELLIPSIS.encode("utf-8")
    )


# ---------------------------------------------------------------------------
# 三 · T300 的三个数绑在一起
# ---------------------------------------------------------------------------


def test_the_three_numbers_of_t300_agree() -> None:
    """**份额、条数、schema 上限 —— 三者只能一起定**（T300 的到期条件原话）。

    | | |
    |---|---|
    | 份额 | `REMEMBERED_BYTES` = 1,200 字节／每个提示词 |
    | 一条真实记忆 | 值 ≤40 汉字 = 120 + 键约 16 + JSON 那一圈 24 ≈ 160 字节 |
    | 1,200 ÷ 160 | 7.5 → **8** |

    这条用例在**单改一个**时变红。它不是重算一遍 —— 它钉的是
    「这三个数是一起定的」这件事本身。
    """

    per_entry = 120 + 16 + 24
    assert REMEMBERED_BYTES // per_entry == 7, "算式的量级变了 —— 三个数要一起重定"
    assert SCHEMA["properties"]["memory_fields"]["maxItems"] == 8, (
        "schema 的上限与份额反推出来的对不上 —— 见 T300：三者只能一起定"
    )


def test_the_ceiling_is_not_the_provable_worst_case() -> None:
    """**刻意不按最坏值定**，而这是照裁定办的。

    被撤掉那道闸正是按最坏算的：`field_value` 允许 1024 个字符，
    中文满格 3,072 字节 —— 按它反推上限是 **3**。
    用户 2026-09-12 裁定：**「那是一次会改变产品形状的收窄，不是一次修正。」**
    """

    worst_case_entry = 1024 * 3
    assert REMEMBERED_BYTES // worst_case_entry < 1, "最坏值下一条都装不下 —— 所以不按它定"
    assert SCHEMA["properties"]["memory_fields"]["maxItems"] > 3


# ---------------------------------------------------------------------------
# 四 · 管线那半真的接上了
# ---------------------------------------------------------------------------


def test_every_judging_call_site_passes_it() -> None:
    """**读 AST，不数字符串。**

    `last_outbound` 那一批在这里栽过一次：数的是
    `last_outbound=prepared.last_outbound,` 出现几次，**而计划步那个调用点
    也有这一行** —— 删掉一个 `understand` 的还剩两处，判据照样成立。
    「数错了的守卫比没有守卫更坏：它看起来在守。」
    """

    tree = ast.parse(_pipeline_source())
    missing: list[int] = []
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"understand", "plan"}):
            continue
        found += 1
        if not any(keyword.arg == "known_memories" for keyword in node.keywords):
            missing.append(node.lineno)

    assert found >= 3, f"只找到 {found} 个判断步调用点，这条用例要跟着改"
    assert not missing, f"这些调用点没带记忆（行号 {missing}）"


def test_it_is_computed_once_for_the_turn() -> None:
    """**管线算一次，两步收同一份。**

    与 `turn_materials` / `turn_assets` 逐字同一条纪律：两处各算一遍，
    理解步与计划步会看见不同的记忆，而那种不一致只在真实回合里显形。

    算在这里还有第二个用处：**管线因此知道这一轮送了哪几条**，
    于是那一行留痕写得出来。
    """

    source = _pipeline_source()

    assert source.count("memories_for_prompt(") == 1, (
        "不止一处在算 —— 两处各算一遍就会漂"
    )


def test_the_reader_takes_the_values_and_only_the_active_ones() -> None:
    """**它从前只取键。**

    那条 docstring 把「只取键不取值」写成了原则，而它成立的前提是
    「除了写入闸没有第二个读者」—— 本批之后不成立了。
    """

    body = _body("_remembered_memories")

    assert "field_value" in body, "还是只取键 —— 那值就还是回不到提示词里"
    assert "status = 'active'" in body, "墓碑立了却还在被读"
    assert "FROM contact_memories" in body


def test_what_was_sent_is_recorded_and_the_record_carries_no_values() -> None:
    """**留痕只记键。**

    `memory_values.forbidden_value_reason` 的 docstring 记着那次事故：
    一条引用了它所拒的身份证号的拒绝记录，**已经把那个号写进审计面了**。
    """

    body = _body("_record_memories_sent")

    assert "'inference.memories_sent'" in body
    assert '"fields"' in body
    assert '"not_shown"' in body
    assert "field_value" not in body, "留痕里出现了值"
    assert '"value"' not in body, "留痕里出现了值"


def test_the_record_says_what_it_cannot_answer() -> None:
    """**它答「递过去了哪几条」，答不了「引了哪几条」** —— 差的那一跳登记在 T363。

    `app.py` 那条墓碑路由此前写着「记忆还没有进提示词（那是 E4-5），
    所以这一半不是欠账，是还没有发生」。**本批就是 E4-5**，
    所以那句话不再成立 —— 写清楚它答得了什么、答不了什么，
    比让下一个人以为它全答了便宜。
    """

    body = _body("_record_memories_sent")

    assert "T363" in body, "没说清它答不了什么"
    assert "cited" in body or "引了" in body
