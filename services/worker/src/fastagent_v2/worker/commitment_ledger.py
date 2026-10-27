"""承诺台账的**写入方** —— `party=customer` 的候选由这里确认入账。

## 它执行的是哪一句图纸

RFC §5：「`party=customer`：理解步骤只提取候选，**planner/policy 确认后入账**；
有明确时间则创建 `customer_promise_due`，无明确时间按 profile 允许的澄清/默认规则
处理，**不能私自猜到期日**。」

**ADR-0139 在那两个执行者里选定了 policy**，并写明 planner 那条路今天是堵的、
而且是有意堵的：plan 的 `scheduled_event_changes` 被验完就丢，生产者是
`time_rules.py`，理由是「模型挑的时点在虚拟时钟下不可重放」。
把「planner 确认」实现成「planner 的输出行变成台账行」等于推翻 ADR-0096 §四.2。

## 确认那一步做的是什么（而不是什么）

**它不是一个分类器，也不是一个置信度闸** —— 那两样今天都不存在
（`ConfidenceGatePolicy` 零实现，`policy_bindings.commitment_ledger` 只是一个字符串，
T335 明写 `policy:` 没有解析器且那是有意的）。**发明一个会是在编规则。**

它做的是一件确定性的、能指着图纸说清楚的事：

1. **只有客户回合能产生客户承诺。** 判据正向写 —— `event_type == "customer_message"`，
   不是列举要拒的那几种。**这条极性本仓付过两次账**（`_remember_turn_refusal`
   那道闸，以及 D4 的安全回退），第二次是 2026-09-16 才修的；
2. **到期日照抄，不推断。** 模型没说就是 `None`，落库就是 `NULL`。
   提示词那一侧已经这么要求了（`_UNDERSTAND_SYSTEM` 里
   `Never infer a due date that was not said`），这里是它的执行点；
3. **入账状态一律 `proposed`。** 不是 `active` —— 图纸九态里 `proposed` 的意思
   正是「登记了，还没被确认生效」，而让它直接 `active` 等于替一条还没有人
   看过的承诺宣布它成立。状态推进是后面批次的事。

**每一条候选都落一行 `policy_decisions`**（`post_understanding` 那个时点），
收下的是 `allow`、拒掉的是 `deny` 并带理由码。于是「谁在什么依据下把一条候选
记成了承诺」是可查的，而**拒掉的那些同样留痕** —— 一条没有被记下来的承诺，
在台账上与「客户没说过」长得一样，只有 `policy_decisions` 分得开。

## `post_understanding` 这个时点在此之前零发出

`tests/contracts/test_policy_hook_shape.py` 的 `UNWIRED_HOOKS` 把它与
`pre_generation` 一起钉成「有词表、无发出方」。**本批把它接上，那条用例会红** ——
而那是「好消息也要红」那一族：红的含义是「回来把登记改了」，不是「出错了」。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.contracts import load_schema, validate_contract
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .policy_output import PolicyOutput, record_policy_decision

#: 这道确认闸自己的名字与版本，写进 `policy_decisions.policy_id` / `policy_version`。
#: 改行为要同批改版本号 —— 读审计的人靠它分得出「当时那一版怎么判的」。
POLICY_ID = "commitment-admission"
POLICY_VERSION = "v1"

#: 收下一条候选。
ADMITTED_REASON_CODE = "COMMITMENT_ADMITTED"

#: 拒掉的两种，各有一个码。**两种分开**是因为它们要人做的事不同：
#: 前者说「这一轮本来就不该有客户承诺」，后者说「这条候选自己不合形状」。
NOT_A_CUSTOMER_TURN_REASON_CODE = "COMMITMENT_NOT_A_CUSTOMER_TURN"
MALFORMED_CANDIDATE_REASON_CODE = "COMMITMENT_CANDIDATE_MALFORMED"

#: 活着的承诺多过 `commitment_refs` 的 `maxItems` 时，投影被截。
#: **台账一行不丢，被截的只是聚合上那份投影** —— 而这一行就是它的出声点。
REFS_TRUNCATED_REASON_CODE = "COMMITMENT_REFS_TRUNCATED"

#: 入账时的状态。图纸九态里的第一个。
INITIAL_STATUS = "proposed"

#: `evidence_refs` 的上界，与契约（以及 `conversation-aggregate.evidence_event_ids`）
#: 同一个数、同一个单位。这里只是把它写成一个可被用例比对的名字。
EVIDENCE_REFS_MAX = 8


def _dedupe_key(source_event_id: str, ordinal: int) -> str:
    """`<事件 id>:<这条事件里第几条候选>`。

    回合是至少一次的：同一条入站事件可能被重放，而没有这个键时一次重放会把
    同一条承诺记两遍 —— **两遍在台账上读起来是「客户答应了两次」**。
    形状与理由都照 `scheduled_events.dedupe_key`。
    """

    return f"{source_event_id}:{ordinal}"


def admit_customer_commitments(
    *,
    understanding: Mapping[str, Any],
    event_type: str,
    source_event_id: str,
    conversation_id: UUID,
    generation: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """把理解步的候选分成「收下的」与「拒掉的」。

    **纯函数，不碰数据库** —— 这样它的判据可以被用例直接问，而不必起一个事务。
    返回 `(要写的行, [(理由码, 说明), ...])`。

    每一行都当场过一遍 `commitment-record` 契约：**写进库之前先证明它合形状**，
    而不是等 `INSERT` 的 CHECK 去拦。两者都拦得住，而契约的报错说得出是哪个字段。
    """

    candidates = list(understanding.get("customer_commitments") or [])
    if not candidates:
        return [], []

    if event_type != "customer_message":
        # **正向判，不是反向列举。** `event_type` 有六个值，而只有一个是客户在说话。
        return [], [
            (
                NOT_A_CUSTOMER_TURN_REASON_CODE,
                f"a {event_type!r} turn carries no customer words, "
                f"so {len(candidates)} candidate(s) cannot be customer commitments",
            )
        ]

    rows: list[dict[str, Any]] = []
    refused: list[tuple[str, str]] = []
    for ordinal, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            refused.append(
                (MALFORMED_CANDIDATE_REASON_CODE, f"candidate {ordinal} is not an object")
            )
            continue
        evidence = str(candidate.get("evidence_event_id") or "")
        row = {
            "id": str(uuid4()),
            "tenant_id": "",  # 调用方填 —— 见 `write_admitted`
            "conversation_id": str(conversation_id),
            "party": "customer",
            "type": str(candidate.get("type") or ""),
            "summary": str(candidate.get("summary") or ""),
            "source_event_id": source_event_id,
            # **照抄，不推断。** 模型没说就是 None。
            "due_at": candidate.get("due_at"),
            "status": INITIAL_STATUS,
            "evidence_refs": [evidence] if evidence else [],
            "scheduled_event_id": None,
            "generation": int(generation),
            "version": 0,
        }
        rows.append({**row, "_dedupe_key": _dedupe_key(source_event_id, ordinal)})
    return rows, refused


#: 打法声明了要许一条承诺，而这一轮什么都没发出去。
#:
#: **一条没被人听见的承诺不是承诺**：记下来会让台账上多一条
#: 客户从未听过的义务，而台账正是用来答「我们答应过什么」的。
NOTHING_WAS_SAID_REASON_CODE = "COMMITMENT_NOTHING_WAS_SAID"

#: `fulfilment` 里**唯一一个有到期时刻**的值（T364，2026-09-19）。
#:
#: 另外四个都没有：`same_turn` 当轮就结清，`awaiting_customer` /
#: `awaiting_human` 在等别人（那个「多久」不是我们能定的），
#: `customer_promise_due` 是客户侧那一族、走的是模型照抄客户的话。
#: **所以只有这一个值会让下面那个偏移量生效** ——
#: 往一条 `same_turn` 的规则上写 `ai_commitment_due_within_hours`
#: 是无害的、也是没有效果的，而那正好是「多传一个不报错」，
#: 由 `test_only_the_due_fulfilment_gets_a_deadline` 钉住。
DUE_FULFILMENT = "ai_commitment_due"

#: 打法没声明偏移量时用这个（**小时**）。**用户 2026-09-19 当面裁的。**
#:
#: **为什么有默认而不是报错**：报错的那条路（配包不写就拒装）也被考虑过，
#: 而它会报废在架那份 `synthetic-carrier`（它声明了
#: `advance_goal -> ai_commitment_due` 而没有任何偏移量）。
#: **默认 1 小时是一个能交差的数，而不是一个正确的数** ——
#: 正确的数只有写打法的人知道（「我来安排」这一族该长、「这就发您」该短），
#: 所以它在 schema 里可声明、可逐条覆盖。
AI_COMMITMENT_DUE_DEFAULT_HOURS = 1

#: 打法能声明的上限（**小时**）。**用户 2026-09-19 当面裁的。**
#:
#: ⚠️ **这个数与 schema 里那个 `maximum` 必须相等**，
#: 由 `test_the_ceiling_is_one_number_in_two_places` 钉住 ——
#: 两处各写一个数，就是这道闸漂掉的方式，而漂的方向是 fail-open：
#: schema 放过一个 48，而这里以为不可能。
#:
#: **闸在 schema 上，不在这里**：`load_package` 那一步就拒，
#: 与 `ProactivityPolicyError` 那条「配包错误不是运行期条件」同源。
#: 下面那个函数仍然会再判一次 —— 因为它是纯函数，
#: 它的调用方不保证那份 profile 过过 schema。
AI_COMMITMENT_DUE_MAX_HOURS = 24


class CommitmentRuleError(ValueError):
    """商家声明的偏移量读不出来 —— **配包错误，不是运行期条件**。

    与 `ProactivityPolicyError` 逐字同一条论证：默认掉会**悄悄给商家一个
    它没有声明过的期限**，而那个期限会变成一条真的定时器、一条真的逾期告警。
    """


def due_within_hours(profile: Mapping[str, Any], *, strategy_id: str) -> int:
    """这个打法声明的兑现期限（小时）—— **唯一读取点**。

    没声明就是 `AI_COMMITMENT_DUE_DEFAULT_HOURS`。
    超上限或不是正整数就抛 —— 见 `AI_COMMITMENT_DUE_MAX_HOURS` 的注释。
    """

    declared = ((profile.get("strategies") or {}).get(strategy_id) or {}).get(
        "ai_commitment_due_within_hours"
    )
    if declared is None:
        return AI_COMMITMENT_DUE_DEFAULT_HOURS
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise CommitmentRuleError(
            f"strategy {strategy_id!r} declares a non-integer "
            f"ai_commitment_due_within_hours ({declared!r})"
        )
    if not 1 <= declared <= AI_COMMITMENT_DUE_MAX_HOURS:
        raise CommitmentRuleError(
            f"strategy {strategy_id!r} declares "
            f"ai_commitment_due_within_hours={declared}, "
            f"outside 1..{AI_COMMITMENT_DUE_MAX_HOURS}"
        )
    return declared

#: 客户侧那几行的判定落在哪个钩子上 —— 事实从客户的话里读出来。
CUSTOMER_ADMISSION_HOOK = "post_understanding"

#: AI 侧那几行的判定落在哪个钩子上。
#:
#: **不是 `post_understanding`**（客户侧那个）：AI 的义务由**打法**决定，
#: 而打法要到计划步才定下来。两侧时点不同，是因为事实来源不同。
AI_ADMISSION_HOOK = "post_plan"


def _ai_dedupe_key(source_event_id: str, ordinal: int) -> str:
    """AI 侧的去重键 —— **与客户侧分开命名空间**。

    两侧都按 `<事件 id>:<第几条>` 编号，而同一条入站事件**两侧都可能产出**：
    客户说「我明天给你发资料」（客户侧第 0 条），而我们这一轮打
    `answer_question`（AI 侧第 0 条）。**不分开就撞键，撞了就少记一条。**
    """

    return f"ai:{source_event_id}:{ordinal}"


def ai_rules_for(profile: Mapping[str, Any], *, strategy_id: str) -> list[dict[str, Any]]:
    """这个打法让 AI 欠下哪几条 —— **商家声明的那张表，唯一读取点**。

    `outcome_rules` 的形状由 schema 钉着（`strategy` / `party` /
    `creates_commitment` / `fulfilment` 四个必填）。这里只筛，不判断合理性：
    **平台不拥有商家的词表**，与话题那一条同源。
    """

    return [
        dict(rule)
        for rule in (profile.get("outcome_rules") or [])
        if isinstance(rule, Mapping)
        and str(rule.get("party")) == "ai"
        and str(rule.get("strategy")) == strategy_id
    ]


def admit_ai_commitments(
    *,
    profile: Mapping[str, Any],
    strategy_id: str,
    source_event_id: str,
    conversation_id: UUID,
    generation: int,
    said_something: bool,
    #: 这一轮的时刻。**传进来，不在这里读时钟**（T364，2026-09-19）——
    #: 这个函数是纯函数，而「到期时刻」要能被用例直接问；
    #: 在函数里 `datetime.now()` 会让那条判据只能靠冻结时钟去问。
    #: 与 `drafts_for_anchor` 把 `now` 当参数收是同一条理由。
    now: datetime,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """台账的 **`party=ai`** 那一半 —— 契约说「必须建账」的那一半。

    ## 在这个函数之前，它整个不存在

    `commitments.party` 的枚举含 `ai`，`commitment-record` 的描述逐字写着
    「**`ai` 的未来动作必须建账**」，而 `git grep` 写入方 **0 个** ——
    台账只写过 `party=customer`（批 11 交的那一半）。

    ## 事实来自商家的声明，不是模型的自述

    `outcome_rules` 说「AI 打哪个打法就欠一条什么承诺」。
    **不问模型自己许了什么**，理由与 `material_delivered` 那一条逐字同源：
    「Not the model's report… guess wrong… **nothing goes red**」——
    而一条没被记下来的承诺，在台账上与「我们没答应过」长得一样。

    ## 三条判据

    1. **只有真的说出去了才算。** 打法选了而这一轮什么都没发
       （被拒、被静音、谈不拢），那条义务**没有发生** ——
       落一行 `deny` 留痕，不是静默跳过；
    2. **`fulfilment: ai_commitment_due` 那几条有到期时刻，别的没有**
       （T364，2026-09-19 改）。期限由**打法**声明
       （`strategies.<名>.ai_commitment_due_within_hours`，用户当面裁的来源），
       不写用默认 1 小时。
       ⚠️ **上一版这里写的是「`due_at` 一律 `None`，因为没有任何地方声明
       多久之内兑现」** —— 那句话在本批之前为真，本批之后为假。
       它当时的理由（**照抄不推断**）仍然成立，而**现在有地方声明了**：
       照抄的对象从「模型抄客户的话」换成「商家写在打法里的那个数」。
       别的 `fulfilment` 仍然 `None`，理由见 `DUE_FULFILMENT` 的注释；
    3. **入账状态 `proposed`**，与客户侧逐字相同。状态推进是后面批次的事。

    ## 纯函数

    与 `admit_customer_commitments` 同一个形状：不碰数据库，
    所以它的判据可以被用例直接问，而不必起一个事务。
    """

    rules = ai_rules_for(profile, strategy_id=strategy_id)
    if not rules:
        return [], []
    if not said_something:
        return [], [
            (
                NOTHING_WAS_SAID_REASON_CODE,
                f"strategy {strategy_id!r} declares {len(rules)} ai commitment(s), "
                "but this turn put no message in front of the customer, "
                "so the obligation never reached anyone",
            )
        ]

    # **算一次，所有规则共用** —— 它是打法级的，不是规则级的。
    # 两处各算一遍就会在「一个打法两条 `ai_commitment_due` 规则」时给出两个期限。
    hours = due_within_hours(profile, strategy_id=strategy_id)
    rows: list[dict[str, Any]] = []
    for ordinal, rule in enumerate(rules):
        row = {
            "id": str(uuid4()),
            "tenant_id": "",  # 调用方填 —— 见 `write_admitted`
            "conversation_id": str(conversation_id),
            "party": "ai",
            "type": str(rule.get("creates_commitment") or ""),
            # **摘要由规则拼出来，不是一句人写的话**：读它的人要能从这一行
            # 回到配包里那一条规则，而 `strategy` + `fulfilment` 正好定位它。
            "summary": (
                f"strategy {strategy_id} commits us to "
                f"{rule.get('creates_commitment')}, discharged by "
                f"{rule.get('fulfilment')}"
            )[:500],
            "source_event_id": source_event_id,
            # **只有 `ai_commitment_due` 那一族有到期时刻** ——
            # 见 docstring 第 2 条与 `DUE_FULFILMENT` 的注释。
            "due_at": (
                (now + timedelta(hours=hours)).isoformat()
                if str(rule.get("fulfilment")) == DUE_FULFILMENT
                else None
            ),
            "status": INITIAL_STATUS,
            # 证据是这一轮那条入站事件本身：**它是这条义务的由来**。
            "evidence_refs": [source_event_id],
            "scheduled_event_id": None,
            "generation": int(generation),
            "version": 0,
        }
        rows.append({**row, "_dedupe_key": _ai_dedupe_key(source_event_id, ordinal)})
    return rows, []


def _insert_rows(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把这几行写进 `commitments`，回它们校验过的文档。

    **全仓唯一那条 `INSERT INTO commitments`** —— 与 `policy_output` 那条
    「`INSERT INTO policy_decisions` 只许一处」同一条纪律，
    由 `tests/contracts/test_commitment_ledger.py` 钉住。

    **两侧共用它，而判定行各写各的**：那几行的 `hook` 必须是**字面量**
    （`test_every_policy_output_names_its_hook_literally` ——
    全仓靠读那个字面量数每个时点有几个发出方），
    所以这里只抽出插入，不抽出判定。

    **`ON CONFLICT DO NOTHING` 是投递语义的那一半**：重放同一条事件时
    `(tenant_id, conversation_id, dedupe_key)` 撞上，那一行不再写第二遍。
    """

    written: list[dict[str, Any]] = []
    for row in rows:
        document = {key: value for key, value in row.items() if not key.startswith("_")}
        document["tenant_id"] = str(tenant_id)
        validate_contract("commitment-record", document)
        connection.execute(
            """
            INSERT INTO commitments(
                tenant_id, id, conversation_id, party, type, summary, source_event_id,
                due_at, status, evidence_refs, scheduled_event_id, generation, version,
                dedupe_key
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, conversation_id, dedupe_key) DO NOTHING
            """,
            (
                tenant_id,
                UUID(str(document["id"])),
                conversation_id,
                document["party"],
                document["type"],
                document["summary"],
                UUID(str(document["source_event_id"])),
                _as_datetime(document["due_at"]),
                document["status"],
                Jsonb(list(document["evidence_refs"])),
                None,
                int(document["generation"]),
                int(document["version"]),
                row["_dedupe_key"],
            ),
        )
        written.append(document)
    return written


