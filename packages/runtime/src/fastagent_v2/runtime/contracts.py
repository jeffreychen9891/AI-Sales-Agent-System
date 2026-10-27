from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

#: What the envelope carries in `actor.actor_id` when the payload named
#: nobody. **A placeholder, not a person**: `contacts` (039) must never key
#: an identity on it, or every unidentified sender collapses into one row
#: that looks like a customer who never stops talking.
#:
#: **Here rather than in each service** (independent review 2026-09-06,
#: P3-2): ingress writes it into the envelope and the worker reads it back
#: to decide whether a conversation has a contact. Two copies of one rule
#: that must agree is the shape that drifts, and "the two sides cannot
#: disagree about who the customer is" is the argument the contact link
#: rests on.
UNKNOWN_ACTOR = "unknown"

#: Target-frame definition ids beginning with this belong to the **platform**,
#: not to a merchant profile (ADR-0099, B4-7).
#:
#: **Here for exactly the reason `UNKNOWN_ACTOR` is here**, and this batch found
#: out the hard way: the *api* service refuses a profile that declares such an
#: id, and the *worker* resolves those ids for every conversation. Two services,
#: one rule. The first version put it in `worker.platform_frames` and had the
#: profile registry import it from there, which broke the deployment boundary --
#: `test_the_registry_trusts_the_deployment_it_runs_in_not_the_tree_it_was_built_from`
#: starts the api with only its own source root on the path, and the worker
#: package is not on it. **A rule the api enforces may not live in the worker.**
PLATFORM_FRAME_ID_PREFIX = "platform:"


def is_platform_frame(definition_id: object) -> bool:
    """Whether this frame definition id belongs to the platform (ADR-0099).

    The predicate travels with the prefix so that "starts with `platform:`" is
    written once. A second spelling is how the admission guard and the resolver
    come to disagree about which ids are reserved -- and that disagreement is
    silent in the direction that matters: a profile publishes a frame the
    platform then shadows, and the tenant does not get the behaviour the profile
    describes.
    """

    return str(definition_id).startswith(PLATFORM_FRAME_ID_PREFIX)


#: The audit action the worker writes when RFC 4.3 refuses a slot update,
#: and the action `GET /conversations/{id}` filters on to project them
#: back (B3 #2 second delivery, migration 042).
#:
#: **Here for the reason `UNKNOWN_ACTOR` is here**: one service writes the
#: string and another selects on it, and a reader that disagrees with the
#: writer about the action name does not fail -- it silently returns an
#: empty list, which is the shape ADR-0076 §四.2 spent a page refusing.
SLOT_UPDATE_REFUSED_ACTION = "conversation.slot_update_refused"

#: A cost ceiling hit **while a human already holds the conversation**
#: (ADR-0054 §三.6). The worker writes it into `policy_decisions.reason_code`,
#: the conversation detail route counts it, and a rename on one side must not
#: read as "it never happened" on the other -- same argument as
#: `SLOT_UPDATE_REFUSED_ACTION` above.
#:
#: Deliberately **not** in the pipeline's `HANDOFF_REASON_CODES`, and that is
#: the whole point: the handoff path suspends the stack, writes
#: `control_mode = 'human'` and files a manual task -- and all three have
#: already happened. Routed through it, this condition would perform three
#: no-ops and tell nobody, which is exactly what it did before ADR-0058: the
#: understanding step went quiet, the resume preview lost its data, and there
#: was no record that either had happened.
#:
#: Reachable at a realistic volume, not in theory: at `UNDERSTAND_VALUE`'s
#: conservative ¥0.00528 a message, a customer sending **21** messages during
#: a takeover exhausts the conversation soft ceiling and **51** hits hard.
#:
#: Plain text, no migration: `policy_decisions.reason_code` is `text NOT NULL`
#: with no CHECK.
UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL = "UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL"

#: The deployment root: the directory the process was started in, which is
#: `WORKDIR /app` in the image and the repository root under the gate (`ci.sh`
#: and `ci.ps1` both `cd` there before anything runs, for exactly this reason).
#:
#: **This is the one place the layout is decided.** It used to be decided twice:
#: here by working directory, and in `profiles/registry.py` by counting five
#: parent directories up from that module's own file. In the source tree the two
#: agreed by coincidence -- `services/api/src/fastagent_v2/profiles/` is five
#: levels below the repository root -- and in the image they did not: `pip
#: install` puts `fastagent_v2.profiles` in `site-packages`, where five levels up
#: is `/usr/local`, so the trust root moved to a directory containing no
#: profiles at all while `CONTRACT_ROOT` stayed at `/app/packages/contracts`
#: (independent review 2026-09-04, P1-1).
#:
#: A layout derived from a module's install location answers "where is this
#: code" when the question is "where is this deployment". The second question
#: has one answer per process, so it gets one definition.
DEPLOYMENT_ROOT = Path.cwd()

