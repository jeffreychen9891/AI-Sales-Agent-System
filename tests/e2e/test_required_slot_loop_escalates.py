"""连着被同一条闸拒的会话不许无声地转下去 —— 死循环升级（ADR-0137 裁定一）。

## 它守的不是「拒得对」，是「拒完之后有没有人知道」

`STRATEGY_SKIPS_REQUIRED_SLOT` 的拒绝路径**不产出出站行**（`safe_fallback_plan`
的 `candidate_intents` 与 `required_slot_requests` 都是空），而实测这个理由码
**既不在 `HANDOFF_REASON_CODES` 也不在 `TASK_WITHOUT_HANDOFF_REASON_CODES`** ——
于是 `manual_task_id` 是 `None`：**不建人工任务、不通知经理、不发消息。**

连拒 N 轮的全部产出是 N 行 `policy_decisions` deny。**客户每轮收不到东西，
而没有任何人被叫醒** —— 这比单次沉默严重得多，也是这份用例存在的全部理由。

## 期望行为（用户 2026-09-13 裁定，N=2）

| 轮 | 期望 |
|---|---|
| 1 | deny 行，**不建任务** —— 一次失误不该惊动人 |
| 2 | deny 行 **+ 一条人工任务** —— 连续两次同码说明它不会自己走出来 |
| 3 | deny 行，**不新建任务** —— 那一条仍开着 |

**「连续」的语义**：查该会话**最近一行** `policy_decisions`，同码 = 连续第二次。
**中间隔了一轮别的理由码的拒绝算打断**（那说明模型在尝试不同的东西，不是卡死）。
这个语义**不在去重键里**，所以它写在查询逻辑与这段说明里。

**去重键是「会话 + 理由码 + 未关闭」** —— **一个循环一条任务，不是一轮一条**。
后者是仓里已经点名过的坏形状：成本上限那条注释写着「客户不停打字就一条任务
一条消息」，而分类是 `NO_PUSH`，没人被叫醒。

## 为什么用**基础**替身而不造一个专用的

**不造替身，而是发布一版定制 release**，让基础替身本来就会选中的那条策略
（`DeterministicMockInference` 选 `"acknowledge" if "acknowledge" in allowed else allowed[0]`）
声明 `required_slots`，而那个槽位第一轮是空的。基础替身的
`required_slot_requests` 本来就是 `[]`（`inference.py`），所以它**天然不问**。

**这不是图省事，它改变了断言的含义**：

* 专用替身证明的是「**一个特意构造的计划**会被拒」；
* 基础替身证明的是「**一份普通的确认型计划**在那种配包下**每轮都会被拒**」。

前者只说明闸在工作，**后者说明这个循环在正常运转下会发生** ——
而死循环之所以值得建一道守卫，正是因为后者。
（下一个人看到这个绕法，第一反应会是「为什么不直接造个替身，那样简单」。）

## 为什么必须定制一版 release

e2e 夹具发布的是 `synthetic-service-desk`，而实测**它没有任何策略声明
`required_slots`**（三份在架配包里只有 `synthetic-two-axis-desk` 有）——
**闸二在夹具那份配包上永远不触发**，照原样三轮驱动会三轮全绿。

发布第二版而不是改第一版，是生产的做法：`profile_releases` 是 append-only
（迁移 `006` 上了触发器、运行期角色没有 UPDATE），「这一版不对」的答案是
**再发一版并重绑**（先例 `test_reengagement_quota._publish_a_re_engaging_release`）。

⚠️ **这条路径直接 `INSERT` 进 `profile_releases`，绕过 `load_package`**，
所以 §4.1 那道封版闸（`required_slots` 的槽位必须在允许该策略的帧里存在）
**不会在这条路上跑**。本夹具仍然让那份 profile 满足它（`welcome` 帧有
`request_summary` 且允许 `acknowledge`）——**不是因为闸会拦，而是因为
一份连封版闸都过不了的夹具，证明不了生产里会发生的事**：
生产里那份配包根本发不出来。

**夹具可以简化，但不能简化成一个生产里不可能存在的形状** ——
与「跨边界的数据结构必须有真实样本驱动」同一个精神。

## 反向探针（2026-09-13 实测）

命令：`scripts/probe_required_slot_gate.py 1556`，在一个从 `ci.sh` 前 175 行派生
出来的壳里跑（那三条要真库真队列）。
**范围**：`pytest tests/contracts/ tests/e2e/test_required_slot_loop_escalates.py`
—— 基线 1556 passed。

| 支 | 变异 | 红 | 本文件里红的 |
|---|---|---|---|
| G1 | 闸二整条关掉 | 7 | **3**（没有 deny 行就没有升级可谈；这一支不算升级的证据） |
| G2 | 判据改成「一律拒」 | 2 | 0 |
| G3 | 判据改成「点名任何东西就放行」 | 1 | 0 |
| **E1** | **升级整条关掉（闸二留着）** | **2** | **2** —— 闸二八条纹丝不动 |
| **E2** | **「连续」改成「有没有同码的行」** | **1** | **1** —— 下面第三条 |

**E1 的 2 而不是 3**：第三条（隔一行别的码就不升级）断言的是**不升级**，
升级整条关掉之后它照绿 —— 它守的是升级的**边界**，不是升级本身。
**E2 的那 1 就是它**，而且是**独守**：「连续」这个语义不在去重键里
（键是「会话 + 理由码 + 未关闭」），除了那一条没有第二处会发现这个退化。

**G1 那一栏我先写成了「闸二八条全红」，实测是 4** —— 另外四条断言的是**放行**，
闸关掉之后它们照样放行。一道闸的守卫里，只有断言「拒」的那些会在闸消失时红。
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import inference_for_provider
from fastagent_v2.worker.pipeline import InboxProcessor
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e]

REASON = "STRATEGY_SKIPS_REQUIRED_SLOT"

#: 基础替身会选中的那条策略，以及本帧真有的那个槽位 —— 两者都来自夹具那份配包，
#: 不是发明的：`welcome` 帧允许 `acknowledge`，它的槽位是 `request_summary`。
STRATEGY = "acknowledge"
SLOT = "request_summary"
FRAME = "welcome"


class AcceptingAdapter:
    """Test double. 它从不开 socket。"""

    def send(self, *, command: SendCommand, idempotency_key: str, deadline_at: Any) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: Any) -> SendReceipt:
        raise AssertionError("本用例的每一轮都不该产出出站行")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: Any) -> SendReceipt:
        raise AssertionError("本用例的每一轮都不该产出出站行")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _publish_a_release_whose_strategy_requires_a_slot(
    tenant_id: UUID, seeded: dict[str, Any]
) -> None:
    """再发一版，让 `acknowledge` 声明一个本帧有、而第一轮还空着的必填槽位。

    **只动 `strategies[STRATEGY].required_slots` 一处** —— 夹具越窄，
    结论越归因得清楚（先例那份也只动 `emit_kind` 一处，并把这一点写在注释里）。
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, UUID(str(seeded["profile_release"]))),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row["profile"])

        frame = next(item for item in profile["target_frames"] if item["id"] == FRAME)
        assert STRATEGY in frame["allowed_strategy_set"], (
            f"夹具那份配包的 {FRAME!r} 帧不再允许 {STRATEGY!r} —— "
            "基础替身会选别的策略，本用例要跟着换"
        )
        assert SLOT in {slot["name"] for slot in frame["information_slots"]}, (
            f"{FRAME!r} 帧没有 {SLOT!r} 槽位了 —— 换一个本帧真有的，"
            "别声明一个帧里不存在的（那会是 §4.1 封版闸拦的形状，而这条路绕过它）"
        )
        profile["strategies"][STRATEGY]["required_slots"] = [SLOT]
        _assert_the_admission_gate_would_have_passed_this(profile)

        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        # `^\d+\.\d+\.\d+$` —— schema 钉着形状，而 `prepare` 会校验它刚加载的 profile，
        # 所以一个描述性后缀会让**管线**红，不是让 INSERT 红。
        # ⚠️ **别写在架包已占的号**：third 自缺陷 19 批起就是 1.0.1，
        # 夹具再发 1.0.1 会撞 `(tenant_id, profile_id, version)` 唯一约束。
        profile["identity"]["version"] = "1.0.5"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                release_id,
                row["profile_id"],
                "1.0.5",
                # 重算而不是照抄：`UNIQUE (tenant_id, content_hash)` 会拒掉在架那个哈希，
                # 而一个声明的哈希不等于自身内容的 release，registry 出口处会拒。
                canonical_hash(profile),
                __import__("psycopg").types.json.Jsonb(profile),
            ),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, tenant_id),
        )
        connection.commit()


