"""取值层的禁止项绊线（T298）——**以及它挡不住什么**。

E4-1b 的闸读的是商家给列取的名字，它自己登记了缺口：写 `id_card_no` 挡得住，
写 `customer_extra_2` 挡不住。**T298 的到期条件写死「E4-2 / E4-3 落地时」，
E4-3 是它点名的最后一批**，所以本批兑现。

**本文件最值钱的不是「它拦得住」那几条，是「它拦不住」那几条。**
一个只断言拦得住的用例文件，会让下一个读到「平台拒绝敏感字段」的人
以为这件事已经解决了——**而那正是 T298 当初被登记下来要防的误读**。
"""

from __future__ import annotations

import pytest
from fastagent_v2.runtime.memory_values import (
    FORBIDDEN_CATEGORIES,
    forbidden_value_reason,
)

#: 校验位真的对得上的合成身份证号。**不是任何真人的**：
#: 地址码 `110101`（北京东城）、出生日期 `19900307`、顺序码 `425`，
#: 末位按 GB 11643-1999 的加权和算出来。
VALID_ID = "110101199003074258"

#: Luhn 通过且前缀是 Visa 的测试卡号，**支付网络的公开测试号**，不是真卡。
VALID_CARD = "4111111111111111"


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (VALID_ID, "identity number"),
        (f"她说是 {VALID_ID}，让我们记一下", "identity number"),
        (VALID_CARD, "payment card"),
        ("4111 1111 1111 1111", "payment card"),
        ("4111-1111-1111-1111", "payment card"),
    ],
)
def test_a_forbidden_value_is_refused_whatever_the_field_is_called(
    value: str, category: str
) -> None:
    """**这一族就是 T298 登记的那个场景**：列叫什么都不影响判断。

    参数里没有 `field_key`——本函数根本拿不到它，**而那是设计**：
    声明层的闸看名字，取值层的闸看值，两层各看各的。
    """

    reason = forbidden_value_reason(value)
    assert reason is not None, f"{category} 没有被拦下来"
    assert category in reason


@pytest.mark.parametrize(
    "value",
    [
        "18",  # 手寸
        "18 号圈口，她妈妈戴的",
        "2026-03-07",
        "三年级",
        "订单号 SO20260307001",
        # 17 位，短一位——**不是身份证**
        "11010119900307425",
        # 18 位但校验位错
        "110101199003074259",
        # Luhn 过了但前缀不是任何已知发卡行（`9` 开头）
        "9111111111111116",
        # 前缀像 Visa 但 Luhn 不过
        "4111111111111112",
    ],
)
def test_an_ordinary_memory_is_not_refused(value: str) -> None:
    """**过度拒绝有代价，所以两个判据都要满足才拒。**

    `"18"` 是这个仓自己的主判据里那句话——「我妈妈戴 18 号圈口」。
    一道会把它拦下来的闸，不是严格，是坏了。
    """

    assert forbidden_value_reason(value) is None


def test_the_refusal_never_repeats_the_value_it_refused() -> None:
    """**一条拒绝身份证号的错误信息，不能把身份证号写进日志。**

    E4-2 在同族的地方刚付过这笔钱：`_record_rate_limit` 把 `withheld_content`
    （**客户事实本身**）原样写进了 `policy_decisions.context`
    ——「一条防着模型乱记的闸，把被拒的内容原样写进了审计面」
    （独立审查 2026-09-12 P1-1）。**这一条是那次教训的回归基线。**
    """

    reason = forbidden_value_reason(VALID_ID)
    assert reason is not None
    assert VALID_ID not in reason
    assert "425" not in reason, "连片段都不行——号的一部分仍然是号的一部分"

    card_reason = forbidden_value_reason(VALID_CARD)
    assert card_reason is not None
    assert VALID_CARD not in card_reason


def test_two_of_the_four_forbidden_categories_are_out_of_reach() -> None:
    """**这一条是本文件存在的主要理由。**

    ADR-0131 M2 点名四类：身份证号、支付信息、健康、宗教。
    **后两类没有可计算的形状**——`"糖尿病"` 是一串字符，和 `"18 号圈口"` 一样，
    没有任何校验位能把它认出来。

    所以取值层这道闸**只覆盖四类里的两类**，另外两类仍然只在声明层
    （商家给列取的名字）被拦。**断言这件事，而不只是在注释里说**：
    一条注释会随着下一次改动漂走，而这条用例会在有人声称"四类全覆盖"时变红。
    """

    reachable = {name for name, seen in FORBIDDEN_CATEGORIES.items() if seen}
    assert reachable == {"identity number", "payment details"}
    assert FORBIDDEN_CATEGORIES["health"] is False
    assert FORBIDDEN_CATEGORIES["religion"] is False


@pytest.mark.parametrize(
    "disguised",
    [
        "一一零一零一一九九零零三零七四二五八",  # 汉字数字
        "110101 1990 0307 4258",  # 按身份证的语义分段（本闸只认卡的分段）
        "MTEwMTAxMTk5MDAzMDc0MjU4",  # base64
    ],
)
def test_a_determined_bypass_still_gets_through(disguised: str) -> None:
    """**绊线的边界，钉成用例而不是写成注释。**

    `_check_memory_fields` 当初被要求写下「它挡得住没多想，挡不住换个名字」；
    取值这一层欠的是同一句话的另一个版本，**而它是一句不同的话**：
    这里挡得住「原样粘贴」，挡不住「改个写法」。

    **这条用例故意是绿的，而它的绿色是一条结论**：
    任何人不得把这个模块读成「平台保证不会记下敏感信息」。
    哪天有人真做了规范化再判，这一条会变红，**那时该改的是它的名字与断言，
    不是删掉它**。
    """

    assert forbidden_value_reason(disguised) is None
