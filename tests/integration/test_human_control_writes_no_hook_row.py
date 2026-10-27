"""**B2-4：人工接管期间，行为在，而 `pre_generation` 那一行不在。**

## 它是 D1 那条裁定唯一的行为面执行者

ADR-0137 §二（裁定二）取的是「**只统一输出，不改结构**」，而这句话的全部内容
就是：**我们有意不去补 hook 契约**。`tests/contracts/test_policy_hook_shape.py`
从静态面钉住了「`pre_generation` 零写者」，**而静态面证明不了另一半** ——
它看不出「那件该被 hook 记下来的事，今天确实发生了」。

**没有这一份，D1 关掉之后「我们有意不用 hook 契约」就没有任何执行者。**

## 这一轮真实发生的事（ADR-0054 §三.1，RFC §4.2 / §8）

| | 接管期间 |
|---|---|
| `understand` | **跑** |
| `apply_slot_updates` | **跑**，槽位真的写回聚合 |
| `plan` / `generate` | 不跑 |
| 目标栈 | **冻结**，这一轮不迁移 |
| 出站行 | **零**，客户这一轮收不到任何东西 |
| `policy_decisions` | 有一行 `post_plan` 的 `deny`，理由码 `HUMAN_CONTROL_ACTIVE` |
| `policy_decisions` 的 `pre_generation` | **零行** ← **本文件的题目** |

**最后两行合起来才是 B2-4**：RFC §8 把 `human control` 放在 `pre_generation` 那一格，
而实现在 `post_plan` 上留了一行带理由码的证据。
**补一行 hook 记录不等于补一个 hook 契约**（没有 `policy_id` / `policy_version` /
`obligations` 那套针对这一格的输出）—— 所以这一份**不要求**它变成 `pre_generation`，
它只把「今天是这个样子」钉住，好让哪天有人改了这件事时有人到场。

## 为什么要连库

「理解跑了、槽位写回了、而什么都没发出去」这三件事**只有跑完一轮才看得见**。
静态扫描能证明「没有人写 `pre_generation`」，**证明不了「该写的那一刻到了」**——
而这一份的价值全在后半句：**零行是有意义的，前提是那一刻真的发生过。**
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor

from tests.conftest import TENANT_A
from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.integration]

#: 接管那一轮客户说的话要落进这个槽位 —— 它是「理解步真的跑了」的证据。
#: **挑一个帧真的声明了的槽位**，否则 `apply_slot_updates` 会拒掉它，
#: 而那时这份用例会红在一个与题目无关的地方。
TAKEOVER_SLOT = "request_summary"

#: 接管那一轮 `_persist_no_send` 写下的理由码（`process_prepared` 的第一支）。
HUMAN_CONTROL_REASON = "HUMAN_CONTROL_ACTIVE"

#: **两轮说的话必须不同** —— 见 `_UnderstandsOneSlot` 的 docstring：
#: 相同的话会让「接管轮理解了」那条断言在第一轮就已经成立。
SAID_BEFORE_TAKEOVER = "接管之前客户说的那句话"
SAID_DURING_TAKEOVER = "接管期间客户补的那句话"


class _UnderstandsOneSlot(DeterministicMockInference):
    """理解步把客户这一轮的话写进一个槽位，**每一轮写的话不一样**。

    **只改理解那一半**：计划与生成在接管期间根本不会被调用，
    替身在那两处做什么都观察不到 —— 而一个把三步都改了的替身，
    会让「plan 没跑」这件事变得无法区分「跑了但没产出」。

    **`said` 必须逐轮不同，而这是改过一版的。** 初版两轮写同一句话，
    于是「接管轮理解步跑了」那条断言**在第一轮就已经成立** ——
    第一轮（AI 那一支）本来就会把它填上，接管轮什么都不做也照样绿。
    **那是一条断言了自己刚提供的值的用例**（`AGENTS.md`「断言自己把被测的那个值
    重新提供了一遍」，本仓第四个变种）。逐轮不同之后，
    断言量的是**最后写进去的那句是不是接管轮那句**。
    """

    def __init__(self, said: str) -> None:
        super().__init__()
        self._said = said

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        frame = kwargs["aggregate"]["target_stack"][-1]
        document["slot_updates"] = [
            {
                "frame_id": str(frame["id"]),
                "slot": TAKEOVER_SLOT,
                "value": self._said,
                "confidence": 0.95,
                "evidence_event_id": str(kwargs["envelope"]["event_id"]),
            }
        ]
        return document


def _accept(settings: Settings, seeded: dict[str, object], conversation_id: UUID) -> None:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=callback(external_event_id=f"hc-{uuid4()}", sender_id=sender_of(conversation_id)),
    )
    assert accepted.accepted is True


def _turn(settings: Settings, seeded: dict[str, object], conversation_id: UUID, said: str) -> Any:
    _accept(settings, seeded, conversation_id)
    result = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=_UnderstandsOneSlot(said),
        worker_id="human-control-hook-worker",
    ).process_one(block_ms=500)
    assert result is not None, "这一轮没被处理 —— 后面每一条断言都会量错东西"
    return result


def _hand_it_to_a_person(settings: Settings, conversation_id: UUID) -> None:
    """把这通对话交给人。

    **列与 `state` 两处都要改，而这不是重复**：`control_mode` 在
    `conversation_aggregates` 上有一列（给 SQL 查询用），同一个事实也在 `state`
    这份聚合文档里（`process_prepared` 读的是后者）。
    只改列的话这一轮照旧走 AI 那一支，**而用例会绿** ——
    它会断言「没有 `pre_generation` 行」，而那在 AI 那一支上本来就成立。
    **那正是一条什么都没证明的用例。**
    """

    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        connection.execute(
            "UPDATE conversation_aggregates"
            "   SET control_mode = 'human',"
            "       state = jsonb_set(state, '{control_mode}', '\"human\"')"
            " WHERE tenant_id = %s AND id = %s",
            (TENANT_A, conversation_id),
        )


def _aggregate(settings: Settings, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_A, conversation_id),
        ).fetchone()
    return row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])


def _policy_rows(settings: Settings, conversation_id: UUID) -> list[dict[str, Any]]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        return list(
            connection.execute(
                "SELECT hook, effect, reason_code FROM policy_decisions"
                " WHERE tenant_id = %s AND conversation_id = %s ORDER BY created_at",
                (TENANT_A, conversation_id),
            ).fetchall()
        )


def _shape_of(stack: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """目标栈的**形状**：每一帧的 id / 定义 / 状态，按栈序。

    **「栈冻结」说的是形状，不是「帧里什么都没变」，而这个区分是承重的。**
    本用例第一版拿整个 `target_stack` 做相等比较，于是它**红在槽位写回上** ——
    而槽位写回正是 ADR-0054 §三.1 那张表里写着「**跑**」的那一行，
    也正是本用例上面几条断言刚刚证明的事。
    **一条与自己 docstring 打架的断言**：同一份文件里，
    上半说「槽位要写回」，下半说「什么都不许变」。

    冻结要挡的是**栈操作**（push / repair / complete / abandon）—— 那是 plan
    才做得出来的事，而 plan 在接管期间根本不跑。所以判据取这三样：
    帧数、每帧的身份、每帧的状态。
    """

    return [
        (str(frame["id"]), str(frame["definition_id"]), str(frame["status"])) for frame in stack
    ]


def _outbox_count(settings: Settings, conversation_id: UUID) -> int:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        row = connection.execute(
            "SELECT count(*) AS total FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (TENANT_A, conversation_id),
        ).fetchone()
    return int(row["total"])


def test_the_takeover_turn_understands_and_records_but_writes_no_pre_generation_row(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**行为在、hook 契约不在 —— 两半在同一轮里一起断言。**

    先跑一轮正常的 AI 回合把会话建出来，再把它交给人，再跑一轮。
    **两轮是必需的**：接管标记要落在一个已经存在的聚合上。
    """

    conversation_id = new_conversation(TENANT_A)

    # ① 一轮正常回合 —— 把会话建出来，同时给下面那个「零」一个对照：
    #    这一轮是 AI 在跑，它会留下自己的那几行判定。
    _turn(settings, seeded_database, conversation_id, SAID_BEFORE_TAKEOVER)
    before = _policy_rows(settings, conversation_id)
    stack_before = _shape_of(_aggregate(settings, conversation_id)["target_stack"])
    # **出站行要按「这一轮新增了几条」量，不是按「这通对话一共几条」。**
    # 第一轮是 AI 在跑，它**本来就会发一条** —— 第一版按总数断言 0，
    # 于是红在「1 == 0」上，而红的理由与接管毫无关系。
    # 这一条同时是下面那个「零」的**非零对照**：AI 轮发得出来，接管轮发不出来。
    outbox_before = _outbox_count(settings, conversation_id)
    assert outbox_before > 0, (
        "第一轮（AI 那一支）一条出站行都没有 —— 那么下面「接管轮没新增出站行」"
        "就不是接管拦住的，而是这通对话本来就发不出东西"
    )

    assert before, (
        "第一轮一行判定都没有 —— 那说明这一轮根本没走到 policy 那一步，"
        "后面「接管轮没有 pre_generation 行」就成了一句什么都没证明的话"
    )

    # ② 交给人
    _hand_it_to_a_person(settings, conversation_id)

    assert (
        _aggregate(settings, conversation_id)["target_stack"][-1]["slots"][TAKEOVER_SLOT]
        == SAID_BEFORE_TAKEOVER
    ), (
        "第一轮就该把这个槽位填上（AI 那一支照常理解）—— 它是下面那条"
        "「接管轮也理解了」的**前值**：两轮的值必须不同，断言才量得出是哪一轮写的"
    )

    # ③ 接管期间客户又说了一句
    _turn(settings, seeded_database, conversation_id, SAID_DURING_TAKEOVER)

    state = _aggregate(settings, conversation_id)
    rows = _policy_rows(settings, conversation_id)
    fresh = rows[len(before) :]

    assert state["control_mode"] == "human", (
        "这一轮跑完之后控制权不在人手上了 —— 那么上面那一轮根本不是接管轮，本用例量的是别的东西"
    )

    # —— 行为在（ADR-0054 §三.1 那张表）——
    frame = state["target_stack"][-1]
    # **`frame["slots"][name]` 装的是值本身（一个标量），不是 `{"value": ...}`** ——
    # 证据（`confidence` / `evidence_event_id` / `source`）在兄弟字典
    # `frame["slot_evidence"][name]` 里（`target_stack.apply_slot_updates`）。
    # 本用例第一版按 dict 读它，连库那一跑当场 `TypeError` ——
    # 而 `pipeline.py` 里 `_settle_pending_question` 上方那段注释**记着同一个坑**
    # （「第一版我把值当成 dict 去读它的证据」）。**同一个形状，第三次。**
    recorded = frame["slots"].get(TAKEOVER_SLOT)
    assert recorded == SAID_DURING_TAKEOVER, (
        "接管期间**理解步要跑、槽位要写回**（ADR-0054 §三.1）。"
        f"这一格现在是 {recorded!r}，而接管轮说的是 {SAID_DURING_TAKEOVER!r}。"
        "\n**断言的是「最后写进去的是接管轮那句」，不是「这一格非空」** ——"
        "非空在第一轮就成立了，那样的断言什么都证明不了。"
        "\n停在前一轮那句，说明接管轮没有理解 —— 而那正是 ADR-0054 之前的老行为："
        "「客户对人说的话，机器事后一概不知道」。"
    )
    assert frame.get("slot_evidence", {}).get(TAKEOVER_SLOT, {}).get("source") == "model", (
        "槽位写回时要同时记下它的来源（`apply_slot_updates` 写 `slot_evidence`）。"
        "\n**这一条是上一条的另一半**：值对了而证据没落，说明写回只走了一半 ——"
        "而接管期间「这句话是客户对**人**说的」这件事，只有证据那一侧记得住"
    )
    assert _outbox_count(settings, conversation_id) == outbox_before, (
        "接管期间**不该新增任何出站行**：plan 与 generate 都不跑，客户这一轮收不到东西。"
        f"\n接管前 {outbox_before} 条，接管轮之后 "
        f"{_outbox_count(settings, conversation_id)} 条"
    )
    assert _shape_of(state["target_stack"]) == stack_before, (
        "接管期间**栈冻结**：这一轮不做任何栈操作（push / repair / complete / abandon 都没有）。"
        f"\n接管前：{stack_before}"
        f"\n接管后：{_shape_of(state['target_stack'])}"
        "\n形状变了说明 plan 跑了，而 ADR-0054 §三.1 明确拒绝「顺手维护一下栈」"
    )

    # —— 而 hook 契约那一行不在（B2-4）——
    assert any(
        row["hook"] == "post_plan"
        and row["effect"] == "deny"
        and row["reason_code"] == HUMAN_CONTROL_REASON
        for row in fresh
    ), (
        f"接管轮没留下 `post_plan` / `deny` / `{HUMAN_CONTROL_REASON}` 那一行。"
        f"\n这一行是「这件事确实发生过」的证据 —— 没有它，下面那个「零」就没有意义"
        f"（什么都没发生时 `pre_generation` 当然也是零）。这一轮实际写下的：{fresh}"
    )
    assert not [row for row in rows if row["hook"] == "pre_generation"], (
        # 1) 看到了什么
        f"这通对话现在有 `pre_generation` 的判定行了："
        f"{[row for row in rows if row['hook'] == 'pre_generation']}。"
        # 2) 为什么这条要红
        "\nRFC §8 把 `human control` 放在 `pre_generation` 那一格，而实现**有意**"
        "不写这一行：ADR-0137 §二 第 1 条明写「不新增发出点」，理由是"
        "「补它们要先答『那个时点各自要检什么』」——"
        "**补一行 hook 记录不等于补一个 hook 契约**（那一格没有 `policy_id` / "
        "`policy_version` / `obligations` 这套输出）。"
        # 3) 到期了该做什么
        "\n**如果这一行是有意加的，那么 B2-4 到期了**：去做那一次裁定，"
        "把结论写进 ADR 并点名修订 RFC §3.4 / §8 那两条标注，"
        "然后连同 `tests/contracts/test_policy_hook_shape.py` 的 `UNWIRED_HOOKS` 一起改。"
    )
