"""`remember` 的写入路径：**它写一行记忆，一个字都不发**。

本文件守的是 E4-2 的四件，**每一件都是「错了也不会吵」的那种**：

1. `remember` 的效果是 `DirectWrite`——**排一行出站消息会很安静地错**：
   记忆写进去了、看起来一切正常，只是投递器那边多了一行发不出去的命令；
2. 两个文本列的上界与 `054` 对得上，**且单位是字符**；
3. worker 与 profile 注册表说的是同一个键名（`memory_fields`）——
   **两处字符串，一处改名另一处不会报错，只会让 allowlist 悄悄变成空集**；
4. 四条处境闸各自拒对了东西。

**为什么不靠「跑一回合看看」**：那些也要有（见 §四的说明），
但一条端到端用例红的时候，指不出是六道闸里的哪一道。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.profiles.registry import MEMORY_FIELDS_KEY as REGISTRY_MEMORY_FIELDS_KEY
from fastagent_v2.worker.pipeline import (
    GENERATION_WRITE_PATHS,
    MEMORY_FIELD_KEY_MAX_CHARS,
    MEMORY_FIELD_VALUE_MAX_CHARS,
    MEMORY_FIELDS_KEY,
    DirectWrite,
    PreparedEvent,
    _remember_payload_refusal,
    _remember_turn_refusal,
)

ROOT = Path(__file__).resolve().parents[2]

MIGRATION = ROOT / "infrastructure/migrations/054_contact_memories.sql"

ENVELOPE_SCHEMA = ROOT / "packages/contracts/schemas/message-envelope.schema.json"

CONTACT = UUID("11111111-1111-4111-8111-111111111111")


def _non_customer_event_types() -> list[str]:
    """契约里除 `customer_message` 之外的每一个 `event_type`。

    **从契约读，不在用例里抄一份。** 抄一份的那一版为什么是空的，
    见 `test_any_turn_that_is_not_a_customer_message_is_refused` 的 docstring。
    """

    schema = json.loads(ENVELOPE_SCHEMA.read_text(encoding="utf-8"))
    values = [str(value) for value in schema["properties"]["event_type"]["enum"]]
    assert "customer_message" in values, "契约里那个 enum 变了形状，这条用例要跟着看一眼"
    others = [value for value in values if value != "customer_message"]
    assert others, "enum 里只剩一个值时这条用例什么都没跑 —— 空集合会静默全绿"
    return others


def _prepared(
    *,
    event_type: str = "customer_message",
    contact_id: UUID | None = CONTACT,
    declared: tuple[str, ...] = ("ring_size",),
    remembered: tuple[str, ...] = (),
) -> PreparedEvent:
    """一个够用的 `PreparedEvent`，**只填这四条闸问到的东西**。

    其余字段给空壳：填一份看起来完整的假数据，会让读的人以为
    这条用例在测比它实际测的更多的东西。
    """

    return PreparedEvent(
        inbox_id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        envelope={"event_type": event_type, "event_id": str(uuid4())},
        profile={MEMORY_FIELDS_KEY: [{"key": key} for key in declared]},
        aggregate={},
        previous_state_version=0,
        previous_generation=0,
        contact_id=contact_id,
        remembered_fields=remembered,
    )


def _payload(key: str = "ring_size", value: str = "18 号") -> dict[str, Any]:
    return {"field_key": key, "field_value": value}


# ---------------------------------------------------------------- 效果


def test_remember_writes_a_row_and_queues_nothing() -> None:
    """**本批最值钱的一条。**

    `remember` 改变的是我们记住了什么，**没有任何一个字送到客户那端**。
    它若被登记成 `OutboxDelivery`，每条记忆都会排一行**没有渠道能发**的出站消息——
    而那一行不会让任何用例变红：记忆写进去了，投递器那边只是多了一行卡住的命令。
    **正是 `GENERATION_SUPPORTED_KINDS` 上方那段注释用来论证「今天不注册
    `send_material`」的那个形状**，只不过换成了 `remember`。
    """

    effect = GENERATION_WRITE_PATHS["remember"].effect
    assert isinstance(effect, DirectWrite), (
        "remember 的效果必须是 DirectWrite——它不发任何东西。"
        "改成 OutboxDelivery 会给每条记忆排一行发不出去的出站行，且没有东西会红。"
    )
    assert callable(effect.write)


def test_the_registry_is_what_decides_the_effect_now() -> None:
    """第 3 处**关对了**的那一半（T308）。

    接替 E3-3 那条绊线（`test_site_three_is_still_open_...`，本批按它的说明删掉）：
    绊线守的是「第 3 处还开着」，**这一条守的是它关上之后没有再退回去**。

    扫源码，因为要证的是「`_persist_accepted_proposal` **不再**自己决定排不排出站行」——
    一个把 `INSERT INTO outbox_messages` 写回那个函数里的人不会让任何运行期用例变红。
    """

    source = (
        Path(__file__).resolve().parents[2]
        / "services/worker/src/fastagent_v2/worker/pipeline.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def _persist_accepted_proposal(")
    end = source.index("\n    def ", start + 1)
    body = source[start:end]

    assert "GENERATION_WRITE_PATHS" in body, "接受一个提案之后干什么，必须由注册表条目决定"
    assert "INSERT INTO outbox_messages" not in body, (
        "出站行又长回 `_persist_accepted_proposal` 里了——"
        "那样 `remember` 会重新开始排发不出去的行。"
    )
    assert '"audience": "customer"' not in body, "收件对象又写死了（第 3 处的另一半）"


# ---------------------------------------------------------------- 量纲与命名


def test_the_bounds_match_the_migration_and_they_are_characters() -> None:
    """上界照 `054` 抄，**而 `054` 数的是字符**。

    **这条用例是 E4-1b 那次的价钱。** 那一批把 `054` 的 `1024` 当成字节做了一道闸，
    用户 2026-09-12 裁定撤掉——`length()` 数字符，`octet_length()` 才数字节
    （`050` 在真要字节的地方用的是后者）。

    **两个数写在两个文件里，而只有这条用例会在它们漂开时红。**
    """

    sql = MIGRATION.read_text(encoding="utf-8")
    found = dict(
        (name, int(limit))
        for name, limit in re.findall(
            r"CHECK \(length\((field_key|field_value)\) BETWEEN 1 AND (\d+)\)", sql
        )
    )
    assert found == {
        "field_key": MEMORY_FIELD_KEY_MAX_CHARS,
        "field_value": MEMORY_FIELD_VALUE_MAX_CHARS,
    }, (
        f"迁移里的上界是 {found}，而 pipeline 里的是 "
        f"{MEMORY_FIELD_KEY_MAX_CHARS} / {MEMORY_FIELD_VALUE_MAX_CHARS}"
    )

    assert "octet_length(field_" not in sql, (
        "054 改用 octet_length 了——那样上界的单位从字符变成字节，"
        "pipeline 里按 len() 数字符的守卫就不再是同一把尺。"
    )


def test_the_worker_and_the_registry_name_the_same_key() -> None:
    """`memory_fields` 这个字符串在仓里有两处，**而唯一定义点在 profile 注册表**。

    worker 不能 import api 服务，所以 `pipeline.py` 自己写了一份。
    **改名的人只会改到一处，而漏掉的那一处不会报错**——
    它只会让 `_declared_memory_fields` 读到 `None`、allowlist 变成空集，
    于是**每一条 `remember` 都被拒，且理由看起来完全合理**（「这个字段没声明」）。
    **一个静默地把功能关掉的改名。**
    """

    assert MEMORY_FIELDS_KEY == REGISTRY_MEMORY_FIELDS_KEY


# ---------------------------------------------------------------- payload


def test_the_payload_needs_both_halves() -> None:
    assert _remember_payload_refusal(_payload()) is None

    for broken in ({"field_key": "ring_size"}, {"field_value": "18 号"}, {}):
        detail = _remember_payload_refusal(broken)
        assert detail is not None
        assert "remember" in detail, "拒绝理由要说清它在替哪个 kind 说话"


def test_the_payload_refusal_is_bounded_and_counts_characters() -> None:
    """`detail` 有界（2026-09-07 P3-1 立的规矩），**而长度按字符数**。

    用一个全中文的值：**按字节它早就超了，按字符正好没超**——
    两种数法在这条用例上给出相反的答案，所以它测得出用的是哪一种。
    """

    just_fits = "中" * MEMORY_FIELD_VALUE_MAX_CHARS
    assert _remember_payload_refusal(_payload(value=just_fits)) is None, (
        "1024 个汉字按字符正好到界；这里若红，说明守卫在数字节，与 054 的 length() 不是同一把尺"
    )

    detail = _remember_payload_refusal(_payload(value="中" * (MEMORY_FIELD_VALUE_MAX_CHARS + 1)))
    assert detail is not None
    assert len(detail) <= 500


# ---------------------------------------------------------------- 处境


def test_a_timer_turn_has_no_customer_words_behind_it() -> None:
    """反向判据「客户没说过的事不许记」**在平台能验的范围内的那一条**。

    定时器回合里**一个字都不是客户说的**，据此记下来的必然是凭空的。
    """

    detail = _remember_turn_refusal(_payload(), _prepared(event_type="timer"))
    assert detail is not None
    assert "timer" in detail


def test_any_turn_that_is_not_a_customer_message_is_refused() -> None:
    """**极性**（独立审查 2026-09-12，P3-2）：正向判「是不是客户消息」，
    而不是反向列举要拒的那几种。

    第一版写的是 `== "timer"` 即拒。`event_type` 的值域不止两个——
    那几种**同样一个字都不是客户说的，却都能过那道闸**。

    **今天够不着**（全仓只有 adapter 产 `customer_message`、scheduler 产 `timer`），
    **所以这条用例是它唯一的守卫**：下一个 `event_type` 出现时，
    关门方向会拒它，开门方向会放它进来，而两者在别的用例里都是绿的。

    ## 2026-09-16：值域从契约读，因为**这条唯一的守卫先前是空的**

    独立复核 2026-09-16 实测：把 `pipeline.py` 那道闸改写成反向列举**本用例
    迭代的那四个名字 + `timer`**，`tests/contracts` **全绿**，
    而行为确实分开了（`channel_event` 原状被拒、变异下放行）。

    成因是这里手抄的那四个名字与契约对不上**两个方向**：
    `manager_comment` **不在** `message-envelope.schema.json` 的 enum 里
    （全仓只有本用例与一段注释提过它），而 enum 里真有的 `channel_event`
    **不在这份清单里**、生产 Python 里也 0 命中。
    **于是它迭代的集合恰好等于反向列举会列的集合，两种极性在它眼里一样绿。**

    `CLAUDE.md` 第三节末：「守卫本身也可能是空的 —— 一条断言今天为真，
    明天也可能只是因为它什么都没检查。」**这是那句话的第二个实例。**
    改成从契约读之后，契约里新增一个值，这条用例自动把它跑上。
    """

    for event_type in _non_customer_event_types():
        detail = _remember_turn_refusal(_payload(), _prepared(event_type=event_type))
        assert detail is not None, (
            f"{event_type!r} 的回合放行了——这道闸要正向判 customer_message，"
            "不是列举已知的那几种要拒的"
        )
        assert event_type in detail


def test_there_has_to_be_somebody_to_remember_about() -> None:
    """没有 contact 就没有地方放。

    **不在这里拒就是一次 NOT NULL 违例**——异常回滚整个回合、
    inbox 事件不被消费、退回重试预算里再炸一次（T110 的形状）。
    """

    detail = _remember_turn_refusal(_payload(), _prepared(contact_id=None))
    assert detail is not None
    assert "contact" in detail


def test_only_fields_the_profile_declares() -> None:
    """ADR-0131 M2：词表归商家，**而「要有一份封闭的清单」归平台**。

    **这是 `memory_fields` 的第一个运行期消费方**——
    在本批之前它只被封版校验读过（E4-1b 的禁止项与重复 key 两道闸）。
    """

    assert _remember_turn_refusal(_payload(key="ring_size"), _prepared()) is None

    detail = _remember_turn_refusal(_payload(key="blood_type"), _prepared())
    assert detail is not None
    assert "blood_type" in detail


def test_an_undeclared_profile_remembers_nothing() -> None:
    """没声明 `memory_fields` = **一个都不记**，不是「没说所以随便」。

    缺省朝哪边倒，是这道闸唯一真正的设计决定。
    """

    detail = _remember_turn_refusal(_payload(), _prepared(declared=()))
    assert detail is not None


def test_the_model_may_append_but_never_rewrite() -> None:
    """裁定八：**模型只能追加，不能重写已有的那一份。**

    表上 `contact_memories_one_active_per_field_idx` 保证一个字段只有一条 active——
    **而在这里拒和撞唯一索引是两回事**：撞索引是异常（整轮回滚），
    拒是一条带原因的记录，运维看得见「模型想改客户的手寸，我们没让」。
    """

    detail = _remember_turn_refusal(
        _payload(key="ring_size"), _prepared(remembered=("ring_size",))
    )
    assert detail is not None
    assert "ring_size" in detail


def test_a_different_field_is_not_blocked_by_an_occupied_one() -> None:
    """**占位是按字段算的，不是按 contact 算的。**

    一条只看「这个 contact 有没有记忆」的实现会在第一条记忆之后
    **把所有字段一起锁死**，而它在上一条用例里照样绿。
    """

    prepared = _prepared(declared=("ring_size", "anniversary"), remembered=("ring_size",))
    assert _remember_turn_refusal(_payload(key="anniversary"), prepared) is None
