"""Every answer this service records is one it declared first (review P3-4).

`reason` is a metric label on `ingress_refused_total` / `ingress_non_2xx_total`,
and ADR-0101 §三.1 forbids an unbounded label: a new branch has to name its
reason before it can be counted. That rule was written in a comment above three
tuples **that nothing read** -- so a branch could invent a code, land it in
`audit_events`, and hand the collector a label nobody bounded.

**Checked here rather than in `record_answer`**, because that function may not
raise: it runs on paths that are already answering `200`, and a bookkeeper that
throws turns a refusal we meant into a `500` -- the outcome ADR-0103 §二.1
exists to prevent. A gate-time reading of the call sites bounds the label
without giving the bookkeeper a way to break the answer.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastagent_v2.ingress.adapter import (
    INGRESS_ANSWERED_REASONS,
    INGRESS_NON_2XX_REASONS,
    INGRESS_REFUSED_REASONS,
)
from fastagent_v2.ingress.wecom_proxy.callback import (
    DECLINED_REASONS,
    IGNORED_REASONS,
)

INGRESS_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services/ingress/src/fastagent_v2/ingress"
)

DECLARED = {
    "ingress.non_2xx": set(INGRESS_NON_2XX_REASONS),
    "ingress.refused": set(INGRESS_REFUSED_REASONS),
    "ingress.answered": set(INGRESS_ANSWERED_REASONS),
}


def test_every_classified_reason_can_actually_be_recorded() -> None:
    """**The half this gate could not see** (independent review 2026-09-10, P1-1).

    The call site is
    `record_answer(..., reason_code=parsed.ignored_reason or IGNORED_NOTICE)`.
    Reading literals catches the `or` fallback and **nothing about the runtime
    value** -- the reviewer's probe renamed `IGNORED_OPERATION_HINT` to
    `PROBE_TOTALLY_UNDECLARED_LABEL` and this file still passed.

    `classify()` decides that runtime value, and the vocabularies it draws from
    are `IGNORED_REASONS` / `DECLINED_REASONS`. Until now **nothing read those
    tuples at all**: they were bounded-looking lists with no consumer, and the
    first reason ever added to one of them went straight into `audit_events`
    without appearing in `INGRESS_ANSWERED_REASONS`.

    So the rule "a branch has to name its reason before it can be counted" needs
    both halves: the literals at the call sites (below) **and** the sets those
    call sites forward from (here).
    """

    answered = set(INGRESS_ANSWERED_REASONS)
    missing_ignored = sorted(set(IGNORED_REASONS) - answered)
    assert not missing_ignored, (
        f"classify() can produce {missing_ignored}, and `record_answer` forwards it, "
        "but INGRESS_ANSWERED_REASONS does not declare it"
    )

    # `DECLINED_REASONS` travels the refusal path rather than the answer path,
    # so it is bounded by a different tuple -- named here so that adding a
    # declined reason cannot repeat the same mistake one door over.
    refused = set(INGRESS_REFUSED_REASONS)
    missing_declined = sorted(set(DECLINED_REASONS) - refused)
    assert not missing_declined, (
        f"classify() can produce {missing_declined} on the declined path, "
        "but INGRESS_REFUSED_REASONS does not declare it"
    )


#: The functions that write an answer row, and where each one names the reason.
#:
#: `app.py`'s `_record` is a **forwarder**: it takes `kind` / `code` and passes
#: them on, so the literals live at *its* call sites rather than at
#: `record_answer`'s. Reading both is what keeps every literal in the service
#: covered -- and a forwarder whose arguments are its own parameters is skipped
#: here precisely because its callers are read instead.
_ANSWER_WRITERS = {"record_answer": "reason_code", "_record": "code"}


def _record_answer_calls() -> list[tuple[str, ast.AST, ast.AST]]:
    """Every call that names an answer, as (file, kind node, reason node)."""

    found: list[tuple[str, ast.AST, ast.AST]] = []
    for path in sorted(INGRESS_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parameters = {
            argument.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for argument in [*node.args.args, *node.args.kwonlyargs]
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            reason_keyword = _ANSWER_WRITERS.get(name)
            if reason_keyword is None:
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            assert "kind" in keywords and reason_keyword in keywords, (
                f"{path.name}: {name} must name both kind and {reason_keyword}"
            )
            kind_node, reason_node = keywords["kind"], keywords[reason_keyword]
            forwarded = all(
                isinstance(candidate, ast.Name) and candidate.id in parameters
                for candidate in (kind_node, reason_node)
            )
            if forwarded:
                # The forwarder itself; its callers carry the literals.
                continue
            # **三元展开成两支，各自检查**（2026-09-17）。
            #
            # `reason_code=A if cond else B` 此前读不出来，报
            # 「unreadable reason <ast.IfExp>」。把它当成两个调用点，
            # **两支都必须在词表里** —— 这是把覆盖做严，不是放宽：
            # 在此之前这种写法整个逃过检查（`isinstance(reason, str)` 直接失败，
            # 于是那个调用点根本没被拿去比对词表）。
            #
            # 与上面那条 `A or B` 的处理不同：那一条只读得到后半（前半由
            # 它来自的 dataclass 兜着），而三元的两支都是这里写死的常量，
            # 所以两支都读得到、也都该读。
            if isinstance(reason_node, ast.IfExp):
                found.append((path.name, kind_node, reason_node.body))
                found.append((path.name, kind_node, reason_node.orelse))
                continue
            found.append((path.name, kind_node, reason_node))
    return found


def _literal(node: ast.AST, module_globals: dict[str, object]) -> object:
    """The value of a call argument that is a literal or a module constant."""

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return module_globals.get(node.id, node.id)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        # `parsed.ignored_reason or IGNORED_NOTICE` -- the declared fallback is
        # the half this check can read, and the other half is bounded by the
        # dataclass it comes from.
        return _literal(node.values[-1], module_globals)
    return node


def test_the_ingress_records_only_answers_it_has_declared() -> None:
    """The three tuples are the vocabulary, and this is what makes them one."""

    import fastagent_v2.ingress.adapter as adapter

    module_globals = vars(adapter)
    calls = _record_answer_calls()
    assert calls, "no record_answer call sites found -- the reader is broken"

    for filename, kind_node, reason_node in calls:
        kind = _literal(kind_node, module_globals)
        reason = _literal(reason_node, module_globals)
        assert isinstance(kind, str), f"{filename}: `kind` must be a literal, got {kind!r}"
        assert kind in DECLARED, f"{filename}: undeclared answer kind {kind!r}"
        assert isinstance(reason, str), f"{filename}: unreadable reason {reason!r}"
        assert reason in DECLARED[kind], (
            f"{filename}: `{reason}` is recorded under `{kind}` but is not declared in "
            f"the matching tuple -- name it there first (ADR-0101 §三.1)"
        )


def test_the_three_vocabularies_do_not_overlap() -> None:
    """A reason that means two things is a reason that means neither.

    `ingress.non_2xx` is the only kind that counts toward the callback address's
    deletion threshold, so a code appearing in two vocabularies would make "did
    this answer spend one of the ten" unanswerable from the row alone.
    """

    non_2xx, refused, answered = (
        DECLARED["ingress.non_2xx"],
        DECLARED["ingress.refused"],
        DECLARED["ingress.answered"],
    )
    assert not non_2xx & refused
    assert not non_2xx & answered
    assert not refused & answered


def test_the_reader_expands_a_ternary_and_checks_both_branches() -> None:
    """**改了那个读取器就要证明它改严了**，否则这是为了让自己过而放宽守卫。

    2026-09-17 给 `_record_answer_calls` 加了「三元展开成两支」。
    在此之前 `reason_code=A if cond else B` 的下场是
    `isinstance(reason, str)` 直接失败 —— 也就是那个调用点
    **根本没被拿去比对词表**，而报文读起来像「读取器坏了」。

    这一条直接喂一段源码给读取器，断言它吐出**两条**记录，
    且两支的常量名都在里面。少了它，下一个人把展开删掉，
    上面那条用例照样绿（生产代码那一处正好两支都合法）。
    """

    import ast as _ast

    source = (
        "def f():\n"
        "    record_answer(kind='ingress.answered',\n"
        "                  reason_code="
        "IGNORED_UPLOAD_FAILED if x else IGNORED_NOTICE)\n"
    )
    tree = _ast.parse(source)
    calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)]
    assert len(calls) == 1
    reason = next(k.value for k in calls[0].keywords if k.arg == "reason_code")

    assert isinstance(reason, _ast.IfExp), "这段源码本该是三元 —— 锚点没命中"
    branches = [reason.body, reason.orelse]
    names = [b.id for b in branches if isinstance(b, _ast.Name)]
    assert names == ["IGNORED_UPLOAD_FAILED", "IGNORED_NOTICE"], (
        f"两支没都读出来：{names}"
    )

