"""写的是列、读的是 JSONB —— 于是静默那一步永远看到「0 秒」（运维缺陷 20）。

## 现场

```
08:56:55  客户最后一句
08:57:08  我们最后一条出站            ← 列 last_outbound_at 写在这一刻
08:58:41  静默判定                    ← 模型收到的 silent_for_seconds = 0.0
```

**过去了 93 秒。** 而那一步的全部意义是「主动打破静默之前，先问一句
他为什么不说话」——**「他静默了多久」恒为 0，它永远看不到一次长静默**。

## 一个事实两份

| | 谁写 | 实测 |
|---|---|---|
| **列** `conversation_aggregates.last_outbound_at` | 投递后那句 `UPDATE … = now()` | 有值 |
| **JSONB** `state.last_outbound_at` | 建会话时置 `None`，**此后零写点** | 恒 `None` |

`git grep '"last_outbound_at"\\] *=' services/worker/` 在修复前是 **0 命中**。

## ⚠️ 兜底方向是对的，而它正是这个 bug 的保护色

`_seconds_since` 读不出就回 `0.0`，模型因此倾向 `wait` 而不是倾向再发一条。
运维复核时点名说这个方向选得对。**而输出因此永远看起来合理 ——
因为输入永远是同一个值。** 所以本批不改兜底，改的是它的沉默：
「说不上来」与「刚刚」分开记（`SILENCE_ANCHOR_MISSING_TOTAL`）。

## 真正会红的那一条不在这个文件里

「读的是列还是 JSONB」只有连库跑一遍答得了 ——
`tests/e2e/test_the_silence_question_runs_on_a_real_turn.py` 里那条
`..._is_told_how_long_the_silence_actually_was` 把列往回拨 600 秒再问。
**本文件守的是它周围那几件纯函数的事**，它们各自独立、且不需要库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastagent_v2.worker.metrics import SILENCE_ANCHOR_MISSING_TOTAL
from fastagent_v2.worker.pipeline import _iso_or_none, _seconds_since

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPOSITORY_ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"


def test_a_column_timestamp_becomes_a_contract_shaped_string() -> None:
    """列里是 `datetime`，聚合契约那一格要的是 `format: date-time` 的字符串。

    ⚠️ **塞 `datetime` 进 JSONB 会在序列化那一刻才炸**，而那时离刷新那一句
    已经隔了半条管线 —— 报出来的位置指不到真正的原因。
    """

    moment = datetime(2026, 9, 23, 8, 57, 8, tzinfo=UTC)

    assert _iso_or_none(moment) == "2026-09-23T08:57:08Z"
    assert isinstance(_iso_or_none(moment), str)


def test_an_empty_column_stays_empty() -> None:
    """**空不是一次失败**：这一列为空只有一种意思 —— 这通对话一条出站都没发过。

    契约那一格是 `["string", "null"]`，所以 `None` 是一个合法的答案。
    把它变成别的（比如「现在」）会让一通从没发过消息的对话
    看起来刚刚发过一条。
    """

    assert _iso_or_none(None) is None


def test_the_fallback_still_opens_the_safe_way() -> None:
    """⚠️ **兜底一个字没改，而这一条钉住它没被顺手改掉。**

    读不出来回 `0.0`，模型因此倾向 `wait`。运维复核原话：
    「这个 fail-open 方向选得对」。本批改的是它的**沉默**，不是它的方向。
    """

    assert _seconds_since(None) == 0.0
    assert _seconds_since("") == 0.0
    assert _seconds_since("这不是一个时间") == 0.0


def test_a_moment_in_the_past_reads_as_time_having_passed() -> None:
    """**反侧**：给得出锚点时它真的算出了秒数。

    没有这一条，一个恒回 `0.0` 的实现会让上面那条绿 ——
    而恒回 `0.0` 正是缺陷 20 的症状本身。
    """

    long_ago = datetime(2020, 1, 1, tzinfo=UTC)

    assert _seconds_since(long_ago) > 600
    assert _seconds_since(long_ago.isoformat()) > 600


def test_a_missing_anchor_leaves_a_number_behind() -> None:
    """**「说不上来」要与「刚刚」分得开。**

    先前 `0.0` 同时表示这两件事，**而那正是缺陷 20 活下来的方式**：
    输出永远合理（总是 `wait`），因为输入永远是同一个值。
    """

    from fastagent_v2.worker.pipeline import InboxProcessor

    before = SILENCE_ANCHOR_MISSING_TOTAL.value("last_outbound_at")

    class _Prepared:
        aggregate: dict[str, object] = {}

    assert InboxProcessor._silent_for_seconds(_Prepared()) == 0.0  # type: ignore[arg-type]
    assert SILENCE_ANCHOR_MISSING_TOTAL.value("last_outbound_at") == before + 1

    class _WithAnchor:
        aggregate: dict[str, object] = {"last_outbound_at": "2020-01-01T00:00:00Z"}

    assert InboxProcessor._silent_for_seconds(_WithAnchor()) > 600  # type: ignore[arg-type]
    assert SILENCE_ANCHOR_MISSING_TOTAL.value("last_outbound_at") == before + 1, (
        "锚点给得出时也记了一笔 —— 那个数会被读成「又有一轮从空锚点推出来了」"
    )


def test_the_other_anchor_does_not_have_the_same_fork() -> None:
    """⚠️ **`last_inbound_at` 没有这个分叉，而方向正好相反** —— 运维点名要核的那一条。

    它在 JSONB 里写（`aggregate["last_inbound_at"] = envelope["occurred_at"]`），
    再随那一次 `UPDATE` 落到列。所以**权威的那份是 JSONB**，
    与 `last_outbound_at` 恰好反过来。

    这一条不主张两者该统一成一个方向 —— 那是一次裁定，不是一条用例的事。
    它主张的是更窄的那件：**`last_inbound_at` 今天有 JSONB 写点，而那正是
    `last_outbound_at` 缺的东西**。哪天它也没了，这一条会红。
    """

    source = PIPELINE.read_text(encoding="utf-8")

    inbound_writes = [
        line.strip()
        for line in source.splitlines()
        if '["last_inbound_at"] =' in line or '["last_inbound_at"]=' in line
    ]
    assert inbound_writes, (
        "`last_inbound_at` 的 JSONB 写点没了 —— 它会变成第二个缺陷 20："
        "列有值而管线读到空"
    )
