"""The real-provider replay script builds the same plan prompt production does.

**Independent review 2026-09-10, P2-2.** The script imports the production
`_PLAN_SYSTEM` but assembles its own user prompt, and it stamps every result
with `prompt_release = PROMPT_RELEASE`. So when the system prompt gained "choose
only from `allowed_generation_constraints_by_strategy`" and the script's payload
did not, the model was handed an instruction pointing at a key that was not
there -- while the output was labelled `m1a-prompt-v4`.

That is evidence gathered in an environment that differs from the real one, and
labelled as if it were not. This repository has a standing answer for that shape
(`PROMPT_RELEASE` and the replay golden are pinned to each other so that
"forgetting is not one of the available outcomes"); this file gives the script's
prompt the same treatment.

**Read out of the source rather than by running the script**, because running it
requires a real provider, an account balance and money -- T11 is still open for
exactly that reason. The keys are what drift; reading them is enough to catch
the drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "replay_deepseek_v4_flash.py"
#: **2026-09-15：生产那一侧的组装搬进了 `runtime/request_shape.py`**（唯一定义点）。
#: `inference_model.plan` 现在调 `request_shape.plan_prompt`，所以键在那边。
PRODUCTION = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"


def _plan_prompt_keys(source: Path, *, function: str) -> set[str]:
    """The literal keys of the `plan` user prompt built in `source`.

    Both sides build the payload as a dict literal inside a `json.dumps(...)`
    call, so the keys are readable without executing anything: the script would
    need a provider, and the production path would need a whole aggregate.
    """

    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for inner in ast.walk(node):
            # **两种形态都认**（2026-09-15）：脚本那一侧仍然是
            # `json.dumps({...})`；生产那一侧的组装搬进了 `request_shape`，
            # 那里是直接 `return {...}`。认死一种形态会让这条守卫
            # 在搬家之后**解析不到东西然后恒绿** —— 那正是本文件要防的形状。
            payload = None
            if isinstance(inner, ast.Call):
                call = inner.func
                is_dumps = (
                    isinstance(call, ast.Attribute)
                    and call.attr == "dumps"
                    and isinstance(call.value, ast.Name)
                    and call.value.id == "json"
                )
                if is_dumps and inner.args:
                    payload = inner.args[0]
            elif isinstance(inner, ast.Return):
                payload = inner.value
            if not isinstance(payload, ast.Dict):
                continue
            keys = {
                key.value
                for key in payload.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if "allowed_strategies" in keys:
                return keys
    raise AssertionError(f"no plan user prompt found in {source.name}::{function}")


def test_the_replay_script_sends_the_same_plan_prompt_keys_as_production() -> None:
    """Equal key sets, so a field added to one cannot be missing from the other.

    The failure this prevents is not a crash: the script keeps running, the
    model answers a slightly different question, and the result is filed under
    the production prompt's release string.
    """

    production = _plan_prompt_keys(PRODUCTION, function="plan_prompt")
    script = _plan_prompt_keys(SCRIPT, function="main")

    assert "allowed_generation_constraints_by_strategy" in production
    assert script == production, (
        "the replay script's plan prompt drifted from production's: "
        f"missing {sorted(production - script)}, extra {sorted(script - production)}"
    )


def _generate_prompt_keys(source: Path, *, function: str) -> set[str]:
    """同一种读法，换一个锚点键。

    生产那边锚 `strategy_reason_code`（只有生成步的 `user_prompt` 有它），
    脚本那边同理 —— 与上面那个 helper 逐字同一条理由：
    读 AST 里那个 dict 的键集，不数字符串。
    """

    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for inner in ast.walk(node):
            payload: ast.expr | None = None
            if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Call):
                call = inner.value
                is_dumps = (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "dumps"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "json"
                )
                if is_dumps and call.args:
                    payload = call.args[0]
            elif isinstance(inner, ast.Return):
                payload = inner.value
            if not isinstance(payload, ast.Dict):
                continue
            keys = {
                key.value
                for key in payload.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if "strategy_reason_code" in keys:
                return keys
    raise AssertionError(f"no generate user prompt found in {source.name}::{function}")


def test_the_replay_script_sends_the_same_generate_prompt_keys_as_production() -> None:
    """**生成步那份镜像也要对齐 —— 而它 2026-09-18 之前没人守着。**

    上面那条只盯计划步，于是脚本里生成步这份**悄悄少了三个键**
    （`content_materials` / `knowledge_chunks` / `content_assets`）。
    本批往生成步加 `last_outbound` 时数出来是四个。

    **它的失败形态与计划步那条逐字相同**：脚本照跑、模型答一个
    略有不同的问题，**而结果按生产那串 `prompt_release` 归档** ——
    于是「回放说它没复现」这句话本身就不成立。
    """

    production = _generate_prompt_keys(PRODUCTION, function="generate_prompt")
    script = _generate_prompt_keys(SCRIPT, function="main")

    assert "content_materials" in production
    assert script == production, (
        "the replay script's generate prompt drifted from production's: "
        f"missing {sorted(production - script)}, extra {sorted(script - production)}"
    )