CONTRACT_ROOT = DEPLOYMENT_ROOT / "packages" / "contracts"
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
INFERENCE_SCHEMA_ROOT = CONTRACT_ROOT / "inference"
CONTROL_PLANE_SCHEMA_ROOT = CONTRACT_ROOT / "control-plane"
#: The narrow waist: the records and envelopes the engine persists and passes
#: across its own seams.
#:
#: **`commitment-record` joined on 2026-09-16 (批 11a), and that is a choice
#: worth naming.** The comment below says a *new family* gets its own directory
#: rather than loosening the count here -- and that is still the rule. It does
#: not apply to this one: `inference` and `control-plane` are different **kinds**
#: (step results; HTTP envelopes), while `CommitmentRecord` is the same kind as
#: `intent-record` and `scheduled-event` -- a tenant-scoped persisted record with
#: a status vocabulary that this engine reads and writes. **The families are
#: separated by kind, not by a count that must never move.**
#:
#: The birth gate moved with it, and got **stronger rather than looser**: it used
#: to assert `len(...) == 7`, which a new name satisfies by editing one digit;
#: it now asserts the **set** (`test_the_narrow_waist_is_exactly_these`), so a
#: new contract has to be named in a test before it can be loaded.
CONTRACT_NAMES = (
    "message-envelope",
    "conversation-aggregate",
    "intent-record",
    "policy-hook",
    "channel-adapter",
    "merchant-profile",
    "scheduled-event",
    "commitment-record",
)
#: Step results of the three-stage inference path. They are deliberately kept
#: out of `SCHEMA_ROOT`: the narrow-waist contracts are a fixed set and a birth
#: gate asserts it **by name** (it used to assert a count of seven; 批 11a added
#: an eighth of the same kind and turned the count into a set).
INFERENCE_CONTRACT_NAMES = (
    "understanding-result",
    "plan-result",
    "generation-result",
    #: **第四步的结果，而它只在主动轮次产出**（ADR-0149，2026-09-22）。
    #: 客户消息那一轮一次都不调它 —— 三步对那一侧逐字不变。
    "silence-reading-result",
    "confidence",
    "model-evidence",
)
#: The common HTTP contracts every control-plane route builds on
#: (`control-plane-parity.md` §5). A third family for the same reason the
#: inference one exists: the narrow-waist contracts in `SCHEMA_ROOT` are a fixed
#: set with a birth gate asserting it by name, so a new **family** gets its own
#: directory and its own gate rather than loosening that assertion. **A new
#: contract of an existing kind joins the set instead** -- see the note above
#: `CONTRACT_NAMES` for why `commitment-record` is the second case and not the
#: first.
CONTROL_PLANE_CONTRACT_NAMES = (
    "page",
    "projection-version",
    "resource-ref",
    "command-receipt",
    "error-envelope",
    "superuser-target",
    "secret-write",
)


@cache
def _parsed_schema(name: str) -> dict[str, Any]:
    if name not in CONTRACT_NAMES:
        raise KeyError(f"unknown contract: {name}")
    return cast(
        dict[str, Any],
        json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")),
    )


def load_schema(name: str) -> dict[str, Any]:
    """A schema, copied. The cache holds one parse; callers get their own object.

    The cached value used to be handed out directly, so every caller shared one
    mutable dict -- including `GET /profiles/schema`, which puts it straight into
    a response body (independent review 2026-09-04, P3-5). This is the same
    reasoning that made `authz._resource_catalog()` return a `MappingProxyType`:
    a parsed contract is *evidence*, and evidence anyone in the process can edit
    is not evidence. A copy rather than a proxy here because the value is nested
    and callers legitimately walk it into `jsonschema`, which expects plain
    dicts.
    """

    return deepcopy(_parsed_schema(name))


