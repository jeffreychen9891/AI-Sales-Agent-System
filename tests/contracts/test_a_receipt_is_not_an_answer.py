"""回执不是答案，而那句回执是我们自己教的（运维缺陷 15 / 16，2026-09-23）。

## 缺陷 15：配包禁止的短语，平台提示词里给了范例

配包 goal 明文写着：

> 「我记下了」「我听到了」「我收到了」「我知道了」「我来安排」**那是回执不是答案**

而生成步的**示例**逐字是：

```json
{"strategy_id": "acknowledge",
 "customer_visible_segments": [{"type": "text", "content_or_ref": "收到。"}]}
```

**两份文档对模型说了相反的话，而范例的说服力强过散文禁令。**
stage 实测出站两条：「晚上没人回消息，这个我记下了。」「收到，报价表我这边记下了。」

### ⚠️ 用户裁定：根除，不加词表闸

> 「**想办法根除 最好的方法是让模型别复述和确认收到的话
> 词汇无法穷尽闸是加不完的**」

所以本批**不加**「这五个短语不许出现」那种闸 —— 那种闸挡得住这五个，
挡不住第六个。做的是两件：**换掉那个范例**，**把规则写成正面的**。

## 缺陷 16：摘要为什么会记错 —— 结构性

`turn_summary` 由**理解步**产出，而理解步跑在计划与生成之前 ——
**那一刻回复还不存在**。而契约与提示词都要它答
「what the customer wanted **and what we did**」。

**后半句在结构上不可能答对。** 模型只能猜，而它猜了
「本轮未给出实质回答」——**回答就在同一轮的第一条**。
那句猜测进了滚动摘要，`request_shape` 让后面每一轮都读到它。

## 这两条为什么在一个文件里

它们是同一个形状的两面：**平台让模型去做一件它做不到的事，
然后责怪输出**。一个是要它写还不存在的东西，一个是给了它一个
与禁令相反的范例。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastagent_v2.worker.inference_model import (
    _GENERATE_SYSTEM,
    _UNDERSTAND_SYSTEM,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
CONTRACT = (
    REPOSITORY_ROOT / "packages" / "contracts" / "inference" / "understanding-result.schema.json"
)

#: 那五个回执句。**2026-09-24（缺陷 25）起它们的身份变了**：
#: 先前是「配包点名禁止的五个」，现在是「配包与范例都不许出现的五个」——
#: 复测实测点名式禁令的例句被模型当措辞样本借走（连反例整句的形状一起），
#: 所以 goal 侧改成只描述动作（「回执式短句…不是答案」），字面量一个不留。
RECEIPTS = ("我记下了", "我听到了", "我收到了", "我知道了", "我来安排")


def test_the_package_still_forbids_them() -> None:
    """禁令还在，而**不再点名** —— 缺陷 25（2026-09-24）把点名本身定成了缺陷。

    先前这条要求五个短语**逐个出现在 goal 里**（「点名才说得清禁的是什么」）。
    复测推翻了它：模型把反例里的词（「接住了」）借去当业务概念，
    最后照着反例的形状把客户的话复述了回去 ——
    **对模型而言，禁令里的例句同时是措辞样本**，后一半没人想要。

    现在两半都钉：**字面量拆净**（教材没收），**行为禁令仍在**（四个业务帧
    各有一句「回执式短句…不是答案/不是回答」）。
    """

    frames = json.loads(PACKAGE.read_text(encoding="utf-8"))["target_frames"]
    goals = {str(f.get("id")): str(f.get("goal") or "") for f in frames}
    joined = " ".join(goals.values())
    taught = [phrase for phrase in RECEIPTS if phrase in joined]
    assert not taught, (
        f"goal 又点名了 {taught} —— 缺陷 25 实测：点名的例句会被当措辞样本借走"
    )
    for frame_id in ("introduce", "quote", "trial_close"):
        assert "回执式短句" in goals[frame_id] and "不是答案" in goals[frame_id], (
            f"{frame_id} 的行为版禁令没了 —— 字面量拆掉之后它是唯一的那道禁令"
        )
    assert "回执式短句" in goals["objection"] and "不是回答" in goals["objection"]


@pytest.mark.parametrize("phrase", RECEIPTS)
def test_no_example_teaches_the_phrase_the_package_forbids(phrase: str) -> None:
    """⚠️ **范例不许示范被禁的话。**

    这一条挡的不是「模型说了这句」（那挡不住，词汇穷尽不完），
    **是「我们教了它这句」** —— 而后者是可以穷尽的：
    提示词是我们自己写的，它有多长我们知道。
    """

    # ⚠️ **按位置判，不按「这几个字在不在」判。** 第一版写成后者，
    # 而它误伤了**新禁令自己那句**（禁令要点名才说得清禁的是什么）——
    # 同一个错今天在另一份用例里也犯过：
    # 一个只问「某几个字在不在」的判据，分不出「在教它写」与「在禁止它写」。
    #
    # **范例是那几段 JSON**，从第一处 `"strategy_id":` 起到末尾。
    examples = _GENERATE_SYSTEM[_GENERATE_SYSTEM.index('"strategy_id":') :]
    assert phrase not in examples, (
        f"生成步的**范例**里出现了配包禁止的「{phrase}」—— "
        "范例的说服力强过散文禁令，模型会照着范例写"
    )


def test_the_acknowledge_example_answers_something() -> None:
    """**那个范例现在答的是一件事，不是一张回条。**

    原来它是 `"收到。"` —— 一个字不差地示范了配包禁止的那件事。
    换掉它是本批的根除动作：**不加闸，改范例。**
    """

    assert "收到。" not in _GENERATE_SYSTEM, "那个「收到。」范例还在"
    assert '"strategy_id": "acknowledge"' in _GENERATE_SYSTEM, (
        "acknowledge 那个范例被整个删了 —— 它还有用，只是内容要换"
    )


def test_the_rule_is_positive_not_a_blocklist() -> None:
    """⚠️ **规则说的是「怎么算懂了」，不是「这几个词不许说」。**

    用户 2026-09-23：「词汇无法穷尽，闸是加不完的」。
    一条只列词的规则，模型换个说法就绕过去了；
    一条说清「用他说的话去做事，而不是说你听懂了」的规则，管的是那个行为。
    """

    assert "a receipt is not an\n  answer" in _GENERATE_SYSTEM or (
        "a receipt is not an answer" in _GENERATE_SYSTEM
    ), "没有那句「回执不是答案」"
    assert "Understanding is shown by using what he said" in _GENERATE_SYSTEM, (
        "只列了禁词，没说该怎么做 —— 那种规则换个说法就绕过去了"
    )
    assert "Do not restate his words back to him" in _GENERATE_SYSTEM
    assert "a question asked is a question to answer now" in _GENERATE_SYSTEM, (
        "「这条先不展开，回头专门说」那个形状没被挡住 —— "
        "用户 2026-09-23：「销售情景不可能别人问了我们不第一时间讲」"
    )


def test_the_summary_is_only_asked_about_his_half() -> None:
    """⚠️ **缺陷 16 的根因：它被要求写一件它写不了的东西。**

    `turn_summary` 由理解步产出，而回复那一刻还不存在。
    """

    assert "the reply does not exist yet" in _UNDERSTAND_SYSTEM, (
        "提示词没说清「回复还不存在」—— 少了它，模型仍然会去写它猜的结果"
    )
    assert "never say what we did" in _UNDERSTAND_SYSTEM
    assert "one line on this turn only" not in _UNDERSTAND_SYSTEM, (
        "旧那句「what he wanted and what we did」还在"
    )


def test_the_contract_says_the_same_thing_and_says_it_short() -> None:
    """⚠️ **两个定义点要一致，而契约那份必须短。**

    契约的 description **整份进请求**：第一版把成因留痕写进去，
    实测让响应 schema 从 **3,300 涨到 3,786（+486）**，**每回合都付**。
    成因搬到了 `inference_model.py` 的 v15 那段注释里，
    契约只留那一句最短的。
    """

    description = json.loads(CONTRACT.read_text(encoding="utf-8"))["properties"][
        "turn_summary"
    ]["description"]

    assert "the reply does not exist yet" in description, "契约那半没说清同一件事"
    assert "what we did" not in description, "契约还在要它写「我们做了什么」"
    assert len(description.encode("utf-8")) < 320, (
        f"契约的 description 长到 {len(description.encode('utf-8'))} 字节 —— "
        "它整份进请求，每回合都付；成因写进代码注释，不写在这里"
    )
