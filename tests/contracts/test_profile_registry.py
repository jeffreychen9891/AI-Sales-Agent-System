"""Registry rules that hold without a database.

The one this file exists for is the last test: the resolver has no fallback.
The legacy resolver answered an unknown merchant with a default industry
profile; `gen2-program-plan.md` §6 requires that behaviour to be dropped, and a
missing fallback is invisible unless something asserts its absence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles import (
    ProfileNotFound,
    ProfilePackageError,
    ProfileRegistry,
    canonical_hash,
)
from fastagent_v2.profiles.registry import TRUSTED_ROOT, load_package

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "profiles"


def _document() -> dict[str, Any]:
    return json.loads(
        (PACKAGE_ROOT / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def _write(root: Path, name: str, document: dict[str, Any]) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _restamp(document: dict[str, Any]) -> dict[str, Any]:
    document["identity"]["content_hash"] = canonical_hash(document)
    return document



@pytest.fixture()
def scratch_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Treat `tmp_path` as the repository for tests about package *content*.

    The trusted-root boundary (P1-1) refuses any registry root outside the
    repository, which is the point -- but it would also refuse every scratch
    directory these tests build. Repointing the boundary rather than weakening
    it keeps the two concerns separate: this fixture's users test collision and
    provenance logic, while `test_a_registry_root_outside_the_repository_is_refused`
    tests the boundary itself against the real constant.
    """

    import fastagent_v2.profiles.registry as registry_module

    monkeypatch.setattr(registry_module, "TRUSTED_ROOT", tmp_path.resolve())
    return tmp_path

def test_canonical_hash_excludes_only_the_hash_field_and_covers_the_release_id() -> None:
    document = _document()
    baseline = canonical_hash(document)

    without_hash = copy.deepcopy(document)
    without_hash["identity"].pop("content_hash")
    assert canonical_hash(without_hash) == baseline

    repinned = copy.deepcopy(document)
    repinned["identity"]["release_id"] = "22222222-2222-4222-8222-222222222222"
    assert canonical_hash(repinned) != baseline

    reordered = dict(reversed(list(document.items())))
    assert canonical_hash(reordered) == baseline


def test_canonical_hash_is_pinned_to_utf8_not_ascii_escapes() -> None:
    """The rule is sha256 over UTF-8 bytes, not over backslash-u escapes.

    Today's profiles are ASCII, so both spellings agree and the choice would
    drift unnoticed. Profiles carry Chinese customer-facing material, so this
    fixes the rule before the first one arrives.
    """

    document: dict[str, Any] = {"material": "你好"}
    escaped = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    utf8 = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert escaped != utf8

    import hashlib

    assert canonical_hash(document) == hashlib.sha256(utf8).hexdigest()


def test_package_with_a_stale_declared_hash_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["proactivity_policy"]["active_window_seconds"] = 900
    path = _write(tmp_path, "drifted", document)
    with pytest.raises(ProfilePackageError, match="content hash does not match"):
        load_package(path)


def test_package_that_fails_the_contract_is_refused(tmp_path: Path) -> None:
    document = _document()
    document.pop("strategies")
    path = _write(tmp_path, "incomplete", _restamp(document))
    with pytest.raises(ProfilePackageError, match="merchant-profile contract"):
        load_package(path)


def test_a_package_declaring_a_platform_frame_is_refused(tmp_path: Path) -> None:
    """**The reserved namespace** (B4-7, ADR-0099).

    The platform owns `platform:`. `frame_definitions` resolves those ids for
    every conversation whether the profile mentions them or not, and it writes
    the platform entry last -- so a profile frame carrying such an id is a
    definition that can never be reached, and the profile would be describing
    behaviour the tenant then does not get.

    Refused at admission for the same reason as the two cases below it: the
    alternative is finding out on the turn a live conversation's root goal
    completes, which is where ADR-0031 already put one bad surprise.
    """

    document = _document()
    document["target_frames"].append(
        {
            **document["target_frames"][0],
            "id": "platform:no_goal",
        }
    )
    path = _write(tmp_path, "shadows-a-platform-frame", _restamp(document))
    with pytest.raises(ProfilePackageError, match="reserved"):
        load_package(path)


