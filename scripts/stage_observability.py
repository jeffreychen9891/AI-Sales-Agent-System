#!/usr/bin/env python3
"""Push the Stage v2 minimum operational metrics to Grafana Cloud via OTLP/HTTP.

The collector is deliberately independent from the application image. It performs
read-only checks against the v2 Docker project, Redis stream group, PostgreSQL audit
facts, and the local backup directory. A root-only marker can force the observer
health metric to zero for an end-to-end alert drill without stopping the service.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ENV_FILE = Path("/etc/fastagent-v2-stage/observability.env")
BACKUP_DIR = Path("/var/backups/fastagent-v2-stage")
STATE_FILE = Path("/var/lib/fastagent-v2-stage-observability/last-push.json")
DRILL_MARKER = Path("/run/fastagent-v2-stage/force-unhealthy-observer")
QUEUE_STREAM = "fastagent:v2:inbox"
QUEUE_GROUP = "fastagent:v2:workers"

CONTAINERS = {
    "api": "fastagent-v2-stage-api-1",
    "ingress": "fastagent-v2-stage-ingress-1",
    "worker": "fastagent-v2-stage-worker-1",
    "scheduler": "fastagent-v2-stage-scheduler-1",
    "postgres": "fastagent-v2-stage-postgres-1",
    "queue_redis": "fastagent-v2-stage-queue-redis-1",
    "cache_redis": "fastagent-v2-stage-cache-redis-1",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def run(command: list[str], timeout: float = 10.0) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed commands, no user input
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def container_healthy(name: str) -> float:
    try:
        state = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name,
            ]
        ).split()
    except (OSError, subprocess.SubprocessError):
        return 0.0
    is_healthy = bool(state) and state[0] == "running"
    return float(is_healthy and (len(state) == 1 or state[1] == "healthy"))


def http_healthy(url: str) -> float:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed localhost URL
            return float(200 <= response.status < 300)
    except (OSError, ValueError):
        return 0.0


def queue_depth() -> tuple[float, float, float]:
    command = (
        'export REDISCLI_AUTH="$(cat /run/secrets/queue_redis_password)"; '
        f"exec redis-cli --raw XINFO GROUPS {QUEUE_STREAM}"
    )
    try:
        output = run(["docker", "exec", CONTAINERS["queue_redis"], "sh", "-ec", command])
    except (OSError, subprocess.SubprocessError):
        return 0.0, 0.0, 0.0
    fields = output.splitlines()
    groups: list[dict[str, str]] = []
    for index in range(0, len(fields) - 1, 2):
        if fields[index] == "name" and groups:
            groups.append({})
        if not groups:
            groups.append({})
        groups[-1][fields[index]] = fields[index + 1]
    group = next((item for item in groups if item.get("name") == QUEUE_GROUP), {})
    lag = float(group.get("lag", 0) or 0)
    pending = float(group.get("pending", 0) or 0)
    return lag + pending, lag, pending


#: Every cost-governor outcome that has to be visible, and the metric each one
#: feeds. Splitting `cost_hard_ceiling` into three reason codes (T43) split the
#: single counter that the accepted S-06 rule watches; anything not named here
#: lands only in `no_send_total`, and no S-06 rule is built on that.
#:
#: Independent review 2026-09-03, P1-2: the first version of this batch counted
#: only the warning, so **the two new refusals -- "a customer was refused" and
#: "settlement is broken" -- woke nobody at all**. That is the failure ADR-0034
#: boundary 3 names, and it is also the only realistic backstop for `K` being
#: mis-fitted: if premature breaking reappears at `hard x K`, this is the one
#: path that lets a person see it.
CEILING_REASON_METRICS = {
    # A refusal that means money was actually charged. Existing accepted rule
    # `S-06 Stage hard-ceiling triggered` is built on this metric -- do not
    # rename it.
    "cost_hard_ceiling": "fastagent_v2_stage_hard_ceiling_total",
    # A refusal from the concurrency guard. Nobody spent the money; too much is
    # in flight to be sure. Needs its own rule.
    "cost_estimated_hard_ceiling": "fastagent_v2_stage_estimated_hard_ceiling_total",
    # A refusal because settlement stopped. This is a platform fault, and it is
    # tenant-wide: one broken settlement path pushes every conversation of that
    # tenant to a human. Needs its own rule.
    "cost_unsettled_backlog": "fastagent_v2_stage_unsettled_backlog_total",
    # Not a refusal -- the call proceeded. The estimate crossed a ceiling the
    # settled spend has not.
    "cost_estimated_ceiling": "fastagent_v2_stage_estimated_ceiling_total",
}


#: T55 / ADR-0040. Money the ledger cannot account for, counted on its own.
#: It is not a ceiling reason code, so it does not belong in
#: `CEILING_REASON_METRICS`; it is a row state, and the question it answers is
#: "is settlement working?" rather than "did someone spend too much?".
UNRECONCILED_METRIC = "fastagent_v2_stage_unreconciled_total"

#: Must equal `RECONCILE_AFTER_SECONDS` in `worker/cost.py`. Duplicated rather
#: than imported because this collector is deliberately independent of the
#: application image; a contract test asserts the two agree.
#:
#: **The counter matches the gate's criterion, not just the row state**
#: (independent review 2026-09-03, P1-1). Counting only `status='unreconciled'`
#: meant the metric read `0.0` on exactly the deployments this batch admits are
#: likely -- the ones where the reconciler was never installed, and where two
#: hundred rows can sit rotting in `estimated`. A metric that is zero because
#: nothing writes the state it counts is the same failure as a collector that
#: pushes seven series while everyone believes it pushes ten.
RECONCILE_AFTER_SECONDS = 900

#: T71 / ADR-0041. Three metrics, and two of them exist because the first one
#: cannot be trusted on its own.
#:
#: `..._balance_difference_cny` is |supplier's spend - ledger's settled sum| for
#: the day that just ended, from the newest **comparable** snapshot. Absolute
#: rather than signed so the Grafana rule stays the same shape as the others in
#: the group ("IS ABOVE"); the signed value stays in the table.
#:
#: **It is not emitted at all when there is nothing to report.** The first
#: version returned `0.0` on every unreadable path -- the healthiest number
#: there is. That is precisely the defect found on this Stage host the same day:
#: `unreconciled_total` read `0.0` because its table did not exist, and the rule
#: watching it showed `Normal` with nothing behind it. Grafana renders an absent
#: series as `No Data`, and the lesson recorded that day is the whole reason
#: for this choice -- **"No Data makes people look; Normal does not"**
#: (`evidence/m1a/stage-schema-level-2026-09-04.md`; independent review
#: 2026-09-04, T71 F4).
#:
#: The two ages answer two different failures:
#:
#: * `..._balance_snapshot_age_seconds` -- the newest row of **any** verdict:
#:   "is the job running at all?"
#: * `..._balance_comparison_age_seconds` -- the newest row that actually
#:   produced a difference: "is it still able to compare?" A revoked credential
#:   or a run of top-ups writes a fresh row every night that never yields a
#:   difference, so the first age stays green while the check is silently dead
#:   (T71 F5). The alert belongs on this one.
BALANCE_DIFFERENCE_METRIC = "fastagent_v2_stage_balance_difference_cny"
BALANCE_AGE_METRIC = "fastagent_v2_stage_balance_snapshot_age_seconds"
BALANCE_COMPARISON_AGE_METRIC = "fastagent_v2_stage_balance_comparison_age_seconds"

#: What an unreadable or empty table reports for an age. Not 0.0: zero age means
#: "just checked", which is the one thing an absent answer cannot promise. A
#: number this large trips any freshness rule immediately, which is the correct
#: behaviour for "I cannot tell you how old it is".
BALANCE_AGE_WHEN_UNKNOWN = 10 * 24 * 3600.0

#: 三条余额查询，**提到模块顶层是为了让用例能真的跑一遍它们**。
#:
#: ## 为什么这三个串必须是常量
#:
#: 2026-09-08 起（建机当天），下面第三条每分钟在 postgres 上报一次语法错误，
#: **连报 14 天、20,147 次**，直到 2026-09-22 才被运维从容器日志里翻出来
#: （`docs/handoffs/ops-to-author-the-memory-write-takes-the-turn-with-it-2026-09-22.md`
#: 缺陷 3）。
#:
#: **后果不是「少了一条曲线」，是「多了一条假曲线」**：三条查询共用一个
#: `try`，一条炸就整体走异常分支，于是 `balance_snapshot_age_seconds` 与
#: `balance_comparison_age_seconds` **双双变成 864000.0** —— 那是
#: `BALANCE_AGE_WHEN_UNKNOWN`，10 天整，一个哨兵不是一个测量值。
#: 而采集器 `otlp_http_status: 200`：**它推送成功**。面板上有一条活着的、
#: 恒定的、假的曲线。
#:
#: ## 为什么既有的守卫是绿的
#:
#: `tests/contracts/test_observability_declarative_copy.py` 钉的是**指标名**：
#: 告警规则引用的名字必须是采集器真的会发的名字。**这个 bug 下它全绿** ——
#: 名字在，也真的发了，发的是异常分支的常量。**它钉的是名字，
#: 不是这条查询跑不跑得通。**
#:
#: 所以配一条会跑它们的用例（`tests/integration/test_the_balance_queries_run.py`），
#: 而那条用例要拿得到这三个串 —— 这就是它们从函数局部提上来的全部理由。
BALANCE_DIFFERENCE_SQL = (
    "SELECT coalesce(abs(difference_cny)::text, '') FROM provider_balance_snapshots "
    "WHERE difference_cny IS NOT NULL ORDER BY taken_at DESC LIMIT 1;"
)

BALANCE_AGE_SQL = (
    "SELECT coalesce(extract(epoch FROM now() - max(taken_at)), -1)::text "
    "FROM provider_balance_snapshots;"
)

#: **Two different unknowns, two different numbers** (re-review 2026-09-04, R3).
#: "No snapshot has ever been taken" is the sentinel; "snapshots exist but none
#: has ever produced a comparison" is a real, growing age -- measured from the
#: oldest snapshot, because that is how long the job has been running without
#: ever managing to compare. Reporting the sentinel for both made any threshold
#: below ten days fire the moment the rule was created and keep firing for two
#: days, which teaches an operator to silence it.
#:
#: ⚠️ **`FILTER` 贴在聚合函数上，不是贴在外面那个 `extract()` 上。**
#: 2026-09-08 到 09-22 这里写的是
#: `extract(epoch FROM now() - max(taken_at)) FILTER (WHERE …)`，
#: PostgreSQL 报 `syntax error at or near "FILTER"` —— **移一个右括号的事**，
#: 而它让两条指标假了 14 天。`max(taken_at) FILTER (…)` 在没有匹配行时回 `NULL`，
#: `now() - NULL` 也是 `NULL`，于是 `coalesce` 落到第二支 —— 正是上面那段要的语义。
BALANCE_COMPARISON_AGE_SQL = (
    "SELECT coalesce("
    "extract(epoch FROM now() - max(taken_at) FILTER (WHERE difference_cny IS NOT NULL)), "
    "extract(epoch FROM now() - min(taken_at)), -1)::text "
    "FROM provider_balance_snapshots;"
)


def balance_reconciliation() -> tuple[float | None, float, float]:
    """The newest comparison, and two ages.

    Returns `None` for the difference when there is no comparable snapshot --
    the caller then emits no series at all rather than a reassuring zero.
    """

    difference_sql = BALANCE_DIFFERENCE_SQL
    age_sql = BALANCE_AGE_SQL
    comparison_age_sql = BALANCE_COMPARISON_AGE_SQL
    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"'
    try:
        difference_output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", difference_sql]
        )
        age_output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", age_sql]
        )
        comparison_output = run(
            [
                "docker",
                "exec",
                CONTAINERS["postgres"],
                "sh",
                "-ec",
                command,
                "sh",
                comparison_age_sql,
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return None, BALANCE_AGE_WHEN_UNKNOWN, BALANCE_AGE_WHEN_UNKNOWN
    try:
        raw_difference = difference_output.strip()
        difference = float(raw_difference) if raw_difference else None
        age = float(age_output.strip())
        comparison_age = float(comparison_output.strip())
    except ValueError:
        return None, BALANCE_AGE_WHEN_UNKNOWN, BALANCE_AGE_WHEN_UNKNOWN
    # `-1` is the marker for "no row at all": staler than any real age rather
    # than fresher than all of them.
    return (
        difference,
        BALANCE_AGE_WHEN_UNKNOWN if age < 0 else age,
        BALANCE_AGE_WHEN_UNKNOWN if comparison_age < 0 else comparison_age,
    )


def abandoned_reservation_count() -> float:
    """Reservations that left and never came back, by the **gate's** criterion.

    `unreconciled` **or** still `estimated` past `RECONCILE_AFTER_SECONDS` --
    the same test `CostGovernor._snapshot` applies. Only the reconciler writes
    the `unreconciled` state, and the reconciler is not installed anywhere yet,
    so a state-only count would report `0.0` on precisely the systems that have
    a problem.

    Zero is the expected value; any sustained increase means calls are leaving
    and not coming back, and **what they cost is unknowable** -- the ledger holds
    no supplier-side identifier to ask about.
    """

    # Written out rather than interpolated, same habit as `database_counts()`:
    # building SQL by f-string is the wrong reflex even where every value is a
    # module constant. The guard below keeps the literal and the constant in
    # step -- change one without the other and this raises rather than quietly
    # counting the wrong window.
    sql = (
        "SELECT count(*) FROM model_invocation_costs "
        "WHERE status='unreconciled' "
        "OR (status='estimated' AND occurred_at <= now() - interval '900 seconds');"
    )
    if f"interval '{RECONCILE_AFTER_SECONDS} seconds'" not in sql:
        raise AssertionError(
            f"the query's age window disagrees with RECONCILE_AFTER_SECONDS "
            f"({RECONCILE_AFTER_SECONDS}s); the metric would not match the gate"
        )
    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"'
    try:
        output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", sql]
        )
        return float(output.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def database_counts() -> tuple[float, dict[str, float]]:
    """Cumulative no-send count, plus one count per ceiling reason code.

    T43 / ADR-0034 / ADR-0036. These counters are what make "the estimate may
    only warn" a decision rather than a way of doing nothing, and what keep the
    two new refusals from being invisible. A warning never becomes a no-send --
    the call proceeds -- so it appears in none of the other numbers, and before
    T43 it appeared nowhere at all: the governor wrote a `policy_decisions` row
    that no query in this repository read.

    Counted per reason code rather than as one total because they warrant
    different responses and therefore different thresholds: one says a customer
    was refused for money actually spent, one says a guard fired, one says
    settlement is broken, one says spending may be approaching a limit.
    """

    # Written out rather than assembled from `CEILING_REASON_METRICS`. Building
    # SQL by interpolation is the wrong habit to keep even where every value is
    # a module constant, and the guard below keeps the two in step without it:
    # add a reason code and forget the query, and this raises rather than
    # silently emitting a counter that is always zero.
    sql = (
        "SELECT count(*) FILTER (WHERE action='plan.no_send'), "
        "count(*) FILTER (WHERE reason_code='cost_hard_ceiling'), "
        "count(*) FILTER (WHERE reason_code='cost_estimated_hard_ceiling'), "
        "count(*) FILTER (WHERE reason_code='cost_unsettled_backlog'), "
        "count(*) FILTER (WHERE reason_code='cost_estimated_ceiling') FROM audit_events;"
    )
    missing = [reason for reason in CEILING_REASON_METRICS if f"'{reason}'" not in sql]
    if missing:
        raise AssertionError(f"reason codes counted by no query, so alerting nobody: {missing}")
    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"'
    empty = {reason: 0.0 for reason in CEILING_REASON_METRICS}
    try:
        output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", sql]
        )
        fields = output.split("|")
        if len(fields) != 1 + len(CEILING_REASON_METRICS):
            return 0.0, empty
        return float(fields[0]), {
            reason: float(value)
            for reason, value in zip(CEILING_REASON_METRICS, fields[1:], strict=True)
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0, empty


#: The `plan.no_send` reason codes that get a series of their own. Bounded on
#: purpose (ADR-0101 §三.1: labels must be bounded) -- a reason code is written
#: by whichever guard refused, and that set grows; anything not named here is
#: folded into `other` so the cardinality cannot follow the code.
#:
#: `PROVIDER_MALFORMED_OUTPUT` is the reason this metric exists at all: ADR-0084
#: Q4 ruled "fail closed, do not retry, do not repair -- **but count it**", and
#: until now it was counted only inside the unlabelled `no_send_total`, which no
#: rule can be built on (user ruling 2026-09-07, ADR-0101 §三.2 增补).
NO_SEND_REASON_CODES = (
    "PROVIDER_MALFORMED_OUTPUT",
    "GENERATION_PAYLOAD_INCOMPLETE",
    # **The refusal that replaced a terminal delivery failure** (independent
    # review 2026-09-10, P2-2). Content a text channel cannot read used to be
    # queued and then go `FAILED` in `outbox_messages`; it is refused at the
    # plan step now. That is the better place -- but the batch that moved it
    # would have moved it out of sight: an unlisted code folds into `other`,
    # and **the incident that motivated the whole batch was found by a person
    # reading `outbox_messages`**. A refusal nobody can count is the shape this
    # batch argues against.
    "GENERATION_CONTENT_UNREADABLE",
    "GENERATION_PRODUCED_NO_INTENT",
    "GENERATION_INTENT_KIND_UNSUPPORTED",
    # **E4-2 的那个码**，上面那条注释写的正是同一件事。
    # `remember` 的四道处境闸全部写它（字段没被 profile 声明、这一回合不是客户
    # 消息、没有 contact、字段已经有一条 active 记忆），而**按 T311 这条路
    # 端到端还不可达**——它一旦开始产出记录，**第一批记录全是拒绝**，
    # 不列出来就整批折进 `other`。
    #
    # **漏掉它的方式值得记下来**：独立审查第一轮的 P3-1 说的就是这一条，
    # 而作者把它与**另一个批次同号的 P3-1** 记混了，于是处置表里整行缺席。
    # 复审方的话：「在一张自称『五条全部处置』的表里，
    # **被漏掉的那条比被拒绝的那条更难发现**」。
    "GENERATION_INTENT_NOT_RECORDABLE",
    "HUMAN_CONTROL_ACTIVE",
    # **T368 的那个码**，而它是**门禁替我发现的**。
    #
    # 批 F 立 `GENERATION_PROMISED_WITHOUT_DELIVERING` 时它只是一个
    # `GenerationRejected` 的理由码 —— 走的是整份拒那条路，不是计划步拒绝，
    # 所以它当时不属于这张表。**批 H 让它变成了 `ProposalDecision.reason_code`**：
    # 速率闸把一轮劈开、而剩下那条除了许诺什么都没有时，那几条提案被扣住，
    # 带的就是这个码。
    #
    # **于是它成了一条计划步拒绝，而它没有自己的序列** ——
    # `test_every_plan_step_refusal_the_pipeline_can_write_has_its_own_series`
    # 从 `pipeline.py` 的源码里解析 `reason_code=<常量>` 现读出来并当场红。
    # 那条用例的理由逐字适用：**折进 `other` 的拒绝是没有人数得出来的拒绝**，
    # 而这一种恰恰是「客户这一轮收不到东西」。
    "GENERATION_PROMISED_WITHOUT_DELIVERING",
    # ========================================================================
    # **T372（2026-09-19）：下面这 24 个一直折进 `other`，而它们全都发生得了。**
    #
    # 上面那几条注释各自论证过「一个折进 `other` 的拒绝是没有人数得出来的拒绝」。
    # **而这张清单在 2026-09-19 只有 8 条。** 实测（基线 `d5207dbd`）：
    # 那两条 `except` 能带出 **25** 个码 —— 其中 **1** 个（`cost_hard_ceiling`）
    # 有自己的专用序列，**剩下 24 个一条序列都没有，全折进 `other`**。
    # ⚠️ **原有那 8 条里只有 1 条落在这 25 个里**（`PROVIDER_MALFORMED_OUTPUT`）——
    # 另外 7 条走的是别的路（计划步拒绝那一族的字面量），
    # **也就是说这张清单先前几乎没盖到「异常转成 no-send」这条路。**
    #
    # ⚠️ **而看着这件事的那道守卫，一条都抓不到**：
    # `test_every_plan_step_refusal_the_pipeline_can_write_has_its_own_series`
    # 只扫 `pipeline.py` 一个文件里 `reason_code="GENERATION_` 这一种写法，
    # 而这 24 个分三族：15 个由**另一个文件**的 `GenerationRejected(...)` 用
    # **位置实参** raise、6 个是 `providers.py` 里异常类的**类属性**、
    # 1 个是 `getattr(error, "reason_code", "GENERATION_FAILED")` 那个
    # **连定义点都没有的兜底默认**、1 个是第二条 `except` 自己写死的字面量
    # （`PROACTIVITY_POLICY_UNREADABLE` —— 它**在** `pipeline.py` 里，
    # 而旧扫描按 `GENERATION_` 前缀过滤，所以也漏了它）。
    #
    # **本批把那道守卫的判据换了**：不问「谁在 `pipeline.py` 里写了码」，
    # 问「那两条 `except` 抓的是哪几类异常，它们各自能带出哪些码」——
    # 于是新加一个子类、换一个文件、改一种写法，这里都跟着走。
    #
    # **为什么值得 24 条标签值**：`no_send_total` 是**一个**指标 + 一个 `reason`
    # 标签，所以这不是新增 24 条指标，`REQUIRED_METRICS` 一行都不用动。
    # 而它们答的是 stage 看板上唯一一个还答不出来的问题：**`other` 里是什么。**
    #
    # 一 · **供应商那一族**（`providers.py` 的类属性）。`PROVIDER_MALFORMED_OUTPUT`
    # 早就在上面 —— 而它的六个同族兄弟一个都不在，**它们同样都是「客户这一轮
    # 收不到东西」**，且各自指向完全不同的处置：凭据没配是部署问题，
    # 超时与熔断是容量问题，`INPUT_TOO_LARGE` 是我们自己把请求造大了。
    "PROVIDER_FAILURE",
    "PROVIDER_CREDENTIAL_MISSING",
    "PROVIDER_TIMEOUT",
    "PROVIDER_HTTP_ERROR",
    "PROVIDER_CIRCUIT_OPEN",
    "PROVIDER_INPUT_TOO_LARGE",
    # 二 · **检索那一条成本顶**。它与 `cost_hard_ceiling` 不同：
    # 那一个有自己的专用序列（`CEILING_REASON_METRICS`），**这一个没有**。
    "RETRIEVAL_COST_CEILING",
    # 三 · **兜底那个默认**。`getattr(error, "reason_code", "GENERATION_FAILED")`
    # —— **它没有定义点**，只是一个字面量，所以任何按「找常量」或
    # 「找 raise」的扫描都看不见它。而它一旦出现，说的是
    # **「有一类异常我们连它是什么都没分类」** —— 最该有人看见的那一种。
    "GENERATION_FAILED",
    # 四 · **校验器 raise 的那 15 个**（`inference_validation.py`）。
    # 它们是「模型交回来的东西不合规，整轮拒」那一族 ——
    # **每一个都意味着客户这一轮收不到回复**，而它们各自指向完全不同的原因：
    # 引了没递过的来源 / 改了打法 / 多发了一条意图 / 抄了素材原文 / 一轮问两个问题。
    # 折在一起看，只能看到「今天拒了很多」，看不出该修哪一条提示词。
    "GENERATION_CHANGED_STRATEGY",
    "GENERATION_ADDED_INTENT",
    "GENERATION_DUPLICATED_INTENT",
    "GENERATION_MATERIAL_REF_UNKNOWN",
    "GENERATION_ASSET_NOT_OFFERED",
    "GENERATION_KNOWLEDGE_REF_UNKNOWN",
    # T363（2026-09-19）：引了一条这一轮没递过的记忆。
    "GENERATION_MEMORY_REF_UNKNOWN",
    "GENERATION_PAYLOAD_DROPPED_MATERIAL",
    "CLAIM_WITHOUT_EVIDENCE",
    "INTENT_WITHOUT_EVIDENCE",
    "MATERIAL_QUOTE_REQUIRED",
    "MORE_THAN_ONE_QUESTION",
    "RETURN_CONFIRMATION_MISSING",
    "SILENT_STRATEGY_PRODUCED_CONTENT",
    "UNREGISTERED_COMMITMENT",
    # 五 · **第二条 `except` 自己写死的那个**。商家的 `proactivity_policy`
    # 读不出来时这一轮不发 —— 而那是**配包错误**，不是运行期条件
    # （`ProactivityPolicyError` 的 docstring 逐字这么写）。
    # 它折进 `other` 时，一个写坏了配包的商家在看板上与「模型今天不听话」同色。
    "PROACTIVITY_POLICY_UNREADABLE",
)

#: One row per fact, so a single `psql` call answers all six. Shape:
#: `kind|label_a|label_b|value`.
GOVERNANCE_SQL = (
    "SELECT 'no_send', reason_code, '', count(*)::numeric FROM audit_events "
    "WHERE action = 'plan.no_send' GROUP BY reason_code "
    "UNION ALL SELECT 'abandoned', 'root_target_frame_abandoned', '', count(*)::numeric "
    "FROM audit_events WHERE reason_code = 'ROOT_TARGET_FRAME_ABANDONED' "
    "UNION ALL SELECT 'exhausted', '', '', count(*)::numeric FROM audit_events "
    "WHERE reason_code = 'DELIVERY_ATTEMPTS_EXHAUSTED' "
    "UNION ALL SELECT 'delivery_unknown', '', '', count(*)::numeric FROM outbox_messages "
    "WHERE status = 'UNKNOWN' "
    # **B4-4a, independent review P3-1.** `CANCELLED` is the one terminal
    # state that by design tells nobody: it writes no receipt and makes no
    # manual task, both deliberately (migration `048`). Without a count, a
    # clock skew or a mistyped `active_window_seconds` could retire every
    # proactive message a tenant has and **nothing would surface it** --
    # the quietest failure this repository can produce. Counted from the
    # rows rather than from `audit_events`, because the row is the fact
    # and the audit line is a description of it.
    "UNION ALL SELECT 'proactivity_cancelled', '', '', count(*)::numeric "
    "FROM outbox_messages WHERE status = 'CANCELLED' "
    # **交接发生了，而没人被通知**（复核 2026-09-17 §2.3）。
    #
    # `pipeline` 在没有联系人时写这一行，它的注释说得很清楚：
    # 「an audit row rather than a metric … **can be counted later**
    # without deciding today what the alert should be」——**今天就是那个 later**。
    #
    # 与上面 `proactivity_cancelled` 同形，而且更贵：那一条丢的是一条主动消息，
    # 这一条丢的是**一个本该有人接手的客户** —— 会话停在人接管状态，
    # `last_outbound_at` 是 `null`，而客户还在等。
    #
    # **它的修法在租户配置里**（`PUT /tenants/current/manager-contact`，ADR-0104），
    # 不在代码里 —— 所以它必须看得见，否则没有人知道该去配。
    "UNION ALL SELECT 'manager_notice_undeliverable', reason_code, '', "
    "count(*)::numeric FROM audit_events "
    "WHERE action = 'manager_notice.undeliverable' GROUP BY reason_code "
    # **一次「宁可沉默」也要数得出来**（复核 2026-09-18 退回那一条，
    # 用户裁定「拒绝可以，但要有人被告知」）。
    #
    # 连着两次有人工任务叫人（`LOOP_ESCALATION_REASON_CODES`），
    # **而第一次没有** —— 一次「我们替客户认了一笔他没说过的账，
    # 于是这一轮没说话」不该只活在 `policy_decisions` 里等人恰好去翻。
    #
    # 与 `manager_notice_undeliverable` 同一条理由：
    # **它的修法不在代码里**（在提示词与配包话术里），所以它更要看得见。
    "UNION ALL SELECT 'unfounded_fact_refusal', '', '', "
    "count(*)::numeric FROM policy_decisions "
    "WHERE effect = 'deny' AND reason_code = 'GENERATION_ASSERTED_AN_UNFOUNDED_CUSTOMER_FACT' "
    # **一整条回复被判成另一种语言而整份拒**（T258，2026-09-20）。
    #
    # 与上面那条**逐字同一条理由**：它的修法不在代码里
    # （在提示词与配包话术里），所以它更要看得见。
    #
    # ⚠️ **这个数只数最重的那一支** —— 丢掉英文那一段、其余照发时
    # 不产生 deny 行（客户照样收到话）。所以它涨，说明模型**整条**
    # 都在用另一种语言写，而那是提示词那一侧的事。
    #
    # **它数得出来，靠的是同一批把那个 raise 改成「丢法一致时报实际那个码」** ——
    # 在此之前一条纯语言的整份拒会写着「替客户认了一笔没说过的账」，
    # 于是这条查询**一条都数不到**，而上面那条会多数一条**错的**。
    "UNION ALL SELECT 'another_language_refusal', '', '', "
    "count(*)::numeric FROM policy_decisions "
    "WHERE effect = 'deny' AND reason_code = 'GENERATION_REPLIED_IN_ANOTHER_LANGUAGE' "
    # **§10.4 判据 2 那条**（批 F）：许了而这一轮兑现不了，
    # 且这条消息**每一句**都是那样一句许诺 —— 于是整份拒。
    #
    # 与上面那条同源地数出来，**而这个数只数最重的那一支**：
    # 丢掉许诺那一句、段里其余的字照发时不产生 deny 行（客户照样收到话）。
    # 它涨，说明模型在写「我这就给您 X」而拿不出 X，
    # 那要改的是提示词与配包话术。
    #
    # ⚠️ **先前这里写的是「它的常见形态不在这里」，而那个估计错过一次**
    # （复核 2026-09-18 退回批 F 第一条）：那时丢的单位是**段**，
    # 而生产的一段是多句的、纯文本回合又常常只有一段 ——
    # **于是常见形态恰恰就在这里**，这个数会比预期高得多。
    # 按句丢之后它才回到「只数最重那一支」。
    # **所以这个数的口径依赖那个单位** —— 谁改丢弃单位，回来重读这一段。
    "UNION ALL SELECT 'unkept_promise_refusal', '', '', "
    "count(*)::numeric FROM policy_decisions "
    "WHERE effect = 'deny' AND reason_code = 'GENERATION_PROMISED_WITHOUT_DELIVERING' "
    # **兜底响了几次**（批 G）。上面那个数只数**整份拒**，
    # 而常见的形态是**丢掉那一句、其余照发** —— 那一种不产生 deny 行，
    # 只进 `plan.generation_disagreed` 的 `evidence->'unkept_promises'`。
    #
    # **在批 G 之前它没有任何人数**，于是「提示词改完好点没有」量不出来：
    # 批 G 往两处提示词里加了「许了就把 X 放进下一条 `send_message`」，
    # **而判断它有没有用，唯一的读数就是兜底响的次数**。
    #
    # ⚠️ **数的是回合数，不是句数**：一个回合可以丢好几句，
    # 而要答的问题是「多少轮里模型许了却没兑现」。句数在那个数组里，随时取得到。
    "UNION ALL SELECT 'unkept_promise_dropped', '', '', count(*)::numeric "
    "FROM audit_events WHERE action = 'plan.generation_disagreed' "
    "AND jsonb_array_length(coalesce(evidence->'unkept_promises', '[]'::jsonb)) > 0 "
    "UNION ALL SELECT 'manual_open', task_class, notify, count(*)::numeric FROM manual_tasks "
    "WHERE status = 'open' GROUP BY task_class, notify "
    "UNION ALL SELECT 'rate_limited', coalesce(evidence->>'scope', 'unknown'), '', "
    "count(*)::numeric FROM audit_events WHERE action = 'plan.rate_limited' "
    "GROUP BY 2 "
    "UNION ALL SELECT 'scheduler_lag', '', '', "
    "coalesce(max(extract(epoch from (now() - due_at))), 0)::numeric "
    "FROM scheduled_events WHERE status = 'PENDING' AND due_at < now();"
)

#: **The callback-deletion trio** (ADR-0103 §二.2 plus the third the user added
#: on 2026-09-07). The provider deletes a callback address after ten
#: *consecutive* failures, so the gauge below is the only one of the three that
#: guards anything: the two counters are for reading afterwards.
#:
#: Shape: `kind|label_a|label_b|value`, same as `GOVERNANCE_SQL`.
#:
#: **Why the streak is computed here rather than stored.** A counter kept in the
#: ingress process would reset exactly when it matters -- the failures this
#: counts are the kind that comes with restarts -- and a column would be a
#: migration for a number that is already derivable. `audit_events` holds one
#: row per answer that was not a plain accepted `200`, and `inbox_events` holds
#: the accepted ones, so "how many non-200s since the last 200" is a query.
INGRESS_SQL = (
    "WITH answers AS ("
    "  SELECT evidence->>'config_id' AS config_id, created_at, true AS failed"
    "    FROM audit_events"
    "   WHERE action = 'ingress.non_2xx' AND evidence ? 'config_id'"
    "  UNION ALL"
    "  SELECT evidence->>'config_id', created_at, false FROM audit_events"
    "   WHERE action IN ('ingress.refused', 'ingress.answered') AND evidence ? 'config_id'"
    "  UNION ALL"
    "  SELECT config_id, created_at, false FROM inbox_events WHERE config_id IS NOT NULL"
    "), last_ok AS ("
    "  SELECT config_id, max(created_at) AS at FROM answers WHERE NOT failed GROUP BY config_id"
    ") "
    "SELECT 'ingress_streak', a.config_id, '',"
    "       count(*) FILTER ("
    "         WHERE a.failed AND (l.at IS NULL OR a.created_at > l.at))::numeric"
    "  FROM answers a LEFT JOIN last_ok l USING (config_id) GROUP BY a.config_id "
    "UNION ALL "
    "SELECT 'ingress_non_2xx', reason_code, '', count(*)::numeric FROM audit_events "
    " WHERE action = 'ingress.non_2xx' GROUP BY reason_code "
    "UNION ALL "
    "SELECT 'ingress_refused', reason_code, '', count(*)::numeric FROM audit_events "
    " WHERE action = 'ingress.refused' GROUP BY reason_code "
    # **`answered` needs its own series too** (independent review 2026-09-10,
    # P1-2). Until this line, `ingress.answered` rows appeared in this query
    # **only inside the `answers` CTE**, where their job is to clear the
    # consecutive-failure streak -- so the `reason_code` on them was written,
    # stored, and read by nothing.
    #
    # That was survivable while every answered reason meant "a notice went by".
    # It stopped being survivable when a reason arrived that carries facts about
    # **deliverability** (`OPERATION_HINT_NOTICE`, `msgType=1011`): the vendor's
    # 「操作提示」 bucket, which is where a send blocked by the recipient or by
    # risk control is reported. The batch that introduced it claimed a countable
    # series as the thing it delivered, and did not have one.
    #
    # **The label is the bucket, not the subset** (re-review P1-1): telling a
    # blocked send from an ordinary grey-print hint needs the
    # `msgUniqueIdentifier` suffix, which ingress deliberately does not read
    # yet. Until it does, read this series as "operation hints arrived", not as
    # "our messages are being blocked".
    "UNION ALL "
    "SELECT 'ingress_answered', reason_code, '', count(*)::numeric FROM audit_events "
    " WHERE action = 'ingress.answered' GROUP BY reason_code;"
)


def governance_counts() -> list[tuple[str, str, str, float]]:
    """The six governance facts ADR-0101 §三.2 asks for, in one round trip.

    **A measured zero is data; an unmeasurable value is not.** The rule "无数据
    不推 0.0，推 No Data" is about the second: if this query cannot run, the
    caller gets an empty list and **no series is pushed at all**, so the
    `absent()` half of the existing rules fires instead of a comforting flat
    zero. A count that really is zero is a fact and is pushed.

    Grouped facts (`no_send`, `manual_open`) produce one row per group that
    exists, so a group nobody has produced yet has no series -- which is the
    same principle, one level down.
    """

    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"'
    try:
        output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", GOVERNANCE_SQL]
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    rows: list[tuple[str, str, str, float]] = []
    for line in output.splitlines():
        fields = line.split("|")
        if len(fields) != 4:
            continue
        try:
            rows.append((fields[0], fields[1], fields[2], float(fields[3])))
        except ValueError:
            continue
    return rows


def ingress_counts() -> list[tuple[str, str, str, float]]:
    """The four ingress series, in one round trip.

    Same rule as `governance_counts`: a query that cannot run returns nothing
    and **no series is pushed**, so the `absent()` half of a rule fires instead
    of a flat zero that looks healthy. A streak that really is zero is a fact
    and is pushed -- and for this metric it is the fact anybody wants.
    """

    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"'
    try:
        output = run(
            ["docker", "exec", CONTAINERS["postgres"], "sh", "-ec", command, "sh", INGRESS_SQL]
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    rows: list[tuple[str, str, str, float]] = []
    for line in output.splitlines():
        fields = line.split("|")
        if len(fields) != 4:
            continue
        try:
            rows.append((fields[0], fields[1], fields[2], float(fields[3])))
        except ValueError:
            continue
    return rows


def backup_freshness_seconds(now: float) -> float:
    dumps = list(BACKUP_DIR.glob("*.dump"))
    if not dumps:
        return 9_999_999.0
    return max(0.0, now - max(path.stat().st_mtime for path in dumps))


def point(
    value: float,
    timestamp_ns: str,
    attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"timeUnixNano": timestamp_ns, "asDouble": value}
    if attributes:
        result["attributes"] = [
            {"key": key, "value": {"stringValue": item}} for key, item in attributes.items()
        ]
    return result


def gauge(
    name: str,
    value: float,
    timestamp_ns: str,
    attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {"name": name, "gauge": {"dataPoints": [point(value, timestamp_ns, attributes)]}}


def labelled_sum(
    name: str, value: float, timestamp_ns: str, attributes: dict[str, str]
) -> dict[str, Any]:
    """A cumulative counter that carries labels.

    Separate from `cumulative_sum` rather than an optional argument: every
    existing caller is unlabelled and should stay that way, and a label added by
    accident to one of them would multiply series in a metric a rule is already
    built on.
    """

    return {
        "name": name,
        "sum": {
            "aggregationTemporality": 2,
            "isMonotonic": True,
            "dataPoints": [point(value, timestamp_ns, attributes)],
        },
    }


def cumulative_sum(name: str, value: float, timestamp_ns: str) -> dict[str, Any]:
    return {
        "name": name,
        "sum": {
            "aggregationTemporality": 2,
            "isMonotonic": True,
            "dataPoints": [point(value, timestamp_ns)],
        },
    }


def collect(now: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    timestamp_ns = str(int(now * 1_000_000_000))
    health = {component: container_healthy(name) for component, name in CONTAINERS.items()}
    health["api"] *= http_healthy("http://127.0.0.1:18080/healthz")
    health["ingress"] *= http_healthy("http://127.0.0.1:18081/healthz")
    health["observer_drill"] = 0.0 if DRILL_MARKER.exists() else 1.0
    depth, lag, pending = queue_depth()
    no_send, ceiling_counts = database_counts()
    unreconciled = abandoned_reservation_count()
    freshness = backup_freshness_seconds(now)
    balance_difference, balance_age, balance_comparison_age = balance_reconciliation()
    governance = governance_counts()
    ingress = ingress_counts()

    metrics = [
        gauge("fastagent_v2_stage_service_healthy", value, timestamp_ns, {"component": component})
        for component, value in health.items()
    ]
    metrics.extend(
        [
            gauge("fastagent_v2_stage_queue_depth", depth, timestamp_ns),
            gauge("fastagent_v2_stage_queue_lag", lag, timestamp_ns),
            gauge("fastagent_v2_stage_queue_pending", pending, timestamp_ns),
            cumulative_sum("fastagent_v2_stage_no_send_total", no_send, timestamp_ns),
            gauge("fastagent_v2_stage_backup_freshness_seconds", freshness, timestamp_ns),
        ]
    )
    metrics.extend(
        [
            cumulative_sum(metric, ceiling_counts[reason], timestamp_ns)
            for reason, metric in CEILING_REASON_METRICS.items()
        ]
    )
    # **The six governance metrics** (ADR-0101 §三.2 plus the ninth added by the
    # user on 2026-09-07). The other three named there --
    # `fastagent_v2_stage_proactive_delivered_total`,
    # `..._proactive_suppressed_total`, `..._rate_limited_total` -- have no
    # source yet: the proactivity body is M1b B4 and the rate gate is B1-6.
    # **They arrive with their features rather than as flat zeroes**, which is
    # the same rule as above: a series with no data must be absent, not calm.
    no_send_by_reason: dict[str, float] = {}
    for kind, label_a, label_b, value in governance:
        if kind == "no_send":
            # **Fold before emitting, not while emitting.** Two reason codes
            # that nobody named above -- or one of them plus the `NULL` group,
            # which arrives as an empty field -- both land in `other`. Appending
            # inside the loop would then put two data points carrying the same
            # name, the same attributes and the same timestamp into one payload,
            # which is a duplicate sample: the write endpoint rejects **the
            # batch**, not the offending point. One unnamed reason code would
            # take all twenty series off the dashboard on the day it first
            # fires, and the only symptom would be that the graphs stop.
            reason = label_a if label_a in NO_SEND_REASON_CODES else "other"
            no_send_by_reason[reason] = no_send_by_reason.get(reason, 0.0) + value
        elif kind == "abandoned":
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_root_frame_abandoned_total",
                    value,
                    timestamp_ns,
                    {"reason": label_a},
                )
            )
        elif kind == "exhausted":
            metrics.append(
                cumulative_sum(
                    "fastagent_v2_stage_delivery_attempts_exhausted_total", value, timestamp_ns
                )
            )
        elif kind == "delivery_unknown":
            metrics.append(
                cumulative_sum("fastagent_v2_stage_delivery_unknown_total", value, timestamp_ns)
            )
        elif kind == "unkept_promise_refusal":
            # **每一个都是一句「我这就给您 X」而拿不出 X**（批 F）。
            #
            # 平台丢掉了那句许诺，而这条消息除了它什么都没有，于是这一轮沉默。
            # **累计量**，与下面那条逐字同一个理由：它记的是
            # 「这件事发生过几次」，回落会把「曾经沉默过」抹掉。
            metrics.append(
                cumulative_sum(
                    "fastagent_v2_stage_unkept_promise_refusal_total", value, timestamp_ns
                )
            )
        elif kind == "unkept_promise_dropped":
            # **兜底响了几次**（批 G）。上面那条只数整份拒 —— 而常见形态是
            # 丢掉那一句、其余照发，客户**收到了**消息，只是少了一句许诺。
            #
            # ⚠️ **它涨不一定是坏消息，而它不动才是要紧的那个信号**：
            # 批 G 往两处提示词里加了「许了就把 X 放进下一条 `send_message`」，
            # **这个数是判断那句话有没有用的唯一读数**。
            # 它下不来，就是模型没照做 —— 那时该考虑的是 T367（重生成）。
            #
            # **累计量**，与上面那条同一个理由：回落会把「曾经丢过」抹掉。
            metrics.append(
                cumulative_sum(
                    "fastagent_v2_stage_unkept_promise_dropped_total", value, timestamp_ns
                )
            )
        elif kind == "unfounded_fact_refusal":
            # **每一个都是一个说了话而没收到回复的客户**（复核 2026-09-18）。
            #
            # 平台拦下的是一句「您付的那 1888」——**替客户认下一笔他没说过的支出**。
            # 拦对了，而客户那一轮什么都没收到。
            #
            # **累计量而不是瞬时值**：它记的是「这件事发生过几次」。
            # 这个数涨，说明提示词或配包话术要改；**它不会回落，而那是对的** ——
            # 回落会把「曾经沉默过」抹掉。
            metrics.append(
                cumulative_sum(
                    "fastagent_v2_stage_unfounded_customer_fact_refusals_total",
                    value,
                    timestamp_ns,
                )
            )
        elif kind == "manager_notice_undeliverable":
            # **交接发生了，而没人被通知**（复核 2026-09-17 §2.3 实测：
            # `tenant_manager_contacts` 0 条，两条 `manual_task` 挂在
            # `PROPOSED`，会话停在人接管、`last_outbound_at` 是 `null`）。
            #
            # **累计量而不是瞬时值**：它记的是「这件事发生过几次」，
            # 而每一次都是一个本该有人接手的客户。配上联系人之后
            # 这个数不会回落，**而那是对的** —— 回落会把「曾经漏过」抹掉。
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_manager_notice_undeliverable_total",
                    value,
                    timestamp_ns,
                    {"reason": label_a},
                )
            )
        elif kind == "manual_open":
            metrics.append(
                gauge(
                    "fastagent_v2_stage_manual_task_open_total",
                    value,
                    timestamp_ns,
                    {"class": label_a, "notify": label_b},
                )
            )
        elif kind == "rate_limited":
            # **B1-6.** The label is the scope that bit (`conversation` /
            # `tenant`), which is what ADR-0101 §三.2 names -- "we are over" is
            # not actionable, "this conversation is over while the tenant is
            # fine" is.
            #
            # **It comes from `evidence->>'scope'`, not from `reason_code`**
            # (independent review 2026-09-08, P2-3). The first version put the
            # scope in `reason_code`, which made the same event answer to two
            # names across two tables, and put a non-reason into a column the
            # `exhausted` branch above filters on **without** an action filter.
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_rate_limited_total",
                    value,
                    timestamp_ns,
                    {"scope": label_a},
                )
            )
        elif kind == "scheduler_lag":
            metrics.append(
                gauge("fastagent_v2_stage_scheduler_lag_seconds", value, timestamp_ns)
            )
    metrics.extend(
        labelled_sum(
            "fastagent_v2_stage_plan_no_send_total", total, timestamp_ns, {"reason_code": reason}
        )
        for reason, total in sorted(no_send_by_reason.items())
    )
    # **B3-4b.** The gauge is the one that guards the irreversible outcome; the
    # two counters exist so that whoever the gauge wakes can see what the
    # failures were. Labels are bounded by construction: `config_id` reaches
    # here only for bindings that resolved (see `IngressOutcomeError`), and the
    # reasons are the bounded tuples in `adapter.py` (three of them since
    # `ingress.answered` got a series of its own).
    for kind, label_a, _label_b, value in ingress:
        if kind == "ingress_streak":
            metrics.append(
                gauge(
                    "fastagent_v2_stage_ingress_consecutive_failures",
                    value,
                    timestamp_ns,
                    {"config_id": label_a},
                )
            )
        elif kind == "ingress_non_2xx":
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_ingress_non_2xx_total",
                    value,
                    timestamp_ns,
                    {"reason": label_a},
                )
            )
        elif kind == "ingress_refused":
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_ingress_refused_total",
                    value,
                    timestamp_ns,
                    {"reason": label_a},
                )
            )
        elif kind == "ingress_answered":
            # Same shape as the two above, and the label is bounded by the same
            # rule: `INGRESS_ANSWERED_REASONS` names every code that may reach
            # here, and `test_ingress_answer_vocabulary` is what makes that
            # true rather than merely written down.
            metrics.append(
                labelled_sum(
                    "fastagent_v2_stage_ingress_answered_total",
                    value,
                    timestamp_ns,
                    {"reason": label_a},
                )
            )
    metrics.append(
        cumulative_sum(UNRECONCILED_METRIC, unreconciled, timestamp_ns)
    )
    metrics.extend(
        [
            gauge(BALANCE_AGE_METRIC, balance_age, timestamp_ns),
            gauge(BALANCE_COMPARISON_AGE_METRIC, balance_comparison_age, timestamp_ns),
        ]
    )
    if balance_difference is not None:
        # Absent, not zero, when there is nothing to compare. See the constant.
        metrics.append(gauge(BALANCE_DIFFERENCE_METRIC, balance_difference, timestamp_ns))
    summary = {
        "collected_at": int(now),
        "health": health,
        "queue_depth": depth,
        "queue_lag": lag,
        "queue_pending": pending,
        "no_send_total": no_send,
        "backup_freshness_seconds": freshness,
        "ceiling_counts": dict(ceiling_counts),
        "unreconciled_total": unreconciled,
        "balance_difference_cny": balance_difference,
        "balance_snapshot_age_seconds": balance_age,
        "balance_comparison_age_seconds": balance_comparison_age,
    }
    return metrics, summary


def source_host() -> str:
    """Which machine this push came from. **Never empty** -- see T219.

    Without this, every machine running this collector emits series that are
    *identically named*, so Grafana cannot tell one from another. That is not a
    hypothetical: ADR-0046 §2.3 records a panel that described the wrong Stage
    while two of them coexisted, and the 2026-09-08 migration avoided a repeat
    only by keeping one pusher alive at a time (94 seconds between stopping the
    old and starting the new) -- a discipline with nothing guarding it.

    `FASTAGENT_STAGE_HOST_ID` wins when set, so an operator can name a host
    something stable and meaningful. Otherwise the kernel hostname, which is
    always available. The fallback is the point: an identifier that can go
    missing when a variable is unset would fail open, and a *silently absent*
    source label is exactly the defect being closed here.
    """
    configured = os.environ.get("FASTAGENT_STAGE_HOST_ID", "").strip()
    if configured:
        return configured
    return socket.gethostname().strip() or "unknown"


def push(metrics: list[dict[str, Any]], env: dict[str, str]) -> int:
    endpoint = env["GRAFANA_CLOUD_OTLP_ENDPOINT"].rstrip("/") + "/v1/metrics"
    user = env["GRAFANA_CLOUD_OTLP_INSTANCE_ID"]
    token = env["GRAFANA_CLOUD_ACCESS_POLICY_TOKEN"]
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "fastagent-v2-stage-observer"},
                        },
                        {"key": "deployment.environment.name", "value": {"stringValue": "stage"}},
                        # T219. Resource-level, not per-point: it describes the
                        # emitter, and putting it on the points would multiply
                        # it across every series for no gain.
                        {"key": "host.name", "value": {"stringValue": source_host()}},
                    ]
                },
                "scopeMetrics": [
                    {"scope": {"name": "fastagent.stage.observer"}, "metrics": metrics}
                ],
            }
        ]
    }
    request = urllib.request.Request(  # noqa: S310 - configured HTTPS endpoint
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return int(response.status)


def main() -> int:
    try:
        env = load_env(ENV_FILE)
        metrics, summary = collect(time.time())
        status = push(metrics, env)
        summary["otlp_http_status"] = status
        STATE_FILE.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(STATE_FILE, 0o640)
        return 0 if 200 <= status < 300 else 1
    except Exception as error:  # noqa: BLE001 - cron boundary must produce one concise error
        print(f"stage-observability: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