def write_ai_admitted(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    refusals: Sequence[tuple[str, str]],
) -> list[str]:
    """台账 **`party=ai`** 那几行的写入 —— 与客户侧同形，**时点不同**。

    判定行落在 `post_plan`，不是 `post_understanding`：
    **AI 的义务由打法决定，而打法要到计划步才定下来。**
    读 `policy_decisions` 的人据这个时点判断「这条义务是怎么来的」。

    ## 为什么不是给 `write_admitted` 加一个 `hook` 参数

    试过，被 `test_every_policy_output_names_its_hook_literally` 挡回来，
    **而它挡得对**：全仓靠读 `PolicyOutput(hook=...)` 那个**字面量**
    数每个时点有几个发出方，`hook=hook` 在 AST 里读不出值 ——
    于是 `post_understanding` 会被少算三个，
    而「哪两个时点今天零写者」那条断言正是靠这个计数。

    **共用的那一半抽成了 `_insert_rows`**：
    `INSERT INTO commitments` 全仓仍然只有一条。

    ## `summary` 不进判定行，与客户侧逐字同源

    客户侧那条注释写着理由：`policy_decisions.context` 是本仓一直在收窄的
    那一格（`withheld_content` 那次的教训）。AI 侧的 `summary` 是规则拼的、
    不含客户原话，**而口径照旧** —— 要看正文去台账。
    """

    for reason_code, detail in refusals:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_plan",
                effect="deny",
                reason_code=reason_code,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={"detail": detail[:500]},
            ),
        )

    for document in _insert_rows(
        connection, tenant_id=tenant_id, conversation_id=conversation_id, rows=rows
    ):
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_plan",
                effect="allow",
                reason_code=ADMITTED_REASON_CODE,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={
                    "commitment_type": document["type"],
                    "has_due_at": document["due_at"] is not None,
                },
            ),
        )

    def _announce_overflow(live: int, bound: int) -> None:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_plan",
                effect="modify",
                reason_code=REFS_TRUNCATED_REASON_CODE,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={"live": live, "bound": bound},
            ),
        )

    return current_refs(
        connection,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        on_overflow=_announce_overflow,
    )


