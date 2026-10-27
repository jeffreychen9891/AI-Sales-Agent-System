"""`InferencePort` backed by **recorded step documents** instead of a provider.

它存在的理由，和 `DeterministicMockInference` 不是同一个 —— 两者必须分开，
而这一段写在这里是为了让下一个想合并它们的人先读到：

| | `DeterministicMockInference` | `ScriptedInference`（本模块）|
|---|---|---|
| 判据 | **不改变别人的断言** | **演示一段真实发生过的对话** |
| 它为什么会改 | 别处的用例变了 | 演示脚本变了 |
| 读上下文吗 | 不读，永远应答 | 不读，**按轮次序号取回那一轮录下来的判断** |

**所以它们是两个事实，不是一个事实的两份拷贝**（`AGENTS.md`「两个数今天相等，
不等于它们是同一个数 —— 问一句它们会不会因为不同的原因改变」）。
把替身改成会演戏的，`inference.py:283-305` 那段账会重演一次：
那个替身第一版给静默轮次挑了 `wait`，**全量门禁红 148 条**。

## 录下来的是「模型产出」，不是 HTTP 响应体

场景文件里每一步存的是**模型那一侧该产出的那份文档**，而不是供应商的 HTTP
响应。两个后果，都是有意的：

1. **它按 `model_output_schema(contract)` 校验**，也就是**真实供应商被要求遵守
   的那份 schema**（`runtime/contracts.py:404`）—— 平台自有的那几个字段
   （`schema_version` / `event_id` / `origin` / `model_evidence`，以及
   `slot_updates` 里那两个 uuid）由本模块回填，**与 `ModelBackedInference`
   逐字同一条路**（`inference_model.py:730-747`）。
   于是一份场景文件**不可能漂成生产产不出来的形状** ——
   而那正是 `AGENTS.md`「那条用例喂的东西必须是生产里会出现的形状」要防的；
2. **它对提示词改动免疫。** 录在 HTTP 层的 cassette 按请求体匹配，
   而这个仓的提示词已经到 v17、最近三笔合并里两笔在动 goal 文案 ——
   按请求体匹配的 cassette 每周失配一次。本模块的匹配键是「场景 + 轮次序号」。

## 轮次序号从哪来：**不用隐藏的自增计数器**

一个「每调一次 +1」的计数器在这个系统里是错的：它**有重试**
（`tests/e2e/test_retry_budget.py`），而重试会静默地取到下一轮的脚本。

所以序号由**这通对话见过的 event_id 的个数**决定，`event_id → ordinal`
记一张表：同一个事件重试多少次都落在同一轮。
`plan` / `generate` 不自己算，它们按 `understanding["event_id"]` 回查 ——
**一轮的四步因此必然同序号**，而不是「各自算一遍然后希望算得一样」
（`AGENTS.md`「两处各算一遍会让两步看见不同的东西」）。

## 脚本用完了就拒，不回绕

脚本演完最后一轮之后再来一个事件，本模块**抛错**而不是重播最后一轮。
一个会静默回绕的演示会让看的人以为系统在回应，而它只是在复读。
抛的是 `ProviderMalformedOutput` —— **因为那就是它的性质**：
这一层产出了一份管线不能用的东西，与真实模型产出坏文档同族，
于是它走的是现成那条路（安全阀 + 带理由码的留痕），不是一条新的失败路径。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any

from fastagent_v2.runtime.contracts import (
    inference_contract_violations,
    model_output_schema,
    validate_inference_contract,
)
from fastagent_v2.worker.cost import CallContext
from fastagent_v2.worker.providers import ProviderMalformedOutput
from jsonschema import Draft202012Validator, FormatChecker

InferenceDocument = dict[str, Any]

#: 哪个场景。值可以是一个场景 id（在 `SCENARIO_ROOT_ENV` 下找同名 `.json`），
#: 也可以是一条路径。**没有默认值**：一个默认场景会让「忘了配」
#: 与「就要这个场景」在行为上长得一样。
SCENARIO_ENV = "FASTAGENT_SCRIPTED_SCENARIO"
SCENARIO_ROOT_ENV = "FASTAGENT_SCRIPTED_SCENARIO_ROOT"
DEFAULT_SCENARIO_ROOT = "demo/scenarios"

#: 四步 → 它们各自的契约名。`INFERENCE_CONTRACT_NAMES` 是权威那一份，
#: 这里只是把 `InferencePort` 的方法名映射过去。
STEP_CONTRACTS: Mapping[str, str] = MappingProxyType(
    {
        "understand": "understanding-result",
        "read_silence": "silence-reading-result",
        "plan": "plan-result",
        "generate": "generation-result",
    }
)

#: 平台回填、**场景文件不许自己写**的那些键，逐契约。
#: 与 `runtime/contracts.py:366` 的 `_PLATFORM_OWNED_OUTPUT_FIELDS` 同源 ——
#: 这里不另抄一份清单，而是直接从那份 schema 的差集算出来（见 `_load_step`）。
_ROLE = "scripted"


class ScenarioError(RuntimeError):
    """场景文件本身不合法。**在加载时抛，不在回合里抛。**

    与 `ProviderMalformedOutput` 分开，因为两者的读者不同：
    这一个的读者是**写场景文件的人**（演示作者），
    那一个的读者是**读审计的人**（运维）。
    一条报错指向谁，决定它该写成什么样（`AGENTS.md`「一句正确的报错，
    可以指向错误的原因」）。
    """


def _usage_evidence(
    *, model: str, prompt_release: str, usage: Mapping[str, Any] | None
) -> dict[str, Any]:
    """`model_evidence`，带上**录下来的**用量。

    **为什么不是全零**：演示要让人看见成本账那一格，而一份全零的账
    会让看的人以为这个系统不花钱。录下来的数是真实历史里的数，
    场景文件里标着它的出处（`provenance`）。

    `latency_ms` 同理：全零的延迟会让「它有多快」这一问没有答案，
    而那是这类系统第一个被问到的问题。
    """

    recorded = dict(usage or {})
    token_usage: dict[str, int] = {
        "input": int(recorded.get("input_tokens", 0)),
        "output": int(recorded.get("output_tokens", 0)),
    }
    cached = int(recorded.get("cached_input_tokens", 0))
    if cached:
        token_usage["cached_input"] = cached
    return {
        "provider": _ROLE,
        "model": model,
        "prompt_release": prompt_release,
        "latency_ms": int(recorded.get("latency_ms", 0)),
        "token_usage": token_usage,
    }


class _Turn:
    """一轮：四步里录了哪几步，各自的文档与用量。"""

    __slots__ = ("documents", "label", "ordinal", "usage")

    def __init__(
        self,
        *,
        ordinal: int,
        label: str,
        documents: dict[str, dict[str, Any]],
        usage: dict[str, Mapping[str, Any]],
    ) -> None:
        self.ordinal = ordinal
        self.label = label
        self.documents = documents
        self.usage = usage


def _validator(contract: str) -> Draft202012Validator:
    """按**供应商被要求遵守的那份 schema** 建校验器。

    `model_output_schema` 已经把 `$ref` 内联掉了，所以这里不需要 registry；
    `FormatChecker` 带上，是为了让 uuid / date-time 这类格式与生产同侧 ——
    一份在生产会被拒的文档，不该在演示里过得去。
    """

    return Draft202012Validator(model_output_schema(contract), format_checker=FormatChecker())


class ScriptedInference:
    """四步推理的**回放替身**。读场景文件，不读上下文，不开 socket。"""

    provider = _ROLE

    def __init__(
        self,
        *,
        scenario: Mapping[str, Any],
        source: str,
        content_sha256: str,
    ) -> None:
        self._scenario_id = str(scenario["scenario_id"])
        self._source = source
        self._content_sha256 = content_sha256
        self._provenance = str(scenario["provenance"])
        self._turns = _parse_turns(scenario, source=source)
        #: 证据里要认得出**是哪一份场景文件的哪一个版本**产的这一轮。
        #: 内容寻址，与配包的 `content_hash` 同一条纪律：
        #: 改一个字，`version` 就变，而它会进 `model_evidence`。
        self.version = f"scripted-v1:{self._scenario_id}@{content_sha256[:12]}"
        self.prompt_release = f"scenario:{self._scenario_id}"
        self._lock = Lock()
        self._ordinal_of_event: dict[str, int] = {}
        self._events_seen_in: dict[str, int] = {}

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_env(cls, *, repository_root: Path | None = None) -> ScriptedInference:
        """按环境变量找场景文件。**找不到就拒，不挑一个默认的。**"""

        requested = os.getenv(SCENARIO_ENV, "").strip()
        if not requested:
            raise ScenarioError(
                f"{SCENARIO_ENV} is not set, so there is no scenario to replay. "
                f"Set it to a scenario id (resolved under {SCENARIO_ROOT_ENV} or "
                f"{DEFAULT_SCENARIO_ROOT!r}) or to a path to a scenario file."
            )
        root_override = os.getenv(SCENARIO_ROOT_ENV, "").strip()
        base = repository_root or Path(__file__).resolve().parents[5]
        candidate = Path(requested)
        if candidate.suffix == ".json":
            path = candidate if candidate.is_absolute() else base / candidate
        else:
            root = Path(root_override) if root_override else base / DEFAULT_SCENARIO_ROOT
            if not root.is_absolute():
                root = base / root
            path = root / f"{requested}.json"
        return cls.from_path(path)

    @classmethod
    def from_path(cls, path: Path) -> ScriptedInference:
        if not path.is_file():
            raise ScenarioError(f"no scenario file at {path}")
        raw = path.read_bytes()
        try:
            scenario = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScenarioError(f"{path} is not valid UTF-8 JSON: {error}") from error
        if not isinstance(scenario, dict):
            raise ScenarioError(f"{path} must hold a JSON object, not a {type(scenario).__name__}")
        for required in ("scenario_id", "provenance", "turns"):
            if required not in scenario:
                raise ScenarioError(
                    f"{path} has no {required!r}. "
                    + (
                        # **`provenance` 是必填的，而这不是形式主义。**
                        # 一份没有出处的录制件正是 `AGENTS.md`「跨边界的数据结构」
                        # 那一节点名的「不算满足的」第一种：自己凭空写的字面量。
                        # 把它写成加载期必填，等于给那条纪律配了一个执行者。
                        "A recording without a stated origin is the 'literal somebody "
                        "made up' case that cross-boundary fixtures are not allowed to "
                        "be; say where these documents came from."
                        if required == "provenance"
                        else ""
                    )
                )
        return cls(
            scenario=scenario,
            source=str(path),
            content_sha256=hashlib.sha256(raw).hexdigest(),
        )

    # ---------------------------------------------------------------- 自述

    @property
    def scenario_id(self) -> str:
        return self._scenario_id

    @property
    def provenance(self) -> str:
        return self._provenance

    @property
    def content_sha256(self) -> str:
        return self._content_sha256

    @property
    def source(self) -> str:
        """这份场景从哪个文件来。报错与演示界面都要说得出它。"""

        return self._source

    def turn_labels(self) -> tuple[tuple[int, str], ...]:
        """`(序号, 标题)`，按序号。演示界面拿它画那条时间轴。"""

        return tuple((turn.ordinal, turn.label) for turn in self._turns)

    def recorded_steps(self, ordinal: int) -> frozenset[str]:
        """这一轮录了四步里的哪几步。

        **有了它，调用方不必再去读一遍文件猜**（演示界面要画「这一轮是不是
        定时器轮次」，用例要知道该不该调 `read_silence`）——
        而「再读一遍文件自己解析」就是本仓「两处各算一遍」那一族的入口。
        """

        for turn in self._turns:
            if turn.ordinal == ordinal:
                return frozenset(turn.documents)
        return frozenset()

    # ---------------------------------------------------------------- 取回

    def _assign(self, *, conversation_id: str, event_id: str) -> int:
        with self._lock:
            existing = self._ordinal_of_event.get(event_id)
            if existing is not None:
                return existing
            ordinal = self._events_seen_in.get(conversation_id, 0) + 1
            self._events_seen_in[conversation_id] = ordinal
            self._ordinal_of_event[event_id] = ordinal
            return ordinal

    def _recall(self, *, event_id: str, step: str) -> int:
        with self._lock:
            ordinal = self._ordinal_of_event.get(event_id)
        if ordinal is None:
            # 管线保证 `understand` / `read_silence` 先跑，所以到这里说明
            # 调用次序与假设不符 —— 报出来，不猜一个序号。
            raise ProviderMalformedOutput(
                f"{step} was asked about event {event_id} before any step of that turn ran, "
                "so the scripted double cannot tell which turn it is. This is a call-order "
                "assumption of the scripted double, not a model failure.",
                role=_ROLE,
            )
        return ordinal

    def _turn(self, *, ordinal: int, step: str) -> _Turn:
        for turn in self._turns:
            if turn.ordinal == ordinal:
                return turn
        raise ProviderMalformedOutput(
            f"scenario {self._scenario_id!r} ({self._source}) has "
            f"{len(self._turns)} turn(s) and nothing for turn {ordinal}; "
            f"{step} has no recorded answer. The script does not wrap around on purpose -- "
            "replaying the last turn would look like the system is answering when it is "
            "repeating.",
            role=_ROLE,
        )

    def _document(self, *, ordinal: int, step: str) -> tuple[dict[str, Any], Mapping[str, Any]]:
        turn = self._turn(ordinal=ordinal, step=step)
        document = turn.documents.get(step)
        if document is None:
            recorded = ", ".join(sorted(turn.documents)) or "nothing"
            raise ProviderMalformedOutput(
                f"turn {ordinal} ({turn.label!r}) of scenario {self._scenario_id!r} "
                f"records {recorded} but not {step}.",
                role=_ROLE,
            )
        return json.loads(json.dumps(document)), turn.usage.get(step, {})

    def _validated(self, contract: str, document: dict[str, Any]) -> dict[str, Any]:
        """完成回填之后再按**完整契约**校验一次。

        与加载期那一次是**两侧**，不是重复（`AGENTS.md`「放宽一条判据时正反两侧
        都要验」的同形）：加载期验的是「录的这份是模型产得出来的」，
        这一次验的是「回填之后它仍然满足完整契约」。
        只有前一次时，一个回填写错的键会一路带进管线。
        """

        try:
            validate_inference_contract(contract, document)
        except Exception as error:  # noqa: BLE001 - 统一映射成一条带全部违规的拒绝
            violations = inference_contract_violations(contract, document)
            raise ProviderMalformedOutput(
                f"scripted {contract} document does not satisfy the contract after the "
                f"platform fields were filled in: {'; '.join(violations) or error}",
                role=_ROLE,
            ) from error
        return document

    # ---------------------------------------------------------------- 四步
    #
    # 下面四个签名**逐字照抄 `InferencePort`**，包括它不读的那些参数。
    # 理由见 `inference.py:444-447` 那条账：生产那一侧加了一个关键字参数
    # 而替身没跟上时，`contracts` 全绿而门禁 123 条红，一条都不在 contracts 里。

    def understand(
        self,
        *,
        envelope: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],  # noqa: ARG002 - 回放替身：它不读图纸，录下来的就是那一轮的判断
        context: CallContext | None = None,  # noqa: ARG002 - 同上
        last_outbound: str = "",  # noqa: ARG002 - 同上
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        memories_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
    ) -> InferenceDocument:
        ordinal = self._assign(
            conversation_id=str(aggregate["id"]), event_id=str(envelope["event_id"])
        )
        document, usage = self._document(ordinal=ordinal, step="understand")
        document["schema_version"] = "1.0.0"
        document["event_id"] = str(envelope["event_id"])
        document["model_evidence"] = _usage_evidence(
            model=self.version, prompt_release=self.prompt_release, usage=usage
        )
        # 与 `inference_model.py:745-747` 同一条：两个 uuid 是身份，平台盖章。
        frame = aggregate["target_stack"][-1]
        for update in document.get("slot_updates", []):
            update["frame_id"] = str(frame["id"])
            update["evidence_event_id"] = str(envelope["event_id"])
        return self._validated("understanding-result", document)

    def read_silence(
        self,
        *,
        envelope: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],  # noqa: ARG002 - 回放替身
        context: CallContext | None = None,  # noqa: ARG002 - 同上
        last_outbound: str = "",  # noqa: ARG002 - 同上
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        memories_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
        silent_for_seconds: float = 0.0,  # noqa: ARG002 - 同上
        unanswered_rounds: int = 0,  # noqa: ARG002 - 同上
        offered_materials: Mapping[str, str] = MappingProxyType({}),  # noqa: ARG002 - 同上
    ) -> InferenceDocument:
        ordinal = self._assign(
            conversation_id=str(aggregate["id"]), event_id=str(envelope["event_id"])
        )
        document, usage = self._document(ordinal=ordinal, step="read_silence")
        document["schema_version"] = "1.0.0"
        # **这一份不盖 `event_id`**，与 `contracts.py:370-378` 写明的理由一致：
        # 它的对象是一段没有事件的沉默，盖上定时器的 id 会让它读起来像在说那个定时器。
        document["model_evidence"] = _usage_evidence(
            model=self.version, prompt_release=self.prompt_release, usage=usage
        )
        return self._validated("silence-reading-result", document)

    def plan(
        self,
        *,
        understanding: InferenceDocument,
        aggregate: dict[str, Any],  # noqa: ARG002 - 回放替身
        profile: dict[str, Any],  # noqa: ARG002 - 同上
        context: CallContext | None = None,  # noqa: ARG002 - 同上
        required_strategy: str | None = None,
        assets: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        last_outbound: str = "",  # noqa: ARG002 - 同上
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        memories_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
        unkept_promises: Sequence[str] = (),  # noqa: ARG002 - 同上
        silence_reading: Mapping[str, Any] | None = None,  # noqa: ARG002 - 同上
    ) -> InferenceDocument:
        ordinal = self._recall(event_id=str(understanding["event_id"]), step="plan")
        document, usage = self._document(ordinal=ordinal, step="plan")
        recorded_strategy = document.get("selected_strategy_id")
        if required_strategy is not None and recorded_strategy != required_strategy:
            # **定时器轮次的策略由规则定**（B4-4d）。录的那一轮与规则声明的不一致时
            # 报出来，而不是让校验器把它拒成安全阀 —— 后者在演示里长得像
            # 「系统决定不回」，而真因是场景文件与配包对不上号。
            raise ProviderMalformedOutput(
                f"turn {ordinal} of scenario {self._scenario_id!r} records strategy "
                f"{recorded_strategy!r} while the time rule behind this turn declares "
                f"{required_strategy!r}. The scenario file and the profile package "
                "disagree; fix the scenario rather than reading this as a model choice.",
                role=_ROLE,
            )
        document["schema_version"] = "1.0.0"
        document["origin"] = "model"
        document["model_evidence"] = _usage_evidence(
            model=self.version, prompt_release=self.prompt_release, usage=usage
        )
        return self._validated("plan-result", document)

    def generate(
        self,
        *,
        plan: InferenceDocument,  # noqa: ARG002 - 回放替身：候选意图已经录在 generate 那一份里
        understanding: InferenceDocument,
        envelope: dict[str, Any],  # noqa: ARG002 - 同上
        materials: Mapping[str, str],  # noqa: ARG002 - 同上
        knowledge: Mapping[str, str] = MappingProxyType({}),  # noqa: ARG002 - 同上
        assets: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        context: CallContext | None = None,  # noqa: ARG002 - 同上
        last_outbound: str = "",  # noqa: ARG002 - 同上
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
        remembered_facts: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        memories_not_shown: int = 0,  # noqa: ARG002 - 同上
        rememberable_fields: Sequence[Mapping[str, Any]] = (),  # noqa: ARG002 - 同上
        pending_question: Any = None,  # noqa: ARG002 - 同上
    ) -> InferenceDocument:
        ordinal = self._recall(event_id=str(understanding["event_id"]), step="generate")
        document, usage = self._document(ordinal=ordinal, step="generate")
        document["schema_version"] = "1.0.0"
        document["origin"] = "model"
        document["model_evidence"] = _usage_evidence(
            model=self.version, prompt_release=self.prompt_release, usage=usage
        )
        return self._validated("generation-result", document)


def _parse_turns(scenario: Mapping[str, Any], *, source: str) -> tuple[_Turn, ...]:
    """把 `turns` 读成 `_Turn`，**每一步当场按模型侧 schema 校验**。

    加载期全部验完，不是用到那一步才验：一个坏在第七轮的场景文件
    应当在启动时就报出来，而不是在演示到第七轮时。
    （`AGENTS.md`「任何『报第一个』的工具，读它的输出不等于读到了全部」——
    所以这里把**所有**问题收齐再抛，不是抛第一个。）
    """

    raw_turns = scenario["turns"]
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ScenarioError(f"{source}: 'turns' must be a non-empty array")

    problems: list[str] = []
    turns: list[_Turn] = []
    seen_ordinals: set[int] = set()

    for index, raw in enumerate(raw_turns):
        where = f"turns[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: must be an object")
            continue
        ordinal_value = raw.get("ordinal", index + 1)
        if not isinstance(ordinal_value, int) or isinstance(ordinal_value, bool):
            problems.append(f"{where}.ordinal: must be an integer")
            continue
        if ordinal_value in seen_ordinals:
            problems.append(f"{where}.ordinal: {ordinal_value} is used twice")
            continue
        seen_ordinals.add(ordinal_value)
        label = str(raw.get("label") or f"turn {ordinal_value}")

        steps = raw.get("steps")
        if not isinstance(steps, dict):
            problems.append(f"{where}.steps: must be an object")
            continue

        unknown = sorted(set(steps) - set(STEP_CONTRACTS) - {"usage"})
        if unknown:
            problems.append(
                f"{where}.steps: unknown step(s) {unknown}; known steps are "
                f"{sorted(STEP_CONTRACTS)}"
            )

        documents: dict[str, dict[str, Any]] = {}
        for step, contract in STEP_CONTRACTS.items():
            document = steps.get(step)
            if document is None:
                continue
            if not isinstance(document, dict):
                problems.append(f"{where}.steps.{step}: must be an object")
                continue
            errors = sorted(
                _validator(contract).iter_errors(document),
                key=lambda error: (error.json_path, str(error.validator)),
            )
            for error in errors:
                problems.append(
                    f"{where}.steps.{step} ({contract}) {error.json_path}: {error.message}"
                )
            documents[step] = document

        if not documents:
            problems.append(f"{where}.steps: records no step at all")

        usage_block = steps.get("usage", {})
        if not isinstance(usage_block, dict):
            problems.append(f"{where}.steps.usage: must be an object keyed by step name")
            usage_block = {}
        usage: dict[str, Mapping[str, Any]] = {
            step: value for step, value in usage_block.items() if isinstance(value, dict)
        }

        turns.append(_Turn(ordinal=ordinal_value, label=label, documents=documents, usage=usage))

    if problems:
        raise ScenarioError(
            f"{source} is not a usable scenario ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems)
        )
    return tuple(sorted(turns, key=lambda turn: turn.ordinal))