def _assert_the_admission_gate_would_have_passed_this(profile: dict[str, Any]) -> None:
    """把 §4.1 那道封版闸**在这里跑一遍**，断言这份夹具过得了它。

    上面那段 docstring 声称「本夹具仍然让那份 profile 满足它」。
    **一句声称不是一次运行** —— 而这条路径直接 `INSERT`，闸永远不会自己跑，
    所以那句话可以一直是错的而没有任何东西发现（这正是本仓五条判据里
    「约束声明了无人执行」的形状，只不过这次发生在夹具里）。

    **两个方向都跑**，因为只跑「过」的那一边证明不了这道闸在工作 ——
    一个改名成空函数的闸也会让这份夹具「通过」：

    * 这份夹具 → 不抛；
    * 同一份夹具把槽位名改成帧里不存在的一个 → **必须**抛。

    后者是「守卫的守卫」：它证明上面那一行真的调用了那道闸，
    而不是调用了一个什么都不判的东西。
    """

    from fastagent_v2.profiles.registry import (  # noqa: PLC0415 - test-only
        ProfilePackageError,
        _check_strategy_required_slots,
    )

    here = Path("tests/e2e/test_required_slot_loop_escalates.py")
    _check_strategy_required_slots(profile, here)

    broken = copy.deepcopy(profile)
    broken["strategies"][STRATEGY]["required_slots"] = ["slot_that_no_frame_declares"]
    with pytest.raises(ProfilePackageError, match="slot_that_no_frame_declares"):
        _check_strategy_required_slots(broken, here)