def write_admitted(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    refusals: Sequence[tuple[str, str]],
) -> list[str]:
    """把收下的那些写进 `commitments`，并给每一条判定落一行 `policy_decisions`。

    **全仓唯一那条 `INSERT INTO commitments`** —— 与 `policy_output` 那条
    「`INSERT INTO policy_decisions` 只许一处」同一条纪律，
    由 `tests/contracts/test_commitment_ledger.py` 钉住。

    返回写进去的那些 id，调用方用它填 `conversation_aggregates.commitment_refs`。

    **`ON CONFLICT DO NOTHING` 是投递语义的那一半**：重放同一条事件时
    `(tenant_id, conversation_id, dedupe_key)` 撞上，那一行不再写第二遍。
    **而 `RETURNING id` 因此只回新写的那些** —— 所以 refs 用一次 `SELECT`
    现读，不用 `RETURNING` 的结果：重放那一轮必须仍然得到完整的 refs，
    否则聚合上的那一列会在重放时变短。
    """

    for reason_code, detail in refusals:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_understanding",
                effect="deny",
                reason_code=reason_code,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={"detail": detail[:500]},
            ),
        )

    for document in _insert_rows(
        connection, tenant_id=tenant_id, conversation_id=conversation_id, rows=rows
    ):
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_understanding",
                effect="allow",
                reason_code=ADMITTED_REASON_CODE,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={
                    "commitment_type": document["type"],
                    # **不写 `summary`** —— 它是客户原话的摘要，而
                    # `policy_decisions.context` 是本仓一直在收窄的那一格
                    # （`withheld_content` 那次的教训）。要看正文去台账。
                    "has_due_at": document["due_at"] is not None,
                },
            ),
        )

    def _announce_overflow(live: int, bound: int) -> None:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=uuid4(),
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="post_understanding",
                effect="modify",
                reason_code=REFS_TRUNCATED_REASON_CODE,
                policy_id=POLICY_ID,
                policy_version=POLICY_VERSION,
                obligations=("audit",),
                context={"live": live, "bound": bound},
            ),
        )

    return current_refs(
        connection,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        on_overflow=_announce_overflow,
    )