def test_package_with_a_dangling_closing_frame_ref_is_refused(tmp_path: Path) -> None:
    """ADR-0031's reference has to resolve inside its own document.

    JSON Schema can say "a string"; it cannot say "one of the ids in this same
    document". Left to runtime, a dangling reference would not fail until a live
    conversation completed its root goal and the pipeline reached for a frame the
    profile does not define -- the worst possible moment to find out.
    """

    document = _document()
    document["closing_frame_ref"] = "no_such_frame"
    path = _write(tmp_path, "dangling-closing-ref", _restamp(document))
    with pytest.raises(ProfilePackageError, match="closing_frame_ref"):
        load_package(path)


def test_package_whose_closing_frame_is_its_own_root_is_refused(tmp_path: Path) -> None:
    """A self-referential closing frame turns the feature off with no signal.

    `should_enter_closing_frame` refuses to push a closing frame when the stack
    is already in one, and the root frame *is* the first definition -- so this
    configuration validates, publishes, and then silently never closes a single
    conversation (independent review 2026-09-03, P3-3). Refusing at load time is
    what makes the misconfiguration loud.
    """

    document = _document()
    document["closing_frame_ref"] = document["target_frames"][0]["id"]
    path = _write(tmp_path, "self-referential-closing-ref", _restamp(document))
    with pytest.raises(ProfilePackageError, match="own root target frame"):
        load_package(path)


def test_package_with_two_rules_of_one_kind_on_one_frame_is_refused(tmp_path: Path) -> None:
    """They render the same dedupe key, so the second could never produce a timer.

    `dedupe_template` is pinned to `{emit_kind}:{target_frame_id}` and the frame
    id is the same for every rule of a frame, so two rules of one frame with the
    same `emit_kind` always collide -- and the collision is **absorbed** by the
    unique key rather than raised: the second rule silently never fires.

    **Refused here rather than at runtime** (independent re-review 2026-09-09,
    P1-1). The producer refuses too, but it runs inside the dispatcher's
    accounting transaction, *after* the message has left; an exception there
    rolls a completed delivery back into `SENDING` and the next claim sends it to
    the customer again. A profile that cannot work should not be publishable.
    """

    document = _document()
    frame = document["target_frames"][0]
    rule = dict(frame["time_rules"][0])
    twin = dict(rule)
    twin["id"] = f"{rule['id']}-twin"
    twin["due"] = {**rule["due"], "offset_seconds": int(rule["due"]["offset_seconds"]) + 30}
    frame["time_rules"] = [rule, twin]
    path = _write(tmp_path, "two-rules-one-kind", _restamp(document))

    with pytest.raises(ProfilePackageError, match="two time rules of kind"):
        load_package(path)


def test_package_with_one_rule_id_on_two_frames_is_refused(tmp_path: Path) -> None:
    """A rule id is a lookup key across the whole document (B4-4d review P2-1).

    B4-4d gave `time_rules[].strategy` its first production reader, and a
    pending timer carries only `payload_ref = {"rule_id": …}` -- so
    `strategy_for_rule` scans **every** frame for that id. Nothing else on the
    timer says which frame armed it, so the lookup cannot be scoped; the id has
    to carry the uniqueness instead.

    **The failure this refuses is not fail-closed**, which is why it is refused
    rather than tolerated: with two frames sharing an id, the first one found
    binds the other frame's timer, and if the firing frame's
    `time_rule_strategy_set` happens to contain that strategy the plan passes
    the validator and the customer receives a play the rule that armed the timer
    never declared. **Refused at admission** for the same reason as the
    collision above -- the alternative is a check on the send path, where the
    failure is a message already gone.

    The two ids differ by frame prefix in both shipped profiles, but that is a
    convention: the schema asks only for `minLength: 1`.
    """

    document = _document()
    first, second = document["target_frames"][0], document["target_frames"][1]
    borrowed = dict(second["time_rules"][0])
    borrowed["id"] = str(first["time_rules"][0]["id"])
    second["time_rules"] = [borrowed]
    path = _write(tmp_path, "one-rule-id-two-frames", _restamp(document))

    with pytest.raises(ProfilePackageError, match="is declared twice"):
        load_package(path)