@cache
def load_inference_schema(name: str) -> dict[str, Any]:
    if name not in INFERENCE_CONTRACT_NAMES:
        raise KeyError(f"unknown inference contract: {name}")
    return cast(
        dict[str, Any],
        json.loads((INFERENCE_SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")),
    )


@cache
def load_control_plane_schema(name: str) -> dict[str, Any]:
    if name not in CONTROL_PLANE_CONTRACT_NAMES:
        raise KeyError(f"unknown control-plane contract: {name}")
    return cast(
        dict[str, Any],
        json.loads(
            (CONTROL_PLANE_SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")
        ),
    )


@cache
def _control_plane_registry() -> Registry[Any]:
    resources = [
        (str(schema["$id"]), Resource.from_contents(schema))
        for schema in (
            load_control_plane_schema(name) for name in CONTROL_PLANE_CONTRACT_NAMES
        )
    ]
    return Registry().with_resources(resources)


def validate_control_plane_contract(
    name: str, value: dict[str, Any], *, fragment: str | None = None
) -> None:
    """Validate against a control-plane contract, or one `$defs` half of it.

    `fragment` names an entry under `$defs`. It exists for `secret-write`: its
    two halves are deliberately asymmetric, and a route validating the response
    it is about to return must not have to build a request object holding the
    plaintext just to satisfy the schema.
    """

    schema = load_control_plane_schema(name)
    if fragment is not None:
        try:
            schema = cast(dict[str, Any], schema["$defs"][fragment])
        except KeyError as error:
            raise KeyError(f"{name} has no $defs entry {fragment!r}") from error
    validator = Draft202012Validator(
        schema,
        registry=_control_plane_registry(),
        format_checker=FormatChecker(),
    )
    validator.validate(value)


@cache
def _inference_registry() -> Registry[Any]:
    resources = [
        (str(schema["$id"]), Resource.from_contents(schema))
        for schema in (load_inference_schema(name) for name in INFERENCE_CONTRACT_NAMES)
    ]
    return Registry().with_resources(resources)


def validate_contract(name: str, value: dict[str, Any]) -> None:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    validator.validate(value)


def describe_validation_error(error: ValidationError) -> str:
    """一条校验失败的**不含正文**描述。

    ## 这个函数是被一次真实泄露逼出来的（2026-09-16）

    它的第一版写在 `scripts/check_stored_releases.py` 里，用的是
    `error.message[:160]`，而同一份 docstring 里写着「**不打正文是有意的**：
    release 里有商家的话术」。**两句话不能同时为真** —— jsonschema 在
    `maxLength` 上的消息体**就是那个超长的值本身**（`'<整段话术>' is too long`），
    于是那次跑把两行各约 160 字商家 goal 原文打进了部署记录。
    `enum` / `type` / `pattern` / `const` 同理，**截断救不了：
    被截掉的是尾巴，留下的是正文。**

    所以这里是**正向白名单**：只取出自我们自己 schema 的字段，
    外加一个不含内容的形状摘要。`CLAUDE.md` 第五节那句「用正向白名单或逐键判断，
    不要用排除式过滤」是同一条道理 —— 上一次排除式过滤漏掉的是 `DATABASE_URL`，
    那一次是「截断过的 message」。

    ## 它为什么在这里，而不在那支脚本里

    **2026-09-16 第二个调用方出现了**：`profiles/resolver.py` 的
    `releases()` 也要说清「这一版今天过不了合同」而不打正文 ——
    那条路上同一段 goal 正文正是从 `ValidationError` 原样抛出去的。
    两处各写一遍，迟早只改一处；而这一族的代价已经付过一次了。

    ## 它打什么、不打什么

    | 打 | 为什么安全 |
    |---|---|
    | `error.validator` | 关键字名（`maxLength` / `enum` / …） |
    | `error.validator_value` | **来自 schema**，是我们自己的合同 |
    | 值的类型与长度 | 一个数和一个类型名，没有字符 |
    | `error.json_path` | 出事的位置，**不含值** |

    **不打**：`error.instance`、`error.message`、以及任何由它们派生的串。

    ## 它换来的代价，如实写着

    `additionalProperties` 那一类失败**说不出是哪个多余的键** ——
    那个键名只在 `message` 里。拿着库的人自己查得到；
    而让这里去解析 message 把键名抠出来，等于把刚关上的门留一条缝。
    """

    instance = error.instance
    if isinstance(instance, str):
        shape = f"str/{len(instance)} 字符"
    elif isinstance(instance, list | dict):
        shape = f"{type(instance).__name__}/{len(instance)} 项"
    else:
        shape = type(instance).__name__

    rendered = str(error.validator_value)
    if len(rendered) > 120:
        rendered = rendered[:120] + "…"
    return f"{error.json_path}: {error.validator}（约束 {rendered}；实际 {shape}）"


def _inference_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        load_inference_schema(name),
        registry=_inference_registry(),
        format_checker=FormatChecker(),
    )


def validate_inference_contract(name: str, value: dict[str, Any]) -> None:
    _inference_validator(name).validate(value)


def inference_contract_violations(name: str, value: dict[str, Any]) -> list[str]:
    """这份文档**一共**破了哪几条规则，每条一行，用同一个正向白名单渲染。

    ## 为什么不是「把抛出来的那个 error 渲染一下」就够了

    `validator.validate()` 抛的是 jsonschema 认为**最匹配的那一条**，
    不是全部。`AGENTS.md`「任何『报第一个』的工具，读它的输出不等于读到了全部」
    点名的成员里就有 `jsonschema` 的第一条 violation ——
    **而那一条的代价 2026-09-22 在 stage 上付过**：同一份输入，理解步跑四次挂两次，
    审计里只有一句「output does not satisfy the understanding-result contract」，
    运维花了一个多小时仍然没缩到某一条规则
    （`docs/handoffs/ops-to-author-the-memory-write-takes-the-turn-with-it-2026-09-22.md`）。

    所以这里走 `iter_errors`：修掉一条再跑一次不该是唯一的办法。

    ## 它打什么、不打什么

    逐条交给 `describe_validation_error`，**那条白名单是唯一的渲染点** ——
    不在这里另写一套，否则两处迟早只改一处（那正是它当初被搬进本模块的理由）。

    **排序是确定的**（按 `json_path`）：一条会进审计、会被人逐字比对的消息，
    两次跑出来的顺序不一样会让人以为内容变了。
    """

    errors = sorted(
        _inference_validator(name).iter_errors(value),
        key=lambda error: (error.json_path, str(error.validator)),
    )
    return [describe_validation_error(error) for error in errors]


_PLATFORM_OWNED_OUTPUT_FIELDS = {
    "understanding-result": {"schema_version", "event_id", "model_evidence"},
    "plan-result": {"schema_version", "origin", "model_evidence"},
    "generation-result": {"schema_version", "origin", "model_evidence"},
    #: 第四步（ADR-0149）。平台写的只有**两样** —— 另外三份都有第三样
    #: （`event_id` 或 `origin`），而这一份两样都没有：
    #:
    #: * 没有 `event_id`：那三份是「对某一条入站事件的产物」，
    #:   而这一步的对象是**一段没有事件的沉默**。定时器信封确实有 id，
    #:   盖上去会让这份文档读起来像在说那个定时器；
    #: * 没有 `origin`：那是计划/生成用来分「模型产的还是平台兜的」的，
    #:   而这一步**没有平台兜底的那一支** —— 它失败就是失败（ADR-0149 §六）。
    "silence-reading-result": {"schema_version", "model_evidence"},
}

#: Platform-owned fields *inside array items*, keyed by contract then by the
#: array property holding them.
#:
#: `slot_updates` items require `frame_id` and `evidence_event_id` -- two uuids
#: the model has never been shown. Under the strict `json_schema` response format
#: the UNDERSTAND_VALUE binding uses, that left exactly one compliant answer:
#: the empty array. So `slot_updates` came back `[]` on every real call, the
#: writeback had nothing to write, and the `complete` exit gate stayed
#: unreachable outside fixtures -- registered as T34 and confirmed against a
#: real provider on 2026-09-03.
#:
#: Both fields are identity, not semantics, and this module's rule is that the
#: platform supplies identity: the frame is the one the model was asked about,
#: and the evidence event is the envelope being processed. Removing them from
#: what the model is asked for is the same move already made for `event_id` and
#: `model_evidence` above; `understand()` fills them back in before validation,
#: so the stored document still satisfies the full contract (RFC `:325`).
_PLATFORM_OWNED_ITEM_FIELDS = {
    "understanding-result": {"slot_updates": {"frame_id", "evidence_event_id"}},
}


@cache
def model_output_schema(name: str) -> dict[str, Any]:
    """JSON Schema sent to the model, excluding platform-owned identity fields."""

    schema = deepcopy(load_inference_schema(name))
    schema.pop("$schema", None)
    schema.pop("$id", None)
    owned = _PLATFORM_OWNED_OUTPUT_FIELDS[name]
    schema["required"] = [field for field in schema["required"] if field not in owned]
    for field in owned:
        schema["properties"].pop(field, None)
    for array_field, item_fields in _PLATFORM_OWNED_ITEM_FIELDS.get(name, {}).items():
        items = schema["properties"][array_field]["items"]
        items["required"] = [field for field in items["required"] if field not in item_fields]
        for field in item_fields:
            items["properties"].pop(field, None)

    def inline_refs(value: Any) -> Any:
        if isinstance(value, list):
            return [inline_refs(item) for item in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if ref:
            target = next(
                (
                    load_inference_schema(candidate)
                    for candidate in INFERENCE_CONTRACT_NAMES
                    if load_inference_schema(candidate).get("$id") == ref
                ),
                None,
            )
            if target is None:
                raise KeyError(f"unresolved inference schema ref: {ref}")
            resolved = deepcopy(target)
            resolved.pop("$schema", None)
            resolved.pop("$id", None)
            return inline_refs(resolved)
        return {key: inline_refs(item) for key, item in value.items()}

    return cast(dict[str, Any], inline_refs(schema))
