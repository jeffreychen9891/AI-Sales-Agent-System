"""Check a profile package's material dimensions before it is sealed.

**Why this exists as a script rather than only as admission rules**: the content
window authors 164 segments of dimensions and finds out whether they fit at
sealing time, which is the end. This runs on a working copy, prints the
remaining head-room per bucket, and names the segment that would tip a bucket
over -- so「交了才发现超」stops being the way this is discovered.

It is deliberately a *reporter*, not a second implementation: every rule it
prints is one `profiles/registry.py` already enforces, and the byte arithmetic
imports `runtime.prompt_bytes.budgeted_entry_cost` / `MATERIAL_BYTES_BUDGET`
from the same modules admission uses. A number here that disagrees with
admission is a bug in this script.

**It had its own copy of the byte arithmetic until 2026-09-11** -- the same
`PER_MATERIAL_OVERHEAD_BYTES + len(utf8)` line admission used, written out a
second time. When admission's copy was corrected, this one would have gone on
telling the content window how much room it had using the old, wrong unit:
a reporter that disagrees with the gate is worse than no reporter, because it
is believed.

Usage:
    python scripts/check_material_dimensions.py profiles/internal-demo-sales
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

#: **这个脚本整篇用中文打印，而 Windows 控制台默认是 cp1252。**
#: 不带 `PYTHONIOENCODING=utf-8` 跑它，**第一句中文就 `UnicodeEncodeError`，
#: stdout 是 0 字节、退出码 1** —— 而它是内容窗口手工跑的工具，
#: 那个失败看起来像「脚本挂了」，不像「少了一个环境变量」。
#:
#: **2026-09-15 实测发现它的是一条用例**：新加的
#: `test_the_reporter_prints_the_same_numbers_the_gate_computes` 用子进程跑本脚本，
#: 本地跑绿（我的 shell 里 export 过那个变量），**进了全量门禁当场红** ——
#: 子进程继承的是门禁的环境，那里没有它。
#: **所以修的是脚本自己，不是那条用例的环境** ——
#: 一个要靠调用方设环境变量才能打印的工具，下一个手工跑它的人同样会撞上。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api/src"))
sys.path.insert(0, str(ROOT / "services/worker/src"))
sys.path.insert(0, str(ROOT / "packages/runtime/src"))

from fastagent_v2.profiles.registry import (  # noqa: E402
    MATERIAL_STAGES_KEY,
    MATERIAL_TEXTS_KEY,
)
from fastagent_v2.runtime.prompt_bytes import budgeted_entry_cost  # noqa: E402
from fastagent_v2.worker.material_selection import MATERIAL_BYTES_BUDGET  # noqa: E402

#: 读 profile 声明的上限，不再是这里的一个常量。
#:
#: **原来这里写死 3，理由是「已覆盖：X、Y；当前：Z」那句话正好装三个**——
#: 独立审查 2026-09-11 P1-1 指出那是从**举例**里读出来的，而那句话的**指令**是
#: 「可以累积」，范围是八类，匹配又是包含式。实测在架包累到六类就是
#: 6,973 字节 / 预算 6,132。上限现在是 `material_topic_concurrency`，
#: 由 profile 声明、由 admission 强制；**不声明就按全部桶算**。


def _load(package: Path) -> tuple[dict[str, Any], dict[str, str]]:
    document = json.loads((package / "profile.json").read_text(encoding="utf-8"))
    texts = document.get(MATERIAL_TEXTS_KEY) or {}
    sibling = package / "materials.json"
    if not texts and sibling.exists():
        loaded = json.loads(sibling.read_text(encoding="utf-8"))
        texts = loaded.get(MATERIAL_TEXTS_KEY) or loaded.get("materials") or loaded
    return document, texts


def _cost(ref: str, texts: dict[str, str]) -> int:
    """同一个算法，**不再在这里重写一遍**。

    先前这里是 `PER_MATERIAL_OVERHEAD_BYTES + len(text.encode("utf-8"))`——
    与 `registry._check_stage_budget` 里那一行**逐字相同的第二份拷贝**。
    那一行改对了而这一行没改，内容窗口就会继续照着旧数切素材，
    并且看到一个与封版结果不一致的「还剩多少」。
    """

    return budgeted_entry_cost(ref, str(texts.get(ref, "")))


def _declarations(frame: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in frame.get("content_materials") or []:
        out.append({"ref": item} if isinstance(item, str) else dict(item))
    return out


def _unreachable_stages(frame: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Stages a later stage permanently shadows. See registry.py for the proof."""
    stages = [s for s in frame.get(MATERIAL_STAGES_KEY) or [] if isinstance(s, dict)]
    found = []
    for index, stage in enumerate(stages):
        after = {str(r) for r in (stage.get("after") or ())}
        shadow = [
            str(later.get("name"))
            for later in stages[index + 1 :]
            if {str(r) for r in (later.get("after") or ())} <= after
        ]
        if shadow:
            found.append((str(stage.get("name")), shadow))
    return found