def _turn(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    """一条真实客户消息，经入口进箱，再由 worker 处理一轮。

    照 `test_reply_before_due_invalidates.py` 的形状 —— **夹具造不出「持续」**：
    三个手写状态证明不了「它会一轮接一轮地发生」，而那正是死循环的定义。
    """

    from test_retry_budget import _accept  # noqa: PLC0415 - suite-local helper

    _accept(settings=settings, seeded_database=seeded, conversation_id=conversation_id)

    class _NeverNames(type(inference_for_provider(settings.model_provider))):  # type: ignore[misc]
        """计划**不点名**任何槽位 —— 本文件要的正是被闸拒的那个形状。

        运维缺陷 19 之后，基础替身会把「声明的且还空着的」槽点进
        `required_slot_requests`（跟真实模型守同一条约束），于是闸放行、
        本套用例测的那条拒绝路径就再也走不到了。**想验某一支就自己给一个
        替身**（本仓记法）——这里覆盖回「模型忘了点名」的样子。
        """

        def plan(self, **kwargs):  # noqa: ANN003, ANN202 - 替身覆盖
            document = super().plan(**kwargs)
            document["required_slot_requests"] = []
            return document

        def generate(self, **kwargs):  # noqa: ANN003, ANN202 - 替身覆盖
            # **回退问句要真的是一个问句**（运维缺陷 19）：记账判据改成
            # 「发出去的正文里有问号」，基础替身的 "Message received."
            # 不算问了 —— 而本文件第 1 轮的断言正是「问了、记下来了」。
            document = super().generate(**kwargs)
            # 每轮正文要**真的**不同 —— 复读闸判的是**最长公共连续片段
            # >= 20 字符**（`REPEATED_OUTBOUND_CHARS`），不是逐字相等：
            # 「固定句 + 序号」的公共前缀就超阈值，序号救不了它
            # （第一、二版分别栽在实例计数器活不过轮次、和这一条上）。
            # 把本轮 event_id **拆散着织进句子**，任何连续片段都到不了 20。
            marker = str(kwargs["understanding"]["event_id"]).replace("-", "")[:12]
            a, b, c = marker[:4], marker[4:8], marker[8:12]
            # 槽名不进正文（「（request_summary）？」那段公共片段 19 字符，
            # 离阈值只差 1）—— 没有断言读正文，识别靠问号就够。
            asked = f"{a}想租{b}哪一类{c}？"
            document["customer_visible_segments"] = [
                {"type": "text", "content_or_ref": asked}
            ]
            for intent in document["proposed_intents"]:
                if intent.get("kind") == "send_message":
                    intent["typed_payload"]["content"] = {
                        "type": "text",
                        "text": asked,
                    }
            return document

    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=_NeverNames(),
        worker_id="slot-loop-worker",
    )
    assert processor.process_one(block_ms=500) is not None