def refs_bound() -> int:
    """`commitment_refs` 的 `maxItems`，**从契约读，不写第二遍**。

    这个数今天是 20，而契约里那段 description 自己写着它**不是从真实分布来的**
    （「今天没有承诺台账、没有真实流量，拍不出来」），到期条件逐字是
    「承诺台账落地后，按真实分布重定」—— **也就是本批之后**。
    本批不重定它（没有流量可数），但把它从「一个没人执行的数」变成
    **一道会说话的界**，见 `current_refs`。
    """

    slot = load_schema("conversation-aggregate")["properties"]["commitment_refs"]
    return int(slot["maxItems"])


def stored_due_rows(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    dedupe_keys: Sequence[str],
) -> list[dict[str, Any]]:
    """这一轮那几条 dedupe_key 在**库里**对应的 `(id, due_at)`。

    ## 为什么不能用刚刚写进去的那份内存行

    `admit_customer_commitments` 每一轮给每条候选**现生成一个 `uuid4()`**，
    而去重靠的是 `(tenant_id, conversation_id, dedupe_key)`。
    于是**重放同一条入站事件时**：`ON CONFLICT DO NOTHING` 什么都不写，
    而内存里那一份带着一个**库里根本不存在的 id**。

    拿那个 id 去建定时器，`064` 的外键会当场拒掉整条事务 ——
    一条本该幂等的重放变成一个死循环。
    **这正是那条外键存在的意义**：没有它，那一行会带着一个指向空处的
    `commitment_id` 安静地写进去，而没有任何东西会红。

    所以这里现读：新写的那一轮读到刚写的行，重放那一轮读到原来那一行，
    两轮拿到的是**同一个 id**。`due_at` 一并读回，理由相同 ——
    以库里那一行为准。
    """

    if not dedupe_keys:
        return []
    rows = connection.execute(
        """
        SELECT id, due_at, party FROM commitments
         WHERE tenant_id = %s AND conversation_id = %s AND dedupe_key = ANY(%s)
         ORDER BY created_at, id
        """,
        (tenant_id, conversation_id, list(dedupe_keys)),
    ).fetchall()
    # **`party` 也读回来（T364，2026-09-19）**：排定时器那一步要按党派筛
    # `emit_kind` —— 一条 AI 的承诺不该实现一条 `customer_promise_due` 规则。
    # 与 `due_at` 同一条理由：**以库里那一行为准**，不拿内存里那一份。
    return [
        {"id": str(row["id"]), "due_at": row["due_at"], "party": str(row["party"])}
        for row in rows
    ]