def _report_frame(frame: dict[str, Any], texts: dict[str, str]) -> bool:
    frame_id = str(frame.get("id"))
    print(f"=== frame {frame_id!r} ===")
    ok = True

    for name, shadow in _unreachable_stages(frame):
        ok = False
        print(f"  [FAIL] 阶段 {name!r} 永远到不了：{shadow} 的 after 是它的子集")

    stage_bytes: dict[str, int] = defaultdict(int)
    topic_bytes: dict[str, int] = defaultdict(int)
    ungated = 0
    every_turn = 0
    both_sides = []

    for declaration in _declarations(frame):
        ref = str(declaration.get("ref"))
        cost = _cost(ref, texts)
        availability = declaration.get("availability")
        stages = declaration.get("stages") or ()
        topics = declaration.get("topics") or ()
        if stages and topics:
            both_sides.append(ref)
        if availability is None:
            every_turn += cost
        elif availability == "proactive":
            if stages:
                for name in stages:
                    stage_bytes[str(name)] += cost
            else:
                every_turn += cost
        elif availability == "on_request":
            if topics:
                for topic in topics:
                    topic_bytes[str(topic)] += cost
            else:
                ungated += cost

    for ref in both_sides:
        ok = False
        print(f"  [FAIL] {ref} 同时写了 stages 与 topics —— 每一段只能选一边")

    if every_turn:
        print(
            "  每回合无条件到场（没有 availability，或 proactive 但不点名阶段）："
            f"{every_turn:,} 字节"
        )
        if not stage_bytes and every_turn + ungated > MATERIAL_BYTES_BUDGET:
            ok = False
            print(
                "  [FAIL] 这个帧一个阶段都没声明，全量 "
                f"{every_turn + ungated:,} > {MATERIAL_BYTES_BUDGET:,}"
            )

    declared = frame.get("material_topic_concurrency")
    concurrency = len(topic_bytes)
    if isinstance(declared, int) and not isinstance(declared, bool):
        concurrency = max(0, min(concurrency, declared))
    worst_topics = sorted(topic_bytes.values(), reverse=True)[:concurrency]
    reserve = sum(worst_topics) + ungated
    print(f"  兜底（on_request 无 topics）：{ungated:,} 字节")
    source = "声明" if declared is not None else "未声明，按全部桶算"
    print(
        f"  topic 侧最坏同时命中 {concurrency} 桶（{source}）："
        f"{sum(worst_topics):,} 字节"
    )
    print(
        f"  => 每个串行子桶的可用额度：{MATERIAL_BYTES_BUDGET:,} - {reserve:,}"
        f" = {MATERIAL_BYTES_BUDGET - reserve:,} 字节"
    )

    if topic_bytes and not frame.get("material_topic_slot"):
        ok = False
        print(
            "  [FAIL] 有素材声明了 topics，但本帧没有 material_topic_slot —— "
            "admission 会拒；设成 \"concern_topic\""
        )

    if topic_bytes:
        # **这一行 2026-09-18 改过一次，因为它原来说的话已经是假的。**
        #
        # 原文是「`[孤]` 模型不会把这个词写进槽」，判据是话题名在不在 `goal`
        # 那段散文里，理由写着「模型往槽里写什么由 goal 那句话规定」。
        #
        # **那句话在 2026-09-15「裁 B」之前成立。** 之后不成立了：
        # 词表由 `runtime.topic_vocabulary` 现推，作为
        # `slots_wanted[].allowed_values` **直接渲染进理解步的提示词** ——
        # 那个模块的 docstring 逐字写着「**让第二份不存在**：名字只留在
        # 素材声明里，由引擎渲染进提示词」。
        #
        # **实测（2026-09-18，`internal-demo-sales` 1.2.4）**：十二个话题里
        # **十个**被标 `[孤]`，**其中包括「花钱」** —— 而运维实测里
        # `concern_topic` 从 09-17 06:31 起连着两天就黏在「花钱」上。
        # **一条说「模型不会写这个词」的提示，指着模型写了两天的那个词。**
        #
        # 本脚本自己的 docstring 早写过这条罪：「a reporter that disagrees
        # with the gate is worse than no reporter, **because it is believed**」。
        #
        # 换成今天为真、而且仍然有用的那句：`goal` 点不点名这个话题，
        # 决定的不是「模型会不会写」，是「模型会不会**优先**往那边走」。
        # 这仍然是**报告**不是门禁：平台不拥有商家的词表（schema 明写）。
        goal = str(frame.get("goal") or "")
        print("  topic 桶：")
        for topic, size in sorted(topic_bytes.items(), key=lambda kv: -kv[1]):
            orphan = "" if topic in goal else "  [goal 里没点名，模型仍然收得到这个值]"
            print(f"    {topic:<28} {size:>8,}{orphan}")

    if stage_bytes:
        print("  串行子桶（余量 = 可用额度 - 本桶）：")
        for name, size in sorted(stage_bytes.items(), key=lambda kv: -kv[1]):
            headroom = MATERIAL_BYTES_BUDGET - reserve - size
            if headroom < 0:
                mark = "  [FAIL] 超了"
            elif headroom < budgeted_entry_cost("material:x:v1", ""):
                mark = "  [紧] 装不下一段了"
            else:
                mark = ""
            if headroom < 0:
                ok = False
            print(f"    {name:<28} {size:>8,}  余 {headroom:>8,}{mark}")

    return ok


