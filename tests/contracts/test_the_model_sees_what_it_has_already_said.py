"""运维报了三次的那一条：**模型每一轮都像第一次看到这些素材**。

## 现场（`ops-to-author-reply-quality-2026-09-17.md`，三处）

§8.4：

> 帧定义里它是必填的：`{"name": "material_delivered", "required": true}`，
> 帧 `goal` 里也写着「记已讲过哪几段」。**而这通会话的槽现读只有
> `{"concern_topic": "花钱"}`。**
>
> **服务端其实知道讲过哪几段**（`pipeline._delivered_materials` 从
> `intent_records.proposal` 里把 ref 捞出来，按发出顺序去重），
> `delivered=` 也确实传给了选取那一步。**但模型那一侧没有这个记录** ——
> **它每一轮都像第一次看到这些素材。**

§10.2 第 3 条、§11.3 各报一次，**同一件事被报了三遍**。

## 现读核过：三个提示词一个都不带它

`grep -n "delivered" packages/runtime/src/fastagent_v2/runtime/request_shape.py`
在这一批之前 **0 命中**。

**两头都在、中间没接**：平台每轮算得出，而模型每轮看不见。

## 为什么只给理解步与计划步，不给生成步

**选哪几段是计划那一步的事**，生成步只从递过去的清单里引。
而生成步的预算是三个角色里最紧的那一个：`MATERIAL_BYTES_BUDGET` 的推导式
从它的空请求起算，安全余量今天 **5,035** 字节。
往它身上挂一串**长度随对话增长**的清单，等于拿那条余量去赌 ——
上一批往生成步系统提示补 245 字节，余量当场掉到 4,790，那一次撤了。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.request_shape import MATERIALS_SENT_SHOWN

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
PROFILES = REPOSITORY_ROOT / "profiles"

#: 逐字取自运维 §10.1 那一轮真的发出去过的两段。
SENT = (
    "material:demo-open-what-we-do:v1",
    "material:demo-price-plain:v1",
)


def _pipeline_source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _understand(**kwargs: object) -> dict[str, object]:
    return request_shape.understand_prompt(goal="g", information_slots=[], **kwargs)


def _plan(**kwargs: object) -> dict[str, object]:
    return request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 一 · 两个判断步都看得见它
# ---------------------------------------------------------------------------


def test_both_judging_steps_see_what_was_already_said() -> None:
    """**理解步与计划步收同一份。**

    理解步要它：客户说「上次那个」时，「那个」是什么只在这份清单里；
    计划步要它：**「上次讲到开通流程」正是跟进那一句要接住的东西**。
    """

    understand = _understand(materials_already_sent=SENT)
    plan = _plan(materials_already_sent=SENT)

    assert understand["materials_already_sent"] == list(SENT)
    assert plan["materials_already_sent"] == list(SENT), "两步看见的不是同一份"


def test_an_empty_conversation_still_sends_the_key() -> None:
    """**空着也发** —— 与 `last_outbound` / `known_memories` 同一个理由。

    「这通对话我们还没讲过话术」与「没人告诉你有这回事」
    不该在模型眼里长得一样。
    """

    assert _understand()["materials_already_sent"] == []
    assert _plan()["materials_already_sent"] == []


def test_the_generation_step_gets_it_too_since_the_correction() -> None:
    """**这条用例反过来了，而那是一次更正。**

    上一版叫 `test_the_generation_step_deliberately_does_not_get_it`，
    断言生成步**不**收这份清单，理由是「选哪几段是计划那一步的事」
    加上「生成步的预算最紧」。

    **用户 2026-09-18 当场更正**（批 E）：

    > **上一句不一定是关键，之前的内容才是** ——
    > 有可能结束前聊的东西和我们的目标没什么关系。

    跟进那一句要接住的是**聊过的那件事**，而写那句话的正是生成步。
    「选材」那个理由对**选**成立，对**写**不成立。

    **预算那个理由没有被绕过，是被付掉了**：批 E 为它扣了
    `MATERIALS_SENT_BYTES_RESERVED`（1,100），
    `MATERIAL_BYTES_BUDGET` 跟着降，**安全余量一个字没动**。
    """

    # ⚠️ **先断言内容真的到了，不是「键在不在」**：探针 A（把那个键改成
    # 恒为空列表）在补这一行之前**打不红这条用例** —— 键还在，而清单没了。
    # **空断言这一族，本轮第三次。**
    document = request_shape.generate_prompt(materials_already_sent=SENT)

    assert document["materials_already_sent"] == list(SENT)
    assert request_shape.generate_prompt()["materials_already_sent"] == [], (
        "空着也该发这个键 —— 与 last_outbound 同一个理由"
    )

    from fastagent_v2.worker.material_selection import (  # noqa: PLC0415
        MATERIALS_SENT_BYTES_RESERVED,
        SAFETY_MARGIN_BYTES,
    )

    assert MATERIALS_SENT_BYTES_RESERVED == 1100, "进去了而没扣预算 —— 那就是拿余量去赌"
    assert SAFETY_MARGIN_BYTES == 5000


# ---------------------------------------------------------------------------
# 二 · 界：今天咬不到任何一份在架配包
# ---------------------------------------------------------------------------


def test_it_keeps_the_tail_not_the_head() -> None:
    """**留尾不留头** —— 「上次讲到哪」问的是最近讲的那几段。"""

    refs = [f"material:filler-{index:03d}:v1" for index in range(MATERIALS_SENT_SHOWN + 5)]

    shown = _understand(materials_already_sent=refs)["materials_already_sent"]

    assert len(shown) == MATERIALS_SENT_SHOWN
    assert shown[-1] == refs[-1], "截掉的是尾巴 —— 那正是最该留下的那几段"
    assert refs[0] not in shown


def test_the_cap_is_above_every_shipped_frame() -> None:
    """**这个界今天咬不到任何一份在架配包。**

    哪天有配包的一帧超过它，**这条用例先红** ——
    于是「要不要抬这个数」变成一次决定，而不是一次悄悄的截断。

    这是本仓对「界」的一贯要法：有界，而且**证明它现在不在咬**。
    """

    biggest = 0
    where = ""
    for package in sorted(PROFILES.iterdir()):
        document = json.loads(
            (package / "profile.json").read_text(encoding="utf-8")
        )
        for frame in document["target_frames"]:
            count = len(frame.get("content_materials") or [])
            if count > biggest:
                biggest, where = count, f"{package.name}/{frame['id']}"

    assert biggest > 0, "一份配包都没读到 —— 这条用例什么都没量"
    assert MATERIALS_SENT_SHOWN >= biggest, (
        f"{where} 有 {biggest} 段素材，而上限是 {MATERIALS_SENT_SHOWN} —— "
        "它开始咬在架配包了，抬这个数之前先看理解步/计划步那两个请求会大多少"
    )


# ---------------------------------------------------------------------------
# 三 · 那份清单是平台自己的记录，不是模型报的
# ---------------------------------------------------------------------------


def test_it_comes_from_the_platforms_own_record() -> None:
    """**不是模型自填的 `material_delivered` 槽。**

    同一个仓里那条注释写着理由：
    「Not the model's report… guess wrong, select the wrong materials,
    and **nothing goes red**」——
    而这一格问的正是「已经讲过哪几段」，拿模型的记账去答它等于没答。
    """

    tree = ast.parse(_pipeline_source())
    passed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"understand", "plan"}):
            continue
        for keyword in node.keywords:
            if keyword.arg != "materials_already_sent":
                continue
            passed.append(ast.unparse(keyword.value))

    assert len(passed) >= 3, f"只有 {len(passed)} 个判断步带上了它"
    assert set(passed) == {"prepared.delivered_materials"}, (
        f"有调用点传的不是平台那份现读：{sorted(set(passed))}"
    )


def test_every_judging_call_site_passes_it() -> None:
    """**读 AST，不数字符串** —— 这个文件上栽过一次。"""

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
        if not any(
            keyword.arg == "materials_already_sent" for keyword in node.keywords
        ):
            missing.append(node.lineno)

    assert found >= 3, f"只找到 {found} 个判断步调用点，这条用例要跟着改"
    assert not missing, f"这些调用点没带上它（行号 {missing}）"


def test_only_the_refs_travel_not_the_words() -> None:
    """**只给 ref，不给正文。**

    这一轮该不该再引某一段由选取那一步判；这里要答的是
    「哪几件事已经讲过了」—— 答它不需要把话再抄一遍，
    而每抄一遍都是两个提示词各付一次。
    """

    shown = _understand(materials_already_sent=SENT)["materials_already_sent"]

    # ⚠️ **先断言非空**：`all(...)` 在空列表上恒真 —— 反向探针 A
    # （把这份清单改成永远空）在补这一行之前**打不红这条用例**。
    assert shown, "清单是空的 —— 下面两句在空列表上恒真，那时它什么都没守"
    assert all(str(ref).startswith("material:") for ref in shown)
    assert all(len(str(ref)) < 120 for ref in shown), "看起来像是把正文也带上了"