def current_refs(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    on_overflow: Callable[[int, int], None] | None = None,
) -> list[str]:
    """这条会话今天**活着的**承诺 id，升序，**且不超过契约那道界**。

    「活着」= 还没有走到终态。**这个集合就是 `commitment_refs` 的定义**
    （`conversation-aggregate` 那一格逐字写着「活跃/待证据的承诺 id」），
    所以它从库里现读，不从内存里攒 —— 重放同一条事件时也必须得到同一份。

    ## 为什么这里必须有一道界，而不是「反正不会超」

    `commitment_refs` 的 `maxItems` 是 **20**。一条长会话累积到第 21 条活着的
    承诺时，不截的话写回聚合当场不过契约 —— 而聚合的校验点遍布**读路径**
    （接管、交回、`pipeline` 多处都是读出一行就地校验），
    **于是它不是一次写入失败，是那条会话的那几条路径 500**。

    **这正是本轮刚修过的那个形状**（人工写槽位值那道界：写入侧没有界、
    读路径上炸）。同一个坑不在同一个月里踩第二次。

    所以：**截到界内，并且让截这件事出声**（`on_overflow` 落一行
    `policy_decisions`）。截掉的是**较新的那些** —— 保留最早的，
    因为最早的那些是最可能已经到期、最该被人看见的。
    **台账本身一行不丢**：被截的只是聚合上那份投影。
    """

    rows = connection.execute(
        """
        SELECT id FROM commitments
         WHERE tenant_id = %s AND conversation_id = %s
           AND status NOT IN ('fulfilled', 'withdrawn', 'superseded')
         ORDER BY created_at, id
        """,
        (tenant_id, conversation_id),
    ).fetchall()
    live = [str(row["id"]) for row in rows]
    bound = refs_bound()
    if len(live) > bound and on_overflow is not None:
        on_overflow(len(live), bound)
    return live[:bound]


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