def _report_request_floors(document: dict[str, Any]) -> None:
    """每个帧在 understand / plan 两种请求上的**固定底与余量**。

    **先前这个脚本一个字都不报它**（独立审查 2026-09-11，P2-2）：
    `_check_request_floors` 的 docstring 写着「每个帧的余量由
    `scripts/check_material_dimensions.py` 报出来，看得见」，
    **而那句兜底不成立** —— 脚本只 import 素材那几个常量，
    `PLATFORM_PROMPT_BYTES` / `PLATFORM_RESPONSE_SCHEMA_BYTES` 在 `scripts/` 下零引用。

    于是本批**有意不做**的那一半（回合准备金，T275）**既没有闸、也没有能看见它的
    报告**：一个 goal 从 1.5 KB 长到 8 KB 的包，封得了版，
    **而没有任何地方显示余量正在消失**。

    取 (i) 而不是 (ii)（把 docstring 改成事实）：**T275 的上界本来就要靠这类读数来定**，
    所以让它现在就看得见，比记一句「今天看不见」更有用。

    **2026-09-15：这里曾经有第三个式子，而三把尺子互不相等。**
    本函数先前给 understand 与 plan **两种角色用同一组项**
    （`goal + 整份 information_slots + allowed_strategy_set`），
    而闸对两种角色用的是两组完全不同的项 —— 于是它在 PLAN 一侧**少**报
    102–851 字节、在 UNDERSTAND 一侧**多**报 22–158。
    **本文件顶部那句「A number here that disagrees with admission is a bug in
    this script」就是它自己的判据**，照它改：现在调
    `registry.request_floor_bytes`，这里不再有任何字节算术。
    """

    from fastagent_v2.profiles.registry import (  # noqa: PLC0415 - 与本文件其余 import 同源
        MAX_MODEL_INPUT_BYTES,
        request_floor_bytes,
    )

    print("固定底与余量（线路口径；余量是留给 understanding 与 plan 其余字段的）：")
    for frame in document.get("target_frames", []) or []:
        floors = request_floor_bytes(frame, document=document)
        cells = []
        for role in ("UNDERSTAND_VALUE", "PLAN_CAPABILITY"):
            floor = floors[role]
            room = MAX_MODEL_INPUT_BYTES - floor
            label = role.split("_")[0].lower()
            cells.append(f"{label} {floor:,}（余 {room:,}）")
        print(f"  帧 {str(frame.get('id')):<28} " + "  ".join(cells))
    print()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    package = Path(sys.argv[1])
    document, texts = _load(package)
    print(f"包：{package}")
    print(f"预算 MATERIAL_BYTES_BUDGET = {MATERIAL_BYTES_BUDGET:,}")
    print(f"每段骨架开销（空正文）= {budgeted_entry_cost('material:x:v1', '')} 字节；"
          "正文按线路 UTF-8 计；差别在每条的骨架（约 20 + len(ref)）。见 runtime/prompt_bytes.py")
    _report_request_floors(document)
    ok = True
    for frame in document.get("target_frames", []):
        ok = _report_frame(frame, texts) and ok
        print()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