def test_the_shipped_profiles_do_not_repeat_a_time_rule_id() -> None:
    """The premise `strategy_for_rule`'s document-wide scan depends on.

    `load_package` refuses a repeat, so this is not the enforcement -- it is the
    statement that the profiles we ship actually satisfy the premise, asserted
    where a reader of `strategy_for_rule` will look for it.
    """

    for name in ("synthetic-carrier", "synthetic-third"):
        document = json.loads((PACKAGE_ROOT / name / "profile.json").read_text(encoding="utf-8"))
        ids = [
            str(rule["id"])
            for frame in document.get("target_frames", [])
            for rule in frame.get("time_rules", [])
        ]
        assert len(ids) == len(set(ids)), f"{name}: {ids}"


def test_the_shipped_profile_declares_a_closing_frame_that_resolves() -> None:
    """The synthetic profile is what every test and the T11 probe run against.

    If it stopped declaring a closing frame, the ADR-0031 path would quietly stop
    being exercised anywhere and the suite would still be green.
    """

    document = _document()
    reference = document["closing_frame_ref"]
    assert reference in {frame["id"] for frame in document["target_frames"]}


def test_unreadable_and_malformed_packages_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "profile.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProfilePackageError, match="not valid JSON"):
        load_package(directory / "profile.json")
    with pytest.raises(ProfilePackageError, match="unreadable"):
        load_package(tmp_path / "absent" / "profile.json")


def test_colliding_packages_are_refused_rather_than_ordered(scratch_repository: Path) -> None:
    tmp_path = scratch_repository
    document = _document()
    _write(tmp_path, "first", document)

    # Same identity twice: the copy is not "the newer one", it is ambiguity.
    _write(tmp_path, "second", document)
    with pytest.raises(ProfilePackageError, match="claim synthetic-service-desk 1.0.4"):
        ProfileRegistry(root=tmp_path).packages()

    # A new version that forgot to take a new release id. This is the collision
    # that actually happens by hand, and the one the publisher would otherwise
    # discover only as a primary key violation.
    other = _document()
    other["identity"]["version"] = "1.0.5"
    # **三段引用语法落地之后改的**：这里只要一处“改过了”让哈希不同，
    # 而原先那个 `collection:changed` 是个随手写的两段值，封版时会被语法拒掉。
    other["knowledge_bindings"] = ["knowledge:changed-collection:v1"]
    _write(tmp_path, "second", _restamp(other))
    with pytest.raises(ProfilePackageError, match="claim release id"):
        ProfileRegistry(root=tmp_path).packages()

    other["identity"]["release_id"] = "33333333-3333-4333-8333-333333333333"
    _write(tmp_path, "second", _restamp(other))
    assert len(ProfileRegistry(root=tmp_path).packages()) == 2


def test_registry_records_provenance_relative_to_the_repository_root() -> None:
    registry = ProfileRegistry(root=PACKAGE_ROOT, relative_to=ROOT)
    package = registry.package(profile_id="synthetic-service-desk", version="1.0.4")
    assert package.source_path == "profiles/synthetic-third/profile.json"
    assert package.content_hash == _document()["identity"]["content_hash"]


def test_an_absent_root_is_an_error_not_an_empty_registry(scratch_repository: Path) -> None:
    with pytest.raises(ProfilePackageError, match="root does not exist"):
        ProfileRegistry(root=scratch_repository / "absent").packages()


