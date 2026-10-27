"""一次生成，**一次判定**，多行出站（ADR-0152，运维缺陷 14）。

## 它更正的是 ADR-0151 的形状

那一批（`test_one_generation_many_bubbles.py`，本文件的前身）把分段摊在
**策略链之前** —— 于是**每一段各自过了一遍全部 policy**。

用户 2026-09-23 裁定取消那件事：

> 「一次生成应该是一次判定 而不是分段之后还要判定
>   分段前就判断好能不能发 **拆分后语义语境可能完全不同**」

**一个片段单看的判定结论，可能与看整条消息完全不同。**

所以今天：判定读的是**整条**（分段还在提案里），**拆是投递的事** ——
发生在 `_queue_outbox_row` 里，批准之后。

## 触发它的现场（缺陷 14）

一条分了两段的消息，第 0 段投递状态是 `UNKNOWN`：

```
ordinal 0 | pace_ms 800 | UNKNOWN | "不会。"
ordinal 1 | pace_ms 0   | PENDING | "只有您明确说开，我们才会开通；…"
```

`UNKNOWN` 不是 `DELIVERED`，**而它不会自己变成 `DELIVERED`** ——
第 1 段永远停在 `PENDING`，客户只看到「不会。」三个字。

⚠️ **而用户没有选我给的三个选项中的任何一个**，他改了题：
分段不该是三个各自判过的东西排队，**它们是一次判定的几个气泡**。

## 平台仍然不选切点

切点是模型在 `content.segments` 里给的。这一点 ADR-0151 就是对的，
本条一个字没改 —— 变的只是**什么时候拆**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastagent_v2.worker.pipeline import (
    SEGMENT_BASIS_INFIX,
    SEGMENT_ORDINAL_STRIDE,
    SEGMENT_PACE_CEILING_MS,
    SEGMENT_PACE_FLOOR_MS,
    SEGMENT_PACE_MS_PER_CHAR,
    _segment_pace_ms,
    _segments_to_queue,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INFERENCE = REPOSITORY_ROOT / "packages" / "contracts" / "inference"

COMMAND: dict[str, Any] = {
    "audience": "customer",
    "content": {"type": "text", "text": "整条拼起来的样子"},
}


def _text(value: str) -> dict[str, str]:
    return {"type": "text", "content_or_ref": value}


def _proposal(*segments: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "send_message",
        "typed_payload": {"content": {"segments": list(segments)}},
        "evidence_refs": [],
    }


def _queued(*segments: dict[str, str]) -> list[tuple[dict[str, Any], str, int]]:
    return _segments_to_queue(
        COMMAND, proposal=_proposal(*segments), materials=None, basis_key="basis-a"
    )


def test_one_segment_is_left_alone() -> None:
    """**一段就是一段** —— 一行，`pace_ms` 0，命令原样。

    这一条钉的是「存量行为逐字不变」：今天绝大多数回合就是一段。
    """

    queued = _queued(_text("收到什么都不说，直接答他"))

    assert len(queued) == 1
    command, key, pace = queued[0]
    assert key == "basis-a", "单段不该被改键 —— 存量行的幂等键要一个字不变"
    assert pace == 0
    assert command == COMMAND, "单段那条路不该重渲染 —— 原样排进去"


def test_three_segments_become_three_rows_in_order() -> None:
    """三段 → 三行，顺序就是模型给的顺序，每行只装自己那一段。"""

    queued = _queued(_text("第一段"), _text("第二段"), _text("第三段"))

    assert len(queued) == 3
    assert [command["content"]["text"] for command, _key, _pace in queued] == [
        "第一段",
        "第二段",
        "第三段",
    ]


def test_every_row_keeps_the_shape_a_channel_can_act_on() -> None:
    """⚠️ **逐段渲染出来的形状，要与不分段那条路逐字相同。**

    出站行的 `content` 是「平台的规范形状」，不是模型的三种之一
    （`_send_message_content` 那段注释）。分段之后每一行仍然是那一种 ——
    **所以渠道那一侧一个字都不用改**，而这条用例钉住它。
    """

    for command, _key, _pace in _queued(_text("一"), _text("二")):
        assert set(command) == set(COMMAND), "分段行多了或少了顶层键"
        assert command["content"]["type"] == COMMAND["content"]["type"]
        assert isinstance(command["content"]["text"], str)


def test_the_keys_cannot_collide_and_the_first_one_does_not_move() -> None:
    """键由平台按序号铸，**撞不了**；而第一段沿用原键。

    ⚠️ **先钉住真的铺成了三行** —— 没有这一句，「键互不相同」在
    **一行都没铺开**时同样为真（只有一个键）。
    ADR-0151 那一批的反向探针实测到过这个形状。
    """

    keys = [key for _command, key, _pace in _queued(_text("一"), _text("二"), _text("三"))]

    assert len(keys) == 3, f"没铺成三行，下面几句就是空的：{keys}"
    assert keys[0] == "basis-a", "第一段改了键 —— 存量键会整体漂移"
    assert len(set(keys)) == len(keys), f"铸出来的键撞了：{keys}"
    assert all(SEGMENT_BASIS_INFIX in key for key in keys[1:]), (
        "后续段的键没有可查的平台印记"
    )


def test_the_last_segment_leaves_no_pause() -> None:
    """停顿是给「下一段还要来」用的，最后一段之后没有下一段。"""

    paces = [pace for _command, _key, pace in _queued(_text("一"), _text("二"))]

    assert paces[0] > 0, "第一段之后没有停顿 —— 两个气泡会同一瞬间落地"
    assert paces[-1] == 0


def test_the_pause_is_bounded_at_both_ends() -> None:
    """`clamp(字数 × 60ms, 800ms, 4000ms)`，两端都钉住。

    ⚠️ **不是去模拟真实打字速度**（那要三十秒，太慢），
    要的是**两个气泡不要同一瞬间落地**。
    """

    assert _segment_pace_ms(_text("好")) == SEGMENT_PACE_FLOOR_MS
    assert _segment_pace_ms(_text("字" * 10_000)) == SEGMENT_PACE_CEILING_MS

    assert _segment_pace_ms(_text("字" * 40)) == 40 * SEGMENT_PACE_MS_PER_CHAR
    assert SEGMENT_PACE_FLOOR_MS < 40 * SEGMENT_PACE_MS_PER_CHAR < SEGMENT_PACE_CEILING_MS


def test_a_material_ref_segment_takes_the_floor() -> None:
    """素材引用按 ref 长度算不合适（ref 短、正文长），所以取下限。"""

    ref = {"type": "material_ref", "content_or_ref": "material:demo-price-plain:v1"}
    assert _segment_pace_ms(ref) == SEGMENT_PACE_FLOOR_MS


def test_a_payload_without_segments_passes_through() -> None:
    """⚠️ **反侧：没有模型给的切点时，平台不发明一个。**

    没有这一条，一个「一律拆」的实现会让上面几条绿 ——
    而 `{"text": ...}` 那种形状里根本没有切点。
    """

    for payload in (
        {"kind": "send_message", "typed_payload": {"content": {"text": "一整句话"}}},
        {"kind": "send_message", "typed_payload": {}},
        None,
    ):
        queued = _segments_to_queue(
            COMMAND, proposal=payload, materials=None, basis_key="basis-a"
        )
        assert queued == [(COMMAND, "basis-a", 0)], f"这个形状被拆了：{payload}"


def test_the_split_does_not_mutate_what_it_was_given() -> None:
    """拆是造新的，不是改原件 —— 原件还要进审计留痕。"""

    command = {"audience": "customer", "content": {"type": "text", "text": "原样"}}
    proposal = _proposal(_text("一"), _text("二"))
    before = json.dumps(proposal, ensure_ascii=False)

    _segments_to_queue(command, proposal=proposal, materials=None, basis_key="b")

    assert json.dumps(proposal, ensure_ascii=False) == before
    assert command["content"]["text"] == "原样"


def test_the_ordinal_stride_clears_both_contract_bounds() -> None:
    """⚠️ **步长必须大于任何一条消息的段数上限，否则两条提案的段会撞号。**

    `ordinal` 是「提案位置 × 步长 + 段位置」，而迁移 `076` 的唯一约束
    `(tenant_id, intent_id, ordinal)` 靠它区分同一次判定的几行。

    **这条用例现读那两个契约上界** —— 它们一变，这条就红并指着这个常量。
    """

    generation = json.loads(
        (INFERENCE / "generation-result.schema.json").read_text(encoding="utf-8")
    )
    plan = json.loads((INFERENCE / "plan-result.schema.json").read_text(encoding="utf-8"))

    max_segments = int(generation["properties"]["customer_visible_segments"]["maxItems"])
    max_candidates = int(plan["properties"]["candidate_intents"]["maxItems"])

    assert SEGMENT_ORDINAL_STRIDE > max_segments, (
        f"步长 {SEGMENT_ORDINAL_STRIDE} 不大于一条消息的段数上限 {max_segments} —— "
        "两条提案的段会撞上同一个 ordinal，而 076 的唯一约束会拒掉第二行"
    )
    # 而序号本身也不能溢出得离谱：最坏是「最后一条候选的最后一段」。
    assert (max_candidates - 1) * SEGMENT_ORDINAL_STRIDE + max_segments < 10_000


def test_the_split_happens_after_the_policy_chain_not_before() -> None:
    """⚠️ **这一条才是本批的兑现物，而它只能读顺序。**

    「判定读到的是整条还是一段」在单元层面看不出来 —— 两种写法
    `_segments_to_queue` 的行为一模一样。**区别在于它被谁调用、在谁之后。**

    ADR-0151：`_expand_segments(proposals, ...)` → `_decide_proposal(每一段)`。
    ADR-0152：`_decide_proposal(整条提案)` → `_queue_outbox_row` 里才拆。

    用户 2026-09-23：「**分段前就判断好能不能发，拆分后语义语境可能完全不同**」。

    ⚠️ **源码扫描看不见代码跑没跑**（本仓记法），所以这一条**不声称**
    「判定真的读到了整条」—— 它声称的是更窄、而正是这次出事的那一件：
    **拆的那个函数只被排出站行的地方调用，不被判定那条路调用。**
    """

    source = (
        REPOSITORY_ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
    ).read_text(encoding="utf-8")

    # ⚠️ **查的是「定义」，不是「名字出现」** —— 第一版写成后者，
    # 而它命中的是**留痕里引它的那句话**（「那一版叫 `_expand_segments`」）。
    # 同一个错今天在另外两份用例里也犯过：
    # 一个只问「某几个字在不在」的判据，分不出「它还在」与「在说它没了」。
    assert "def _expand_segments(" not in source, (
        "ADR-0151 那个「判定之前摊开」的函数还在 —— 它与本条的形状互斥"
    )

    callers = [
        line.strip()
        for line in source.splitlines()
        if "_segments_to_queue(" in line and not line.lstrip().startswith("def ")
    ]
    assert len(callers) == 1, f"拆被调用了 {len(callers)} 处：{callers}"

    # 那唯一一处必须在 `_queue_outbox_row` 里 —— 排出站行的地方。
    queue_row = source.index("    def _queue_outbox_row(")
    next_def = source.index("    def ", queue_row + 10)
    assert queue_row < source.index("_segments_to_queue(", queue_row) < next_def, (
        "拆不在 `_queue_outbox_row` 里 —— 那它就不是「批准之后」了"
    )

    # 而判定那条路上不许出现它。
    decide = source.index("    def _decide_proposal(")
    decide_end = source.index("    @staticmethod", decide)
    assert "_segments_to_queue" not in source[decide:decide_end], (
        "判定那条路上出现了拆 —— 那就是「分段之后还要判定」"
    )
