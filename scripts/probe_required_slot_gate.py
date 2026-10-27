"""闸二（`STRATEGY_SKIPS_REQUIRED_SLOT`）与死循环升级的反向探针。

## 它回答的问题

「把这段改动整个删掉，有没有任何用例会红」—— 五支变异，每支都问一个具体的
退化形态，并报**红在哪些用例上**。一支全绿的探针不是好消息：
那说明那段代码**没有任何人看着**。

## 范围写死

`tests/contracts/` 整个目录 + 那一份 e2e。上一批的 16 就是范围没覆盖它声称的
东西（只跑了三个文件）。范围窄了，「只有这一条红」这句话就不成立 ——
它可能在没跑到的地方也红了。

## 怎么跑（它要一套起着的栈）

那条 e2e 要真库真队列，而环境**不能另起一套** —— 另起一套就是在测另一个环境。
从 `ci.sh` 派生一个只到 `pytest` 那一步的壳，在它里面跑本脚本：

    n=$(grep -n '^"\\$python_bin" -m pytest$' scripts/ci.sh | cut -d: -f1)
    head -n $((n-1)) scripts/ci.sh > scripts/zz_in_stack.sh
    printf '%s\\n' 'set +e' '"$@"' 'echo "EXIT=$?"' >> scripts/zz_in_stack.sh
    FASTAGENT_PYTHON=/path/to/.venv/Scripts/python.exe \\
      sh scripts/zz_in_stack.sh "$FASTAGENT_PYTHON" scripts/probe_required_slot_gate.py 1556

**那个壳是派生的，不是提交进来的**：一份手抄的环境副本会与 `ci.sh` 漂开，
而「两个脚本各自发明形状」是这个仓已经付过账的那一类（`compose()` 那次）。
跑完删掉它。

## 占位自检为什么排在 `import` 之前

第一版把 `assert_placeholders_filled()` 放在 `main()` 里，而 `TREE` 的占位值
在**模块顶层**就被用去拼 `sys.path` —— 于是没填占位时它确实红了，
**但红的是 `ModuleNotFoundError: probe_harness`**，自检那句话一个字都没打出来。
实测出来的（2026-09-13：把 `TREE` 改回占位跑了一次，先得到
`ModuleNotFoundError`；把自检提到 `import` 之前再跑，得到
`这些占位还没填：['TREE']` 与 `EXIT=1`）。

这正是「**红了之后要确认红的是你要测的那一条**」在探针脚本自己身上的形态：
一个「反正会红」的守卫，红的原因可以与它想说的事完全无关，
而下一个人读到那条 `ModuleNotFoundError` 会去查 `sys.path`，不会去填占位。

`TREE` 现在从脚本位置推出来，所以剩下的占位只有那几个**锚点** ——
它们必须按跑的时候 `_persist_no_send` 与 `_validate_model_plan_against_profile`
的实际文本取，`Mutation` 会强制它们各命中一处。

## 缺栈时为什么不让基线去报

`assert_baseline` 在没有栈的时候说的是「baseline is red before any mutation …
Fix the tree first」—— 而树是好的，缺的是栈（实测：`1553 passed, 3 errors`，
三条 e2e 全是 `SettingsError`）。**一句正确的报错指向错误的原因**，
和 §4.1 那道闸拦的东西是同一个毛病，所以缺栈在下面单独拦一次。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]

#: 升级那两支的锚点。**按跑的时候 `_persist_no_send` 的实际文本取。**
ESCALATION_ANCHOR = (
    "        if manual_task_id is None and reason_code in LOOP_ESCALATION_REASON_CODES:\n"
)
ESCALATION_MUTATION = "        if False:\n"

STREAK_ANCHOR = (
    "                     WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'\n"
    "                     ORDER BY created_at DESC LIMIT 1\n"
)
STREAK_MUTATION = (
    "                     WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'\n"
    "                       AND reason_code = 'STRATEGY_SKIPS_REQUIRED_SLOT'\n"
    "                     ORDER BY created_at DESC LIMIT 1\n"
)


def assert_placeholders_filled() -> None:
    """占位没填就在这里停 —— 而不是让别处先因为占位值而报一个无关的错。

    **排在 `sys.path` 那一行之前**，理由见模块 docstring：
    放在 `main()` 里时它被 `ModuleNotFoundError` 抢了先。
    """

    leftovers = [
        name
        for name, value in (
            ("TREE", str(TREE)),
            ("ESCALATION_ANCHOR", ESCALATION_ANCHOR),
            ("ESCALATION_MUTATION", ESCALATION_MUTATION),
            ("STREAK_ANCHOR", STREAK_ANCHOR),
            ("STREAK_MUTATION", STREAK_MUTATION),
        )
        if "PLACEHOLDER" in value
    ]
    if leftovers:
        raise SystemExit(
            f"这些占位还没填：{leftovers}。**按跑的时候 main 上的实际文本取锚点** ——"
            "升级逻辑的插入点在 `_persist_no_send` 里，`manual_task_id` 那几行附近。"
        )


def assert_the_stack_is_up() -> None:
    """缺栈时自己报，别让基线把它说成「树是坏的」。理由见模块 docstring 末节。"""

    missing = [
        name
        for name in (
            "FASTAGENT_WORKER_DATABASE_URL",
            "FASTAGENT_MIGRATION_DATABASE_URL",
            "FASTAGENT_QUEUE_REDIS_URL",
        )
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise SystemExit(
            f"这套环境里没有起着的栈（缺 {missing}）。**树没问题，缺的是栈** ——"
            "照模块 docstring 里那四行从 ci.sh 派生一个壳，在它里面跑本脚本。"
        )


assert_placeholders_filled()
assert_the_stack_is_up()

sys.path.insert(0, str(TREE / "scripts"))
from probe_harness import Mutation, ProbeSession  # noqa: E402

PYTHON = Path(sys.executable)
VALID = TREE / "services/worker/src/fastagent_v2/worker/inference_validation.py"
PIPELINE = TREE / "services/worker/src/fastagent_v2/worker/pipeline.py"

#: 范围。两段都要，因为升级那三条在 e2e 里而闸二的八条在 contracts 里。
TARGETS = [
    "tests/contracts/",
    "tests/e2e/test_required_slot_loop_escalates.py",
]

GATE = '    required_slots = tuple(registered.get("required_slots", []))\n    if required_slots:\n'

PROBES: list[tuple[str, list[Mutation], str]] = [
    (
        "G1 · 闸二整条关掉",
        [Mutation(VALID, GATE, "    if False:\n")],
        "实测 7 条红：闸二八条里的 **4** 条 + 死循环三条。"
        "**不是八条** —— 另外四条断言的是**放行**（空着但点名了、点名其中之一、"
        "槽位已填、没声明 required_slots、fallback 不受管），"
        "闸关掉之后它们照样放行，所以照绿。**一道闸的守卫里，"
        "只有断言「拒」的那些会在闸消失时红** ——"
        "这一条我先写成了八条，是实测把它改过来的。"
        "死循环那三条也红，但在这一支里不算升级的证据（没有 deny 行就没有升级可谈），"
        "看 E1。",
    ),
    (
        "G2 · 判据改成「一律拒」（不看点名）",
        [
            Mutation(
                VALID,
                "if empty and not (set(empty) & asked):",
                "if empty:",
            )
        ],
        "实测 2 条红，都在闸二那八条里：「空着但点名了 → 放行」与"
        "「缺两个只点名一个 → 放行」。它们正是候选 A 与丙的分界。",
    ),
    (
        "G3 · 判据改成「点名任何东西就放行」",
        [
            Mutation(
                VALID,
                "if empty and not (set(empty) & asked):",
                "if empty and not asked:",
            )
        ],
        "实测 1 条红：「点名别的槽位不算问了」。它是丙退化成「点了名就算」的唯一入口。",
    ),
    (
        "E1 · 升级整条关掉（闸二留着）",
        [Mutation(PIPELINE, ESCALATION_ANCHOR, ESCALATION_MUTATION)],
        "**这一支才是升级守卫的证据。** 实测 2 条红，都在那份 e2e 里，"
        "**闸二八条纹丝不动** —— 插入点没有动到共用路径。"
        "第三条（「隔一行别的码就不升级」）断言的是**不升级**，所以它在这一支里照绿："
        "它守的是升级的**边界**，不是升级本身。",
    ),
    (
        "E2 · 「连续」改成「有没有同码的行」（不看最近一行）",
        [Mutation(PIPELINE, STREAK_ANCHOR, STREAK_MUTATION)],
        "实测 1 条红：「中间隔一行别的理由码 → 不升级」。"
        "它是「连续」这个语义的**独守** —— 而那个语义不在去重键里，"
        "所以除了它没有第二处会发现这个退化。"
        "**这个变异不是拍出来的**：给那句查询加一句 `AND reason_code = ...`"
        "就正好是「数同码的行」那个实现，而它与本实现在前两轮上完全同行为。",
    ),
]


def syntax_ok(paths: list[Path]) -> bool:
    for path in paths:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:
            print(f"    ⚠️ 语法坏了：{path.name}:{error.lineno} —— 这一支的红证明不了事")
            return False
    return True


def run() -> tuple[str, bool, list[str]]:
    done = subprocess.run(  # noqa: S603
        [str(PYTHON), "-m", "pytest", *TARGETS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=TREE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (done.stdout or "").splitlines()
    failed = [line.split(" ")[1] for line in out if line.startswith("FAILED ")]
    summary = next(
        (line for line in reversed(out) if " passed" in line or " failed" in line), "(none)"
    )
    return summary, done.returncode != 0, failed


def report(failed: list[str]) -> str:
    by_file = Counter(item.split("::")[0].split("/")[-1] for item in failed)
    # **按「文件 + 函数」去重**，不按函数名 —— 同名函数在两个文件里各有一个时，
    # 按名字去重会把它们合成一个（上一批的 26 vs 27 就是这么来的）。
    functions = {
        (item.split("::")[0], re.sub(r"\[.*\]$", "", item.split("::")[-1])) for item in failed
    }
    lines = [f"    用例数 {len(failed)} · 用例函数 {len(functions)} · 文件 {len(by_file)}"]
    lines += [f"      {name} · {count}" for name, count in by_file.most_common()]
    return "\n".join(lines)


def main() -> int:
    expected = int(sys.argv[1])
    watched = [VALID, PIPELINE]

    def harness_run() -> tuple[str, bool]:
        summary, red, _ = run()
        return summary, red

    with ProbeSession(paths=watched, run=harness_run) as session:
        print("BASELINE " + session.assert_baseline(passed=expected))
        print(f"范围：pytest {' '.join(TARGETS)}")
        for label, mutations, verdict in PROBES:
            for mutation in mutations:
                mutation.apply()
            ok = syntax_ok(watched)
            summary, red, failed = run()
            session.restore()
            session.verify_restored()
            print(f"\n{label}")
            print(f"  语法有效: {'是' if ok else '否'} · {'RED' if red else '**GREEN**'}")
            print(f"    {summary}")
            if failed:
                print(report(failed))
            print(f"  ✅ 它证明的 / ⚠️ 它不证明的：{verdict}")
            if not red:
                print(
                    "  ⚠️ **这一支全绿** —— 在交付说明里给一句结论："
                    "是独守（没有第二个人看着），还是漏了守卫（本该有人看着而没有）。"
                )
    print("\n还原已逐字节核过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