def test_unknown_profile_never_falls_back_to_a_default(scratch_repository: Path) -> None:
    """The behaviour §6 (P4) requires to be removed, asserted as absent.

    The registry holds exactly one package here. A resolver with an industry
    default -- or one that shrugs and returns the only thing it has -- would
    answer all three of these calls; this one raises for every miss.
    """

    _write(scratch_repository, "only", _document())
    registry = ProfileRegistry(root=scratch_repository)
    assert registry.package(profile_id="synthetic-service-desk", version="1.0.4") is not None

    for profile_id, version in (
        ("unknown-merchant", "1.0.0"),
        ("synthetic-service-desk", "9.9.9"),
        ("", ""),
    ):
        with pytest.raises(ProfileNotFound, match="no trusted profile package"):
            registry.package(profile_id=profile_id, version=version)


def test_a_registry_root_outside_the_repository_is_refused(tmp_path: Path) -> None:
    """The trusted-package boundary, asserted rather than merely documented.

    Independent review 2026-09-02 (P1-1): the module docstring said the registry
    never reads an operator-supplied path outside the repository, but nothing
    enforced it -- the CLI's `--root` took any path. A profile carrying the
    merchant-specific words the birth gate forbids could be published from a
    scratch directory, so the enforcement is what makes the birth gate mean
    anything for published releases.
    """

    _write(tmp_path, "rogue", _document())
    assert TRUSTED_ROOT not in tmp_path.resolve().parents
    with pytest.raises(ProfilePackageError, match="outside the deployment"):
        ProfileRegistry(root=tmp_path).packages()
    with pytest.raises(ProfilePackageError, match="outside the deployment"):
        ProfileRegistry(root=tmp_path).package(
            profile_id="synthetic-service-desk", version="1.0.4"
        )
    # The in-repository root still works, so the guard bounds rather than blocks.
    assert ProfileRegistry(root=PACKAGE_ROOT, relative_to=ROOT).packages()


def test_a_package_outside_the_provenance_root_is_refused_not_recorded(
    tmp_path: Path,
) -> None:
    """`source_path` must never read repository-relative when it is not.

    Same finding, second half: `load_package` used to fall back to the absolute
    path when `relative_to` did not contain the package. The migration header
    calls `source_path` the answer to "which trusted package did this come
    from"; a silent fallback answers it wrongly in a format that looks right.
    """

    path = _write(tmp_path, "elsewhere", _document())
    with pytest.raises(ProfilePackageError, match="not under the provenance root"):
        load_package(path, relative_to=ROOT)
    # Without a provenance root there is nothing to lie about, so this is allowed.
    assert load_package(path).source_path.endswith("elsewhere/profile.json")


