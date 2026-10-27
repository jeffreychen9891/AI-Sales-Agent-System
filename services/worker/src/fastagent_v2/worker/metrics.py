"""In-process counters, shaped for the self-hosted Prometheus stack (ADR-0014).

These are deliberately plain: a dict of labelled counts with a snapshot method.
The observability stack is not deployed yet, so exporting is a later step; what
matters now is that the numbers are being kept from the first call rather than
reconstructed afterwards from logs that were never written.

Label values are bounded by construction — provider, model, role and a small
enum of repair kinds. No conversation or tenant identifiers: those are unbounded
and belong in the ledger, not in metric labels.
"""

from __future__ import annotations

import threading
from collections import defaultdict


class Counter:
    """A labelled monotonic counter."""

    def __init__(self, name: str, *, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.label_names = label_names
        self._values: dict[tuple[str, ...], int] = defaultdict(int)
        self._lock = threading.Lock()

    def increment(self, *label_values: str, amount: int = 1) -> None:
        if len(label_values) != len(self.label_names):
            raise ValueError(
                f"{self.name} expects labels {self.label_names}, got {label_values}"
            )
        with self._lock:
            self._values[label_values] += amount

    def value(self, *label_values: str) -> int:
        with self._lock:
            return self._values.get(label_values, 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._values.values())

    def snapshot(self) -> dict[tuple[str, ...], int]:
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        """Only for tests. Production counters are monotonic for the process."""

        with self._lock:
            self._values.clear()


#: Times a model response needed structural repair before it could be parsed.
#:
#: Under a strict `response_format` this should trend to zero. A persistently
#: non-zero rate is the signal that the binding, the prompt or the provider has
#: changed underneath us -- which is exactly why the repair is counted rather
#: than performed silently.
SCHEMA_REPAIR_TOTAL = Counter(
    "schema_repair_total", label_names=("provider", "model", "role", "repair")
)

#: Model responses that could not be parsed even after repair.
SCHEMA_UNREPAIRABLE_TOTAL = Counter(
    "schema_unrepairable_total", label_names=("provider", "model", "role")
)


def repair_report() -> dict[str, object]:
    """Compact view for the daily sync report."""

    repairs = SCHEMA_REPAIR_TOTAL.snapshot()
    return {
        "schema_repair_total": SCHEMA_REPAIR_TOTAL.total(),
        "schema_unrepairable_total": SCHEMA_UNREPAIRABLE_TOTAL.total(),
        "by_label": {"/".join(labels): count for labels, count in sorted(repairs.items())},
    }


#: 因为超出 `UNDERSTANDING_PROMPT_BYTES` 而被丢掉的 understanding 条目数，按字段。
#:
#: **它是 ADR-0132 §三「丢了必须留痕」那一半。** 没有它，
#: 「模型只给了 2 条事实」和「模型给了 40 条而平台丢了 38 条」
#: **在数据里长得一模一样** —— 而这两件事该修的地方不同：
#: 前者修提示词，后者修这个上界或那张预算表。
#:
#: 标签是字段名，取值来自 `understanding_bound._DROP_ORDER` 那五个，**有界**。
#: **`stage` 不能省**（独立审查 2026-09-12，P2-3）：plan 与 generate 各过一次
#: 上界，而 `pipeline._understanding_for_plan` 只替换 `interruption.relation`
#: 一个短字符串 —— 两次输入几乎同样大、丢的条数相同。**不分标签的话这个计数器
#: 读到的是真实丢弃数的两倍**，而那道算术题正是它存在的全部理由。
#: 检索的审计行写不进去的次数，按 `outcome`。
#:
#: **它是「不把回合带走」那条承诺的收尾**（独立审查 2026-09-12，P2-1）：
#: `KnowledgeRetriever.run` 在审计行写失败时**不再往外抛** ——
#: 让客户这一轮收不到消息不是「写不下一行审计」该有的代价。
#: 但那一行确实丢了，而裁定六的另一半正是那行审计，
#: **所以它必须在别处留下一个数**，否则「没有行」会被读成「没请求检索」。
#:
#: `metrics.py` 顶部那句仍然适用：观测栈还没部署，导出是后一步（T302 同源）。
RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL = Counter(
    "fastagent_retrieval_evidence_write_failed_total", label_names=("outcome",)
)


#: 生成之后那次审计 `UPDATE` 写不进去的次数（裁定四那四列 + `hits_sent`）。
#:
#: **它与上面那个不是同一件事，而两者错的方向一样**：上面那个是「整行没写下来」，
#: 于是「没有行」被读成「没请求检索」；这一个是**行在、而判定那几列空着**，
#: 于是它被读成「这一轮没走到生成」（`050` 给 `NULL` 定的含义）。
#: 同一族的误读，只是深了一层。
#:
#: **为什么它只能是一个计数器**：这一刻三次模型调用的钱已经花完、生成也过了校验，
#: 而客户那条回复已经成文。**为几列审计数据把它丢掉不是一个可接受的代价**
#: （独立审查 2026-09-12 P1-1；6b 在 `RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL`
#: 那一处已经把这条口径立好了，本条是把它带到 6c）。
RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL = Counter(
    "fastagent_retrieval_verdict_write_failed_total", label_names=("quoted_side",)
)


UNDERSTANDING_ENTRIES_DROPPED_TOTAL = Counter(
    "understanding_entries_dropped_total",
    label_names=("stage", "field"),
)


#: 静默那一步开算时，锚点是空的次数（运维缺陷 20，2026-09-23）。
#:
#: ## 为什么这里要一个数，而不是把兜底改掉
#:
#: `_seconds_since` 读不出来回 `0.0`，**而那个方向选得对**：模型因此倾向
#: `wait`，而不是倾向再发一条。运维的原话是「这个 fail-open 方向选得对」。
#:
#: ⚠️ **而它也正是那个 bug 一直没被发现的原因**：输出永远看起来合理
#: （总是 `wait`），**因为输入永远是同一个值**。缺陷 20 就这样活了下来 ——
#: 直到有人问了一句「他刚开口、零秒未答，怎么会是 0 秒？」
#:
#: 所以兜底留着，**而「说不上来」要与「刚刚」分开记**：
#: 这个数一涨，就说明有一轮静默判定是从一个空锚点推出来的。
#:
#: **锚点为空今天只有一种正当含义**：这通对话一条出站都没发过。
#: 在那之外它非零，就是又一次「写的是一份、读的是另一份」。
SILENCE_ANCHOR_MISSING_TOTAL = Counter(
    "silence_anchor_missing_total",
    label_names=("anchor",),
)