def _denials(tenant_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT reason_code, hook, effect, created_at, context FROM policy_decisions"
            "  WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'"
            "  ORDER BY created_at",
            (tenant_id, conversation_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _tasks(tenant_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT id, reason_code, status, evidence FROM manual_tasks"
            "  WHERE tenant_id = %s AND conversation_id = %s ORDER BY created_at",
            (tenant_id, conversation_id),
        ).fetchall()
    return [dict(row) for row in rows]


#: 插在中间那一行用的理由码 —— **本帧真会出现的一个**（计划步的另一道闸），
#: 不是随手编的字符串：一个编出来的码会让这条用例看起来在测一个不存在的情形。
OTHER_REASON = "SLOT_NOT_IN_FRAME"


def _insert_a_denial_with_another_reason(tenant_id: UUID, conversation_id: UUID) -> None:
    """伪造一行「别的理由码」的 `post_plan` deny。

    **走的是生产那一个写入点的形状**（`policy_output.record_policy_decision` 的列），
    而不是自己拼一条 SQL —— 列错位在这张表上是静默的
    （`policy_id` 与 `policy_version` 同为 text），而本用例的断言只看 `reason_code`，
    看不出错位。照写入点的形状填，是让这行伪造数据与真实行同形的唯一办法。
    """

    from fastagent_v2.worker.policy_output import (  # noqa: PLC0415 - test-only
        PolicyOutput,
        audit_only,
        record_policy_decision,
    )

    with Database(os.environ["FASTAGENT_WORKER_DATABASE_URL"]).transaction(
        tenant_id=tenant_id
    ) as connection:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_plan",
                effect="deny",
                reason_code=OTHER_REASON,
                policy_id="plan-validator",
                policy_version="1.0.0",
                obligations=audit_only(),
                context={"fabricated_by": "test_required_slot_loop_escalates"},
            ),
        )


def _outbox_count(tenant_id: UUID, conversation_id: UUID) -> int:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            "  WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def _pending_question(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any] | None:
    """聚合上那一格 `pending_question` —— 消费方③ 的出口。"""

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates"
            "  WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "会话聚合该在"
    pending = row["state"].get("pending_question")
    return dict(pending) if pending else None