def test_an_unreadable_schema_is_not_reported_as_an_invalid_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment fault must not be diagnosed as a bad profile (P2-3).

    `validate_contract` finds the schema through `Path.cwd()`. Run the CLI from
    anywhere but the application root and every profile looks invalid, which
    sends the operator to debug the wrong artefact.
    """

    import fastagent_v2.profiles.registry as registry_module

    def _explode(_name: str, _value: dict[str, Any]) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(registry_module, "validate_contract", _explode)
    path = _write(tmp_path, "fine", _document())
    with pytest.raises(ProfilePackageError, match="schema could not be read"):
        load_package(path)


# --------------------------------------------------------------------------
# 裁定一 步骤 1：`strategies` 那一块有了形状（ADR-0137，2026-09-13）
# --------------------------------------------------------------------------
#
# 在此之前 schema 里 `strategies` 是 `additionalProperties: {"type": "object"}`
# —— **一个字段都没声明**：`required_slots` 可以是字符串、可以是数字、可以拼错成
# `required_slot`，封版闸一个字都不会说。而它是裁定一第 2 道闸的**数据源**
# （`STRATEGY_SKIPS_REQUIRED_SLOT` 读 `strategies[<选中策略>].required_slots`），
# 一个没有形状的数据源接上一道会拒计划的闸，等于把「商家写错了」变成「回合被拒」。
#
# **本步骤不收紧封闭性**：没有 `additionalProperties: false`、没有 `required`。
# 两者都会让一份今天合法的包变非法，而排期表把那件事划在本步骤之外
# （`required_slots` 缺省 = 这个策略不声明必填槽位，闸读到的是空列表）。


def test_a_strategy_declaring_required_slots_still_publishes(tmp_path: Path) -> None:
    """**正向那一半**：声明了必填槽位的策略，照样封得版。

    放宽 / 收紧一条判据时两侧都要验（`AGENTS.md`「放宽一条判据时，正反两侧都要验」）
    —— 这里加的是形状（收紧），所以正向这一条问的是
    「**我没有顺带把合法的包变成非法的**」。

    用中文槽位名不是装饰：`minLength: 1` 之外这一列没有 pattern，
    而槽位名在真实配包里是中文（`item_category` 那种英文 id 是合成包的写法）。
    """

    document = _document()
    # **槽位也要加进允许该策略的那个帧** —— §4.1 的封版闸（ADR-0137，同一批之后加的）
    # 要求 `required_slots` 的每一项在某个允许该策略的帧里真的存在。
    # 这条用例原来只改策略、不改帧，于是那道闸落地当天它就红了 ——
    # **而它红得对**：它当时声明的正是一个「永远填不上的必填槽位」。
    frame = next(
        f for f in document["target_frames"] if "advance_goal" in f["allowed_strategy_set"]
    )
    # **`type` 自 D2（2026-09-15）起是必填的封闭词表** —— 「预算」是金额，
    # 取 `money` 既诚实又让这条用例多覆盖一个枚举值（不是只覆盖 `string`）。
    # **`description` 自 ADR-0144 决策 3（2026-09-21）起也是必填的** ——
    # 一个只有名字和类型的槽，到模型那里是一个它得自己猜意思的词。
    frame["information_slots"].append(
        {
            "name": "预算",
            "type": "money",
            "required": False,
            "description": "他打算花多少。",
        }
    )
    document["strategies"]["advance_goal"]["required_slots"] = ["预算"]
    path = _write(tmp_path, "declares-required-slots", _restamp(document))

    package = load_package(path)

    assert package.document["strategies"]["advance_goal"]["required_slots"] == ["预算"]


def test_a_slot_type_outside_the_vocabulary_is_refused(tmp_path: Path) -> None:
    """**D2 的兑现物**：打错的槽位类型在封版期就被拒，而不是安静地到模型手里。

    ## 为什么这一条比「有没有 type 这个键」要紧

    `information_slots` 在 2026-09-15 之前是裸 `{"type": "object"}`
    （ADR-0032 点名、T28 登记）—— 于是 `{"type": "bool"}` 这种打错的值
    **admission 照收**，然后由 `runtime.request_shape` 原样放进 `slots_wanted` 送给模型。
    模型不认识 `bool`，只能猜，而**猜出来的是一个形状不对却看起来正常的值** ——
    `CLAUDE.md` 第三节那条「传错了不报错，而是安静地给出一个看起来正常的结果」。

    **所以这条闸真正拦的不是「缺了一个键」，是「写了一个没人认识的类型」。**
    词表是今天在架的四个（`string` x9 / `boolean` x6 / `money` x1 / `date` x1）；
    加第五个是一次有意的契约改动，**而那正是它该有的代价** ——
    新类型改变的是「模型被要求产出什么」。
    """

    document = _document()
    frame = next(
        f for f in document["target_frames"] if "advance_goal" in f["allowed_strategy_set"]
    )
    # `bool` 是 `boolean` 最像的那个打错法 —— 人看得懂，模型看不懂。
    frame["information_slots"].append({"name": "要发票吗", "type": "bool", "required": False})
    path = _write(tmp_path, "typo-slot-type", _restamp(document))

    with pytest.raises(ProfilePackageError) as refused:
        load_package(path)
    assert "information_slots" in str(refused.value) or "type" in str(refused.value), (
        "报错要说得出是哪一处 —— 配包方手里只有这条消息"
    )


def test_a_required_slots_written_as_a_string_is_refused_by_name(tmp_path: Path) -> None:
    """**反向那一半**：写成字符串就被拒，而且报错点名这个键。

    `"预算"` 与 `["预算"]` 在 JSON 里差一对方括号，而下游读它的是
    `for slot in strategy["required_slots"]` —— **一个字符串会被逐字符迭代**，
    于是「必填槽位」变成「必填的『预』『算』两个槽位」，两个都永远填不上，
    每一轮计划都被拒。**这正是一个没有形状的数据源最坏的失败方式**：
    它不报错，它换了个意思。

    「报错点名这个键」是验收的一部分：`ProfilePackageError` 的文本要能让人
    直接找到是哪一个策略的哪一个字段，而不是只说「合同没过」。
    """

    document = _document()
    document["strategies"]["advance_goal"]["required_slots"] = "预算"
    path = _write(tmp_path, "required-slots-as-string", _restamp(document))

    with pytest.raises(ProfilePackageError) as raised:
        load_package(path)

    message = str(raised.value)
    assert "merchant-profile contract" in message
    assert "required_slots" in message, (
        "报错必须点名这个键 —— 否则商家只知道『合同没过』，不知道哪里没过"
    )


def test_the_other_two_strategy_fields_are_shaped_too(tmp_path: Path) -> None:
    """三个键一起补的形状，三个键一起验。

    `allowed_intent_kinds` 与 `generation_constraints` 的下游读法与
    `required_slots` 同形（都在 `inference_validation` 里被当列表迭代），
    所以它们有同一个失败面。**只给被裁定点名的那一个补形状，
    是把另外两个留在原地等下一次**。
    """

    for field in ("allowed_intent_kinds", "generation_constraints"):
        document = _document()
        document["strategies"]["advance_goal"][field] = "send_message"
        path = _write(tmp_path, f"bad-{field}", _restamp(document))

        with pytest.raises(ProfilePackageError) as raised:
            load_package(path)

        assert field in str(raised.value), f"报错没点名 {field}"


# --------------------------------------------------------------------------
# §4.1 封版闸：`required_slots` 里的槽位必须在某个帧里存在（ADR-0137，取紧）
# --------------------------------------------------------------------------
#
# **它排在裁定一步骤 3 之前，是用户 2026-09-13 定的顺序**：闸没建之前不上那道
# `post_plan` 的必填槽位闸，因为第一个拼错槽位名的配包会变成一次很难查的事故 ——
# 症状是某条打法从此再也不生效，而日志里全是模型的问题。


def test_a_required_slot_no_frame_defines_is_refused_at_admission(tmp_path: Path) -> None:
    """**拼错一个字，封版就拒 —— 而不是等到每一轮都被拒。**

    `welcome` 帧允许 `advance_goal`，该帧的槽位是 `request_summary`。
    把策略的 `required_slots` 写成 `request_summar`（少一个字母）之后：
    那个槽位在任何帧里都不存在，于是永远为空，于是
    `STRATEGY_SKIPS_REQUIRED_SLOT` 会**每一轮**拒掉这条策略，
    而理由码说的是「跳过了必填槽位」——**指向模型，不指向那个拼错的字**。

    报错要点名策略与那个槽位，并说出「加进帧的 `information_slots`，或改名字」。
    """

    document = _document()
    document["strategies"]["advance_goal"]["required_slots"] = ["request_summar"]
    path = _write(tmp_path, "typo-slot", _restamp(document))

    with pytest.raises(ProfilePackageError) as raised:
        load_package(path)

    message = str(raised.value)
    assert "advance_goal" in message, "报错要点名是哪条策略"
    assert "request_summar" in message, "报错要点名是哪个槽位"
    assert "information_slots" in message, "报错要说出怎么改"


def test_a_required_slot_defined_by_the_frame_that_allows_it_publishes(tmp_path: Path) -> None:
    """正向那一半：槽位真的在**允许这条策略的那个帧**里，就放行。

    判据是「允许它的某个帧」而不是「所有帧」—— 槽位是帧的属性，
    一个策略可以被多个帧允许，而它要的槽位只需要在**能选中它的**帧里存在。
    """

    document = _document()
    frame = next(
        f for f in document["target_frames"] if "advance_goal" in f["allowed_strategy_set"]
    )
    existing = frame["information_slots"][0]["name"]
    document["strategies"]["advance_goal"]["required_slots"] = [existing]
    path = _write(tmp_path, "slot-in-the-allowing-frame", _restamp(document))

    package = load_package(path)

    assert package.document["strategies"]["advance_goal"]["required_slots"] == [existing]


def test_a_slot_defined_only_by_a_frame_that_forbids_the_strategy_is_refused(
    tmp_path: Path,
) -> None:
    """**同名槽位存在，但存在的地方选不中这条策略 —— 仍然拒。**

    这是「允许它的某个帧」那个判据的**反面**，也是本闸唯一不显然的地方：
    槽位名在文档里**确实出现过**，所以一条「这个名字在本文件里存在吗」的检查会放行，
    而运行期仍然每一轮都拒 —— 那个帧从来不会选中这条策略。
    **「文档里有这个名字」与「这条策略拿得到这个槽位」是两件事。**

    实测夹具：`clarify_request` 帧不允许 `advance_goal`，它的槽位是 `routing_detail`。
    """

    document = _document()
    forbidding = next(
        f for f in document["target_frames"] if "advance_goal" not in f["allowed_strategy_set"]
    )
    borrowed = forbidding["information_slots"][0]["name"]
    allowing = [
        f for f in document["target_frames"] if "advance_goal" in f["allowed_strategy_set"]
    ]
    assert allowing, "夹具前提：至少有一个帧允许 advance_goal"
    for frame in allowing:
        assert borrowed not in {slot["name"] for slot in frame["information_slots"]}, (
            "夹具前提：借来的槽位不在任何允许该策略的帧里"
        )
    document["strategies"]["advance_goal"]["required_slots"] = [borrowed]
    path = _write(tmp_path, "slot-in-the-wrong-frame", _restamp(document))

    with pytest.raises(ProfilePackageError, match=borrowed):
        load_package(path)


def test_a_strategy_no_frame_allows_is_not_judged_by_this_gate(tmp_path: Path) -> None:
    """**一个帧都不允许的策略，本闸不判 —— 这是有意的。**

    那种策略今天选不中（`validate_plan` 按帧的 `allowed_strategy_set` 准入），
    它的 `required_slots` 是一份没人会读的声明。报它会把人引向一个与本闸无关的
    问题（「这个策略为什么没有帧允许」）—— 那是零调用方那两问的事，不是本闸的。

    **夹具里现成就有一个**：`synthetic-third` 注册了 `propose_followup`，
    而三个帧的 `allowed_strategy_set` 里都没有它（实测）。
    用现成的那个而不是造一个，是因为**它证明这种形状真的存在于仓里**，
    不是一个为用例发明的情形。

    **写成用例而不是注释**，因为「有意不判」与「漏了判」在代码里长得一样。
    """

    document = _document()
    orphan = "propose_followup"
    assert orphan in document["strategies"], "夹具前提：这个策略注册过"
    for frame in document["target_frames"]:
        assert orphan not in frame["allowed_strategy_set"], (
            f"夹具变了：现在有帧允许 {orphan} 了，这条用例要换一个孤儿策略"
        )
    document["strategies"][orphan]["required_slots"] = ["谁都没有这个槽位"]
    path = _write(tmp_path, "orphan-strategy", _restamp(document))

    package = load_package(path)

    assert package.document["strategies"][orphan]["required_slots"] == ["谁都没有这个槽位"]