def test_a_third_turn_does_not_repeat_the_silent_refusal(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """三轮连驱：第二轮升级出一条人工任务，第三轮不再重复建。

    **断言的是「第三轮不是同一条闸的第三次无声拒绝」** —— 而不是「第三轮放行」：
    放行会把一个跳过了必填槽位的回合送到客户面前，那是这道闸存在的反面。
    出路是**让人知道**，不是让它过去。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_release_whose_strategy_requires_a_slot(tenant_id, seeded_database)

    # ---- 第 1 轮：拒，**而这一轮把问题问出去**（T339 第二批，用户裁 (c)）----
    _turn(settings, seeded_database, conversation_id)
    denials = _denials(tenant_id, conversation_id)
    assert [row["reason_code"] for row in denials] == [REASON], (
        f"第一轮该只有一行本闸的 deny，实测 {[row['reason_code'] for row in denials]}"
    )
    assert _tasks(tenant_id, conversation_id) == [], (
        "第一次同码拒绝不该惊动人 —— 这一轮的出路是**问**，不是叫人"
    )
    assert _outbox_count(tenant_id, conversation_id) == 1, (
        "**第一轮该发出一条消息**：回退点名了缺的槽位，所以它说得出内容。"
        "这一条替换了「拒绝路径不产出出站行」那句断言 —— "
        "那曾经是写在管线控制流里的不变式（`if rejection is not None: return _persist_no_send`），"
        "而 (c) 之后它只对「点不出槽位名」的那些拒绝成立"
    )

    # ---- 消费方③：问了什么，被记下来了 ----
    asked = _pending_question(tenant_id, conversation_id)
    assert asked is not None, (
        "`_record_pending_question` 该记上这一问 —— 它的第一个前提是 "
        "`required_slot_requests` 非空，而第一批接线之后它非空了"
    )
    assert asked["slot"] == SLOT, f"记下的该是那个空着的槽位，实测 {asked['slot']!r}"
    assert asked["answer_evidence"] is None, "「问了、还没答」"

    # ---- 第 2 轮：客户没答，**仍是同码** → 升级 ----
    _turn(settings, seeded_database, conversation_id)
    assert [row["reason_code"] for row in _denials(tenant_id, conversation_id)] == [REASON] * 2
    tasks = _tasks(tenant_id, conversation_id)
    assert len(tasks) == 1, (
        f"**问过之后仍然同码**才升级，实测 {len(tasks)} 条 —— "
        "这一条是本用例的主断言。它比「连着被拒两次」是更强的证据："
        "**排除了「模型只是没想到要问」**（第一轮已经问了）"
    )

    # ---- 第 3 轮：拒，且不新建任务 ----
    _turn(settings, seeded_database, conversation_id)
    assert [row["reason_code"] for row in _denials(tenant_id, conversation_id)] == [REASON] * 3
    assert [row["id"] for row in _tasks(tenant_id, conversation_id)] == [tasks[0]["id"]], (
        "第三轮不该新建任务 —— 去重键是「会话 + 理由码 + 未关闭」，"
        "一个循环一条任务，不是一轮一条（一轮一条是仓里已经点名过的坏形状）"
    )
    assert _outbox_count(tenant_id, conversation_id) == 1, (
        "**第二、三轮不再发** —— 升级之后出路是人，不是再问一遍；"
        "所以出站行仍然只有第一轮那一条"
    )


def test_the_task_says_which_slot_and_which_strategy(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """那条人工任务要能自解释 —— 接手的人不查库就知道发生了什么。

    只写理由码是不够的：`STRATEGY_SKIPS_REQUIRED_SLOT` 说的是「跳过了必填槽位」，
    而接手的人要知道**哪个会话、卡在哪个槽位、模型连着选了什么策略**。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_release_whose_strategy_requires_a_slot(tenant_id, seeded_database)

    _turn(settings, seeded_database, conversation_id)
    _turn(settings, seeded_database, conversation_id)

    tasks = _tasks(tenant_id, conversation_id)
    assert len(tasks) == 1
    evidence = tasks[0]["evidence"]
    assert evidence["slot"] == SLOT, "任务要点名卡住的那个槽位"
    assert evidence["strategy_id"] == STRATEGY, "任务要点名模型连着选的那条策略"
    assert len(evidence["policy_decision_ids"]) == 2, (
        "任务要带上那两轮 deny 行的 id —— 顺着它们能看到「连续」是怎么判出来的"
    )


def test_a_different_reason_code_in_between_breaks_the_streak(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**中间隔了一轮别的理由码，不算连续。**

    那说明模型在尝试不同的东西，不是卡死 —— 而「卡死」才是升级要抓的形状。
    这一条守的是「连续」这个语义本身：一个只数「同码总次数」的实现会让它红，
    而那种实现会把「三轮里各错一次」误判成死循环。

    **它不在去重键里**（键是「会话 + 理由码 + 未关闭」），所以只能由这一条守。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_release_whose_strategy_requires_a_slot(tenant_id, seeded_database)

    # 第 1 轮真驱动 —— 一行本闸的 deny。
    _turn(settings, seeded_database, conversation_id)
    assert [row["reason_code"] for row in _denials(tenant_id, conversation_id)] == [REASON]

    # **中间插一行别的理由码的 deny。** 这一行是**伪造**的，而伪造在这里是对的：
    # 本条要测的是**那条查询读不读「最近一行」**，不是「另一道闸会不会触发」——
    # 让第二道真闸在同一个会话上触发要再发一份 release，
    # 而那会把这条用例变成在测那道闸。**被测对象决定夹具能简化到哪一步。**
    _insert_a_denial_with_another_reason(tenant_id, conversation_id)

    # 第 2 轮真驱动 —— 它前面最近的一行是**别的**理由码，所以不算连续第二次。
    _turn(settings, seeded_database, conversation_id)

    codes = [row["reason_code"] for row in _denials(tenant_id, conversation_id)]
    assert codes == [REASON, OTHER_REASON, REASON], f"三行的顺序不对：{codes}"
    assert _tasks(tenant_id, conversation_id) == [], (
        "中间隔了一行别的理由码，就不该升级 —— 一个只数「同码总次数」的实现"
        "会在这里建出一条任务，而它会把「三轮里各错一次」误判成死循环"
    )


def test_the_four_keys_reach_the_route_that_shows_the_task(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**读侧那一个消费方**（回查 B-1，2026-09-14）。

    上面那两条断言的是「升级把那四个键**写进了库**」（直接 `SELECT … evidence`）。
    **而「自解释的人工任务」这句话的价值在人**看到它 —— 所以读侧是第二个消费方。

    > **复核窗口那个区分要留着**：「route 会返回 `evidence`」**已经有人守**
    > （别的任务类的用例断言过那一列会出现在响应里）；
    > **没人守的是「升级写的那四个键会到达那里」** ——
    > 而那四个键就是「自解释」的全部内容。
    > 所以本条断言的不是那条 route 的形状，**是那份 payload 的内容**。

    夹具复用 `tests/integration/test_manual_task_routes.py` 的 `_client_for`
    与那份权限清单 —— **不自造一个登录流程**：
    自造的那一份会与真的漂开，而本条要看的是**真 route 返回了什么**。
    """

    # **那份 helper 在 `tests/integration/` 里，而只跑 `tests/e2e/` 时那个目录不在
    # `sys.path` 上**（pytest 只把被收集文件所在的目录加进去）——
    # 实测 `ModuleNotFoundError`。显式加一次，**而不是把 `_client_for` 抄一份过来**：
    # 抄过来的那一份会与真的登录流程漂开，而本条要看的正是真 route 返回了什么。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
    from test_manual_task_routes import WORKING, _client_for  # noqa: PLC0415 - suite-local

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_release_whose_strategy_requires_a_slot(tenant_id, seeded_database)

    # 第一轮问出去，第二轮仍同码 → 升级建任务（与上面那条同一条路）
    _turn(settings, seeded_database, conversation_id)
    _turn(settings, seeded_database, conversation_id)
    tasks = _tasks(tenant_id, conversation_id)
    assert len(tasks) == 1, "前提：第二轮该升级出一条任务"

    client, headers = _client_for(settings, seeded_database, WORKING)
    response = client.get(f"/conversations/{conversation_id}/manual-tasks", headers=headers)
    assert response.status_code == 200, response.text

    items = response.json()["items"]
    assert len(items) == 1, f"该会话只有那一条升级任务，实测 {len(items)} 条"
    evidence = items[0]["evidence"]

    assert evidence["slot"] == SLOT, (
        f"接手的人要看到卡住的那个槽位，而响应里是 {evidence.get('slot')!r}"
    )
    assert evidence["strategy_id"] == STRATEGY, "以及模型连着选的那条策略"
    assert len(evidence["policy_decision_ids"]) == 2, (
        "以及那两行 deny 的 id —— 少一个就跟不回去「哪两次拒绝」"
    )
    assert evidence["escalated_after_consecutive_denials"] == 2, (
        "N 也要到达读侧：接手的人要知道这是第二次才升级的，不是第一次就叫人"
    )
