"""The words behind `content_materials` come from the package, or not at all.

`target_frames[].content_materials` has named materials since the first profile
was written, and nothing has ever held their text. This file is about the file
that now does: `materials.json`, beside `profile.json`, merged into the document
at admission and covered by the same `identity.content_hash`.

**The fixture is a snapshot, on purpose.** `tests/fixtures/
internal-demo-materials.json` holds the material text of the internal demo
tenant as it stood on 2026-09-10, copied out of
`docs/handoffs/internal-demo-profile-2026-09-10/materials.md`. That document is
being edited in another window while this mechanism is built; a test that read
it live would go red because somebody rewrote a sentence, which is not a
statement about this code. **Refreshed 2026-09-10 to all 29 segments** the
material document then carried.
`material:demo-closing:v1` -- declared by the `closing` frame -- **still has no
text there**, which is exactly the state `_check_material_coverage` refuses;
sealing the demo package has to supply it first. (`material:demo-return:v1` is a
`return_confirmation.tone_material_ref`, not a `content_materials` entry, and is
inert for a different reason: nothing reads that field at all.)
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles import ProfilePackageError, canonical_hash
from fastagent_v2.profiles.registry import (
    MATERIAL_TEXTS_KEY,
    MATERIALS_FILENAME,
    load_package,
    request_floor_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "profiles"
DEMO_MATERIALS: dict[str, str] = json.loads(
    (ROOT / "tests" / "fixtures" / "internal-demo-materials.json").read_text(encoding="utf-8")
)["materials"]


def _base_document() -> dict[str, Any]:
    return json.loads(
        (PACKAGE_ROOT / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def _seal(
    directory: Path,
    document: dict[str, Any],
    materials: dict[str, str] | None,
) -> Path:
    """Write a package the way an author has to write one, and stamp it.

    **The stamp is taken over the merged document**, which is the one authoring
    rule this mechanism adds: `canonical_hash(profile.json + materials.json)`.
    Doing it here rather than in each test is not tidiness -- it is the only
    way these tests can claim a package is sealed the way `load_package`
    expects, instead of the way this file happens to believe.
    """

    directory.mkdir(parents=True, exist_ok=True)
    merged = copy.deepcopy(document)
    if materials is not None:
        merged[MATERIAL_TEXTS_KEY] = dict(materials)
        (directory / MATERIALS_FILENAME).write_text(
            json.dumps({"materials": materials}, ensure_ascii=False), encoding="utf-8"
        )
    document = copy.deepcopy(document)
    document["identity"]["content_hash"] = canonical_hash(merged)
    path = directory / "profile.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _with_materials(document: dict[str, Any], refs: list[str]) -> dict[str, Any]:
    """Point every frame at `refs`, so the coverage rule has something to check."""

    document = copy.deepcopy(document)
    for frame in document["target_frames"]:
        frame["content_materials"] = list(refs)
    return document


@pytest.fixture()
def scratch_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import fastagent_v2.profiles.registry as registry_module

    monkeypatch.setattr(registry_module, "TRUSTED_ROOT", tmp_path.resolve())
    return tmp_path


def test_a_package_without_a_materials_file_loads_exactly_as_before(
    scratch_repository: Path,
) -> None:
    """**The compatibility half, and it is the one that had to be true first.**

    Every profile published today ships no materials file. If adopting this
    mechanism were a flag day, the choice would be between rewriting every
    package at once and leaving the generation step blind -- so a package
    without the file must load, and must carry no material key at all.
    """

    path = _seal(scratch_repository / "plain", _base_document(), materials=None)

    package = load_package(path)

    assert MATERIAL_TEXTS_KEY not in package.document


def test_the_text_reaches_the_document_and_the_hash_covers_it(
    scratch_repository: Path,
) -> None:
    refs = sorted(DEMO_MATERIALS)[:3]
    materials = {ref: DEMO_MATERIALS[ref] for ref in refs}
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "sealed", document, materials)

    package = load_package(path)

    assert package.document[MATERIAL_TEXTS_KEY] == materials

    # **Editing one sentence invalidates the stamp.** This is the property that
    # makes the sibling file trusted rather than merely nearby: a reviewer signed
    # off on the words, so words that changed since are words nobody reviewed.
    (path.parent / MATERIALS_FILENAME).write_text(
        json.dumps({"materials": materials | {refs[0]: "改写过的说法"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ProfilePackageError, match="content hash does not match"):
        load_package(path)


def test_the_hash_error_names_both_files_when_a_materials_file_is_in_play(
    scratch_repository: Path,
) -> None:
    """A hash stamped in one file and invalidated by an edit in another is
    baffling unless the message says so."""

    refs = sorted(DEMO_MATERIALS)[:2]
    materials = {ref: DEMO_MATERIALS[ref] for ref in refs}
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "restamp", document, materials)
    (path.parent / MATERIALS_FILENAME).write_text(
        json.dumps({"materials": materials | {refs[1]: "另一种说法"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ProfilePackageError) as error:
        load_package(path)

    assert MATERIALS_FILENAME in str(error.value)
    assert "restamping" in str(error.value)


def test_a_declared_material_with_no_text_is_refused(scratch_repository: Path) -> None:
    """The defect the whole mechanism exists to remove, in one package.

    A frame naming a material the generation step cannot read hands the model a
    name and no words, and a model asked for words it does not have writes its
    own -- which is what「模型不得现场编造」forbids.
    """

    refs = sorted(DEMO_MATERIALS)[:3]
    materials = {ref: DEMO_MATERIALS[ref] for ref in refs[:2]}
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "incomplete", document, materials)

    with pytest.raises(ProfilePackageError, match="no text in"):
        load_package(path)


def test_a_text_that_no_frame_references_is_refused(scratch_repository: Path) -> None:
    """`AGENTS.md`'s zero-caller check, applied to prose.

    An unreferenced material reads as "this is in play" to whoever edits the
    file, while no frame can ever put it in front of the model.
    """

    refs = sorted(DEMO_MATERIALS)[:2]
    materials = {ref: DEMO_MATERIALS[ref] for ref in sorted(DEMO_MATERIALS)[:3]}
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "orphan", document, materials)

    with pytest.raises(ProfilePackageError, match="no frame declares"):
        load_package(path)


def test_a_profile_that_writes_the_merged_key_itself_is_refused(
    scratch_repository: Path,
) -> None:
    """Two sources for one key is a document whose words depend on load order."""

    refs = sorted(DEMO_MATERIALS)[:1]
    document = _with_materials(_base_document(), refs)
    document[MATERIAL_TEXTS_KEY] = {refs[0]: "写在 profile.json 里的正文"}
    directory = scratch_repository / "self-declared"
    directory.mkdir(parents=True, exist_ok=True)
    document["identity"]["content_hash"] = canonical_hash(document)
    path = directory / "profile.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ProfilePackageError, match="declares 'content_material_texts' itself"):
        load_package(path)


def test_a_material_with_blank_text_is_refused(scratch_repository: Path) -> None:
    refs = sorted(DEMO_MATERIALS)[:1]
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "blank", document, {refs[0]: "   "})

    with pytest.raises(ProfilePackageError, match="has no text"):
        load_package(path)


def test_a_materials_file_without_a_materials_object_is_refused(
    scratch_repository: Path,
) -> None:
    refs = sorted(DEMO_MATERIALS)[:1]
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "shapeless", document, {refs[0]: "正文"})
    (path.parent / MATERIALS_FILENAME).write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )

    with pytest.raises(ProfilePackageError, match="no `materials` object"):
        load_package(path)


def test_the_demo_segments_seal_into_a_package_that_loads(
    scratch_repository: Path,
) -> None:
    """The snapshot of the real material, sealed the way the demo package will be.

    Not a smoke test: it is the only case here where the text is prose somebody
    actually wrote for customers -- multi-line, punctuated, non-ASCII -- rather
    than a string chosen to make an assertion pass. `canonical_hash` pins
    `ensure_ascii=False` precisely for this, and a package of ASCII fixtures
    would never have exercised it.
    """

    refs = sorted(DEMO_MATERIALS)
    document = _with_materials(_base_document(), refs)
    # **夹具分了阶段，因为真包分了阶段**（2026-09-11）。原来它把全部素材堆进一个
    # 不声明阶段的帧——**11,450 字节，预算 6,132**——而那个形状当时能封版，
    # 正是「帧不声明阶段就整条闸跳过」那个缺陷本身。补上闸之后它变红了，红得对：
    # 夹具体现的是被修掉的那个形状。
    #
    # **改的是夹具不是判据**，改完还更贴近这条用例自己的说法（「sealed the way the
    # demo package will be」）——真包现在是 19 个串行子桶。这里用四个、素材轮流派、
    # `after` 链到上一桶的第一段：够证明 `canonical_hash` 吃得下真实中文散文，
    # 也不必在这里复制一份真包的阶段表。
    stage_names = [f"0{index + 1}-阶段" for index in range(4)]
    buckets: list[list[str]] = [[] for _ in stage_names]
    for index, ref in enumerate(refs):
        buckets[index % len(stage_names)].append(ref)
    for frame in document["target_frames"]:
        frame["material_stages"] = [
            {"name": name, "after": [] if index == 0 else [buckets[index - 1][0]]}
            for index, name in enumerate(stage_names)
        ]
        frame["content_materials"] = [
            {
                "ref": ref,
                "availability": "proactive",
                "stages": [stage_names[index % len(stage_names)]],
            }
            for index, ref in enumerate(refs)
        ]
    path = _seal(scratch_repository / "internal-demo", document, DEMO_MATERIALS)

    package = load_package(path)

    assert package.document[MATERIAL_TEXTS_KEY] == DEMO_MATERIALS
    assert package.content_hash == canonical_hash(package.document)
    # 29 as of the 2026-09-10 refresh. Asserted as a floor rather than an exact
    # count: the snapshot is refreshed deliberately, and a test that has to be
    # edited every time somebody adds a sentence teaches people to edit it
    # without reading it.
    assert len(DEMO_MATERIALS) >= 29


# ---------------------------------------------------------------------------
# Availability: the combinations JSON Schema cannot state (2026-09-10)
# ---------------------------------------------------------------------------
#
# Every case below is the same defect in a different spelling: **a material the
# profile says is in play that no turn will ever offer.** Caught at publish time
# it is a typo. Reached at runtime it is a conversation where the model had
# nothing to quote and wrote its own words -- and it would be invisible, because
# a message did go out.


def _frame_with(materials: list[Any], **frame_overrides: Any) -> dict[str, Any]:
    document = _base_document()
    frame = document["target_frames"][0]
    frame["content_materials"] = materials
    frame["time_rules"] = [
        {
            "id": "probe-silence",
            "emit_kind": "silence_check",
            "due": {"anchor": "last_outbound_at", "offset_seconds": 240},
            "dedupe_template": "{emit_kind}:{target_frame_id}",
            "cancel_on": ["customer_message"],
            "strategy": frame["allowed_strategy_set"][0],
        }
    ]
    frame["information_slots"] = [{
        "name": "concern_topic",
        "type": "string",
        "required": False,
        "description": "Which kind of loss he cares about this turn.",
    }]
    # **槽位表换掉了，出口谓词也要跟着指向新表**（exit/tone 批的死锁检查）：
    # welcome 原来的 exit_conditions 点 request_summary，而上面那一句把槽位表
    # 换成只有 concern_topic —— 不对齐就是「这一帧永远 complete 不了」那道拒。
    frame["exit_conditions"] = [{"slot": "concern_topic", "present": True}]
    # **槽位表换掉了，策略的声明要跟着换**（运维缺陷 19 的两道封版守卫）：
    # 在架 third 给 `advance_goal` 声明了 `request_summary`，而上面那一句
    # 把 welcome 的槽位表整个换成了只有 `concern_topic` —— 不对齐就撞
    # 「声明的槽必须在允许帧里存在」那道（死锁检查）；清成全空又撞
    # 「至少一个策略要有声明」那道。指到替换后仍然存在的那个槽上。
    document["strategies"]["advance_goal"]["required_slots"] = ["concern_topic"]
    frame.update(frame_overrides)
    for other in document["target_frames"][1:]:
        other["content_materials"] = []
    return document


def _seal_frame(root: Path, name: str, materials: list[Any], **overrides: Any) -> Path:
    document = _frame_with(materials, **overrides)
    refs = {item if isinstance(item, str) else item["ref"] for item in materials}
    return _seal(
        root / name,
        document,
        {ref: DEMO_MATERIALS[sorted(DEMO_MATERIALS)[0]] for ref in refs},
    )


def test_topics_without_a_topic_slot_are_refused(scratch_repository: Path) -> None:
    path = _seal_frame(
        scratch_repository,
        "no-slot",
        [{"ref": "material:a:v1", "availability": "on_request", "topics": ["花钱"]}],
    )

    with pytest.raises(ProfilePackageError, match="declares no 'material_topic_slot'"):
        load_package(path)


def test_a_topic_slot_that_is_not_an_information_slot_is_refused(
    scratch_repository: Path,
) -> None:
    """The value would always be empty, so every gated material would be
    permanently invisible -- and nothing at runtime would say so."""

    path = _seal_frame(
        scratch_repository,
        "wrong-slot",
        [{"ref": "material:a:v1", "availability": "on_request", "topics": ["花钱"]}],
        material_topic_slot="not_a_slot",
    )

    with pytest.raises(ProfilePackageError, match="not one of its information slots"):
        load_package(path)


def test_a_timer_material_naming_no_event_kind_is_refused(
    scratch_repository: Path,
) -> None:
    path = _seal_frame(
        scratch_repository,
        "kindless",
        [{"ref": "material:a:v1", "availability": "timer"}],
    )

    with pytest.raises(ProfilePackageError, match="names no emit_kinds"):
        load_package(path)


def test_a_timer_material_naming_a_kind_no_rule_emits_is_refused(
    scratch_repository: Path,
) -> None:
    """**The zero-caller check, applied to a scheduled event kind.** No timer
    will ever carry `goal_stall` for this frame, so the material is unreachable
    -- and it reads, to whoever edits the file, as though it were in play."""

    path = _seal_frame(
        scratch_repository,
        "unreachable-kind",
        [{"ref": "material:a:v1", "availability": "timer", "emit_kinds": ["goal_stall"]}],
    )

    with pytest.raises(ProfilePackageError, match="no time rule of that frame emits"):
        load_package(path)


def test_topics_on_a_material_that_is_not_on_request_are_refused(
    scratch_repository: Path,
) -> None:
    path = _seal_frame(
        scratch_repository,
        "decorative-topics",
        [{"ref": "material:a:v1", "availability": "proactive", "topics": ["花钱"]}],
        material_topic_slot="concern_topic",
    )

    with pytest.raises(ProfilePackageError, match="declares topics but is 'proactive'"):
        load_package(path)


def test_emit_kinds_on_a_material_that_is_not_a_timer_material_are_refused(
    scratch_repository: Path,
) -> None:
    path = _seal_frame(
        scratch_repository,
        "decorative-kinds",
        [{"ref": "material:a:v1", "availability": "proactive", "emit_kinds": ["silence_check"]}],
    )

    with pytest.raises(ProfilePackageError, match="declares emit_kinds but is 'proactive'"):
        load_package(path)


def test_a_well_formed_availability_declaration_loads(scratch_repository: Path) -> None:
    """The positive, without which every case above would pass on a loader that
    refused everything."""

    path = _seal_frame(
        scratch_repository,
        "well-formed",
        [
            {"ref": "material:a:v1", "availability": "proactive"},
            {"ref": "material:b:v1", "availability": "on_request", "topics": ["花钱"]},
            {"ref": "material:c:v1", "availability": "on_request"},
            {"ref": "material:d:v1", "availability": "timer", "emit_kinds": ["silence_check"]},
            "material:e:v1",
        ],
        material_topic_slot="concern_topic",
    )

    package = load_package(path)

    assert len(package.document["target_frames"][0]["content_materials"]) == 5


def test_the_topic_slot_key_is_spelled_once_per_package() -> None:
    """Two packages, one string -- the same reason `MATERIAL_TEXTS_KEY` is pinned.
    A silent disagreement would mean admission validates one key while selection
    reads another, and every gated material would be invisible at runtime."""

    from fastagent_v2.profiles.registry import MATERIAL_TOPIC_SLOT_KEY
    from fastagent_v2.worker.material_selection import TOPIC_SLOT_KEY

    assert MATERIAL_TOPIC_SLOT_KEY == TOPIC_SLOT_KEY


def test_the_sealing_script_and_the_loader_agree_on_one_number(
    scratch_repository: Path,
) -> None:
    """**Two windows, two files, one hash -- so there is one implementation.**

    The material text and the profile structure are edited by different people;
    the hash covers both. `scripts/seal_profile_package.py` exists so the side
    editing the words never computes a number, and this is what says the number
    it does compute is the one `load_package` will check against. A script that
    stamped a package the loader then rejected would be worse than no script.
    """

    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from seal_profile_package import stamped_hash  # noqa: PLC0415

    refs = sorted(DEMO_MATERIALS)[:4]
    materials = {ref: DEMO_MATERIALS[ref] for ref in refs}
    document = _with_materials(_base_document(), refs)
    path = _seal(scratch_repository / "agreed", document, materials)

    assert stamped_hash(path.parent) == load_package(path).content_hash


def test_the_sealing_script_notices_a_package_with_no_materials_file(
    scratch_repository: Path,
) -> None:
    """The shape every shipped package still has. If the script only handled
    packages with a sibling file it would restamp the others to a different
    number the moment somebody ran it over the whole directory."""

    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from seal_profile_package import stamped_hash  # noqa: PLC0415

    path = _seal(scratch_repository / "plain-agreed", _base_document(), materials=None)

    assert stamped_hash(path.parent) == load_package(path).content_hash


# ---------------------------------------------------------------------------
# 阶段：admission 的三条（2026-09-11）
# ---------------------------------------------------------------------------


def test_a_stage_waiting_on_an_undeclared_material_is_refused(
    scratch_repository: Path,
) -> None:
    """阶段在「那几段发出去之后」到达。等一个没人声明的 ref，就是等一个永不发生的事。"""

    path = _seal_frame(
        scratch_repository,
        "dangling-stage",
        [{"ref": "material:a:v1", "availability": "proactive"}],
        material_stages=[{"name": "报过价", "after": ["material:nobody:v1"]}],
    )

    with pytest.raises(ProfilePackageError, match="never be reached"):
        load_package(path)


def test_a_material_naming_a_stage_the_frame_does_not_declare_is_refused(
    scratch_repository: Path,
) -> None:
    path = _seal_frame(
        scratch_repository,
        "unknown-stage",
        [{"ref": "material:a:v1", "availability": "proactive", "stages": ["试用中"]}],
        material_stages=[{"name": "刚接触", "after": []}],
    )

    with pytest.raises(ProfilePackageError, match="names stages the frame"):
        load_package(path)


def test_stages_on_a_reactive_material_are_refused(scratch_repository: Path) -> None:
    """**两个闸各管各的。** 客户问什么，与谈到哪一步无关——给反应式素材挂阶段，
    等于让「他问了烧钱」被进度挡掉。"""

    path = _seal_frame(
        scratch_repository,
        "staged-reactive",
        [{"ref": "material:a:v1", "availability": "on_request", "stages": ["刚接触"]}],
        material_stages=[{"name": "刚接触", "after": []}],
    )

    with pytest.raises(ProfilePackageError, match="declares stages but is 'on_request'"):
        load_package(path)


def test_a_well_formed_stage_declaration_loads(scratch_repository: Path) -> None:
    path = _seal_frame(
        scratch_repository,
        "staged-ok",
        [
            {"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触"]},
            {"ref": "material:b:v1", "availability": "proactive", "stages": ["报过价"]},
        ],
        material_stages=[
            {"name": "刚接触", "after": []},
            {"name": "报过价", "after": ["material:a:v1"]},
        ],
    )

    assert load_package(path).document["target_frames"][0]["material_stages"]


def test_the_stages_key_is_spelled_once_per_package() -> None:
    from fastagent_v2.profiles.registry import MATERIAL_STAGES_KEY
    from fastagent_v2.worker.material_selection import STAGE_KEY

    assert MATERIAL_STAGES_KEY == STAGE_KEY


def test_a_proactive_material_with_no_stage_is_refused_once_the_frame_has_stages(
    scratch_repository: Path,
) -> None:
    """**默认必须 fail closed，这条是实测逼出来的。**

    旧默认「不声明 stages 就是不受阶段限制」，对「整帧都没声明阶段」是对的，
    对「帧声明了、这一段没声明」就是悄悄逃逸。301 段那一批：主动桶 164 段里
    只有 30 段带阶段维度，**134 段没有**——按旧默认一个回合会把它们全给出去，
    约 43,684 字节对 8,063 字节的余量，**闸等于没关**。
    """

    path = _seal_frame(
        scratch_repository,
        "unstaged-proactive",
        [
            {"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触"]},
            {"ref": "material:b:v1", "availability": "proactive"},
        ],
        material_stages=[{"name": "刚接触", "after": []}],
    )

    with pytest.raises(ProfilePackageError, match="must name the stages"):
        load_package(path)


def test_a_frame_without_stages_still_takes_unstaged_proactive_material(
    scratch_repository: Path,
) -> None:
    """没听说过阶段的 profile 一字不变——上一条不能顺手把它们也拒掉。"""

    path = _seal_frame(
        scratch_repository,
        "no-stages-at-all",
        [{"ref": "material:a:v1", "availability": "proactive"}],
    )

    assert load_package(path).document["target_frames"][0]["content_materials"]


def test_a_stage_whose_materials_do_not_fit_one_turn_is_refused(
    scratch_repository: Path,
) -> None:
    """**上限是从 `MAX_MODEL_INPUT_BYTES` 反推的实测值，不是拍的。**

    超了不是变贵，是 `ProviderInputTooLarge`——那一回合模型一句话都说不出口，
    而每一道门禁都是绿的。所以拒在 admission，作者当场知道。
    """

    from fastagent_v2.worker.material_selection import MATERIAL_BYTES_BUDGET

    # **段数从预算现算**（2026-09-15，理由见 `_over_budget_refs` 的 docstring）：
    # 先前这里写死 40 段，而那是按 5,630 调的，预算一抬就不再超了。
    refs, body = _over_budget_refs()
    document = _frame_with(
        [{"ref": ref, "availability": "proactive", "stages": ["刚接触"]} for ref in refs],
        material_stages=[{"name": "刚接触", "after": []}],
    )
    path = _seal(scratch_repository / "over-budget", document, {ref: body for ref in refs})

    with pytest.raises(ProfilePackageError, match="塞不进一个回合"):
        load_package(path)

    assert MATERIAL_BYTES_BUDGET > 0


def test_a_stage_within_budget_loads(scratch_repository: Path) -> None:
    """没有这一条，上面那条在一个「拒绝一切」的实现上也会绿。"""

    refs = [f"material:few-{index}:v1" for index in range(4)]
    document = _frame_with(
        [{"ref": ref, "availability": "proactive", "stages": ["刚接触"]} for ref in refs],
        material_stages=[{"name": "刚接触", "after": []}],
    )
    path = _seal(scratch_repository / "in-budget", document, {ref: "短正文。" for ref in refs})

    assert load_package(path).document["target_frames"][0]["material_stages"]


def test_the_budget_is_spelled_once_per_package() -> None:
    """两处相等由用例钉死——第一版是 import，撞了部署边界（api 不依赖 worker）。"""

    from fastagent_v2.profiles.registry import MATERIAL_BYTES_BUDGET as api_budget
    from fastagent_v2.worker.material_selection import MATERIAL_BYTES_BUDGET as worker_budget

    assert api_budget == worker_budget


def test_the_input_limit_is_spelled_once_per_package() -> None:
    """`MAX_MODEL_INPUT_BYTES` **也有两份**，而在此之前没有任何东西钉它们相等。

    ## 它是被上一支的复核找出来的，找法值得记

    2026-09-16 那一批给 `understanding_bound.py` 的倍数表配了一条重算守卫
    （`test_the_table_in_the_docstring_is_recomputed_from_todays_limit`），
    说法是「上限再动一次，那张表当场红」。**复核没有去核那句话对不对，
    而是去试它有没有另一扇门** —— 抬 `profiles/registry.py` 那一份：
    **4 条红，而那条新守卫全绿**。

    成因是机械的：守卫读的是 `worker/cost.MAX_MODEL_INPUT_BYTES`，
    而 `services/api` **不能 import `services/worker`**（部署边界，
    `test_the_registry_trusts_the_deployment_it_runs_in_not_the_tree_it_was_built_from`
    守着），所以 admission 那一侧只能自己再写一份。
    **两份数，一份有守卫，另一份没有** —— 而那张表会从没守卫的那一份失效。

    ## 与上面那条 `MATERIAL_BYTES_BUDGET` 是同一个形状，同一个修法

    这一条早就在了，理由逐字相同；**`MAX_MODEL_INPUT_BYTES` 只是漏了**。
    两个常数同源同因，从此也同一条纪律。
    """

    from fastagent_v2.profiles.registry import MAX_MODEL_INPUT_BYTES as api_limit
    from fastagent_v2.worker.cost import MAX_MODEL_INPUT_BYTES as worker_limit

    assert api_limit == worker_limit, (
        f"admission 那一份是 {api_limit:,}，worker 那一份是 {worker_limit:,}。"
        " 两份漂开时，按 worker 那一份算的每一个推导（素材预算、understanding 的倍数表）"
        "都还是绿的，而 admission 会按另一个数封版。"
    )


# ---------------------------------------------------------------------------
# P1-1：fail-closed 闸的两条绕法（独立审查 2026-09-11，ADR-0125 §6.2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "declaration"),
    [
        ("裸字符串", "material:bare:v1"),
        ("对象形但不写 availability", {"ref": "material:bare:v1"}),
    ],
)
def test_a_declaration_without_availability_is_refused_once_the_frame_declares_stages(
    scratch_repository: Path, spelling: str, declaration: Any
) -> None:
    """**两种拼法是同一条声明，所以两种都要拒**（独立复审 2026-09-11，P1-3）。

    `material_selection.declarations()` 把 `"X"` 归一化成的**正是** `{"ref": "X"}`。
    第一版只拒了字符串那一种，于是同一段素材换个写法就绕过阶段闸与字节预算：
    选取层走无限制分支**每个回合都给**（连定时器回合也给），预算读到 0 字节。

    判据因此是「**这条声明有没有 `availability`**」，不是它写成哪种拼法。
    """

    path = _seal_frame(
        scratch_repository,
        "bare-with-stages",
        [
            {"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触"]},
            declaration,
        ],
        material_stages=[{"name": "刚接触", "after": []}],
    )

    with pytest.raises(ProfilePackageError, match="declares none"):
        load_package(path), spelling


def test_a_bare_ref_is_still_fine_when_the_frame_declares_no_stages(
    scratch_repository: Path,
) -> None:
    """收窄只对声明了阶段的帧生效——没听说过阶段的 profile 一字不变。"""

    path = _seal_frame(scratch_repository, "bare-no-stages", ["material:bare:v1"])

    assert load_package(path).document["target_frames"][0]["content_materials"]


def test_the_ungated_fallback_counts_against_every_stage_budget(
    scratch_repository: Path,
) -> None:
    """**兜底保留，但它照样占那一个回合的字节。**

    它每个普通回合都在可用集里，所以它要计进**每一个**阶段的预算——
    `ProviderInputTooLarge` 不认识「这一段是有意不设闸的」。
    """

    from fastagent_v2.profiles.registry import MATERIAL_BYTES_BUDGET
    from fastagent_v2.runtime.prompt_bytes import budgeted_entry_cost

    # 兜底一段就吃掉几乎整个预算，于是本该放得下的一段主动素材也放不下了。
    #
    # **正文长度是量出来的，不是算出来的**（2026-09-11 口径校准）。先前这里写的是
    # `(预算 − 197) // 3`，把「一个中文字三字节」当成了它的成本——而闸量的是
    # `ensure_ascii=True` 的序列化，一个中文字在那里是六字节。那个算式在新口径下
    # 会造出一段**两倍于预算**的正文：用例照样绿，但它绿的理由变成了「大得离谱」，
    # 而不是「刚好压着线的兜底会把别人挤出去」——后者才是这条用例要证明的事。
    # **2026-09-15：预算抬到 32,420 之后，一条兜底素材再也压不到线了。**
    # `content_material_texts` 每条的合同上限是 **4,000 字符**
    # （`merchant-profile.schema.json`），中文最多约 12,000 字节 ——
    # **单条装不满预算，所以这条用例改成几条兜底一起压线**。
    # 那不是把用例放宽：它要证的仍是「兜底计进每一个阶段的预算」，
    # 只是「压着线的兜底」现在需要不止一段。
    full = "兜" * 3_900  # 贴着 4,000 字符的合同上限，留一点余地
    texts: dict[str, str] = {}
    spent = 0
    while True:
        ref = f"material:valve-{len(texts)}:v1"
        cost = budgeted_entry_cost(ref, full)
        if spent + cost >= MATERIAL_BYTES_BUDGET:
            break
        texts[ref] = full
        spent += cost
    # **最后补一条裁到刚好填缝的** —— 整条 4,000 字符的粒度太粗（一条约 11,700 字节），
    # 只用整条会停在离预算九千字节的地方，那时「压着线」就不成立了。
    ref = f"material:valve-{len(texts)}:v1"
    tail = "兜"
    while (
        spent + budgeted_entry_cost(ref, tail + "兜") < MATERIAL_BYTES_BUDGET
        and len(tail) < 3_900
    ):
        tail += "兜"
    texts[ref] = tail
    valves = list(texts)
    assert valves, "一条兜底都放不下，预算或合同上限算错了"
    path = _seal_frame(
        scratch_repository,
        "fallback-counts",
        [
            {"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触"]},
            *({"ref": ref, "availability": "on_request"} for ref in valves),
        ],
        material_stages=[{"name": "刚接触", "after": []}],
        material_topic_slot="concern_topic",
    )
    (path.parent / MATERIALS_FILENAME).write_text(
        json.dumps(
            {"materials": {"material:a:v1": "短", **texts}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    merged = dict(document)
    merged[MATERIAL_TEXTS_KEY] = {"material:a:v1": "短", **texts}
    document["identity"]["content_hash"] = canonical_hash(merged)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ProfilePackageError, match="塞不进一个回合"):
        load_package(path)


# ---------------------------------------------------------------------------
# 空集：一个阶段都不声明的帧（2026-09-11）
#
# **三道闸的同一个空集条件**——拒裸 ref、要求 proactive 点名阶段、字节预算，
# 全都写成「帧声明了阶段时才生效」。帧一个阶段都不声明，三道全不触发，
# 而选取层 `_is_offered` 对没有 `availability` 的声明走无限制分支、每个回合全给。
#
# **已经发生过**：内部演示包的 `introduce` 帧 296 条裸 ref、96,654 字节，
# 预算 6,132，超 15.8 倍。包全绿封版、全绿部署，客户一条消息都没收到。
#
# 上面那条 `test_a_frame_without_stages_still_takes_unstaged_proactive_material`
# 没有错，而且必须继续绿：**不声明阶段的小包本来就合法**。
# 缺的从来不是「拒掉这条路」，是**这条路上的字节没人数**。
# ---------------------------------------------------------------------------


def _over_budget_refs(count: int | None = None) -> tuple[list[str], str]:
    """一组**必定超过一个回合素材预算**的 ref 与正文。

    **段数从 `MATERIAL_BYTES_BUDGET` 现算，不写死**（2026-09-15）。
    先前这里写的是 `count: int = 40` —— 那个 40 是按当时 5,630 的预算调出来的，
    **上限抬到 49,000、预算变成 32,420 之后，40 段轻松装得下**，
    于是六条「应当被拒」的用例一起变成「没被拒」而红。

    **那不是这些用例错了，是夹具把一个可变量写成了字面量** ——
    `AGENTS.md`「结论仍然对，而它的理由已经不成立」那一族。
    现算之后，预算再怎么变它都仍然超。
    """

    from fastagent_v2.runtime.prompt_bytes import budgeted_entry_cost
    from fastagent_v2.worker.material_selection import MATERIAL_BYTES_BUDGET

    body = "这一段是用来把一个回合撑爆的正文，长度接近真实素材。" * 2
    if count is None:
        # **逐段累加到真的超过为止，不按「预算 ÷ 每段」估**。
        # 第一版就是估的，而它用 `material:flood-0000:v1` 当样本算每段开销 ——
        # 真实 ref 是 `flood-0` … `flood-153`，**更短、每段更便宜**，
        # 于是算出来的段数不够，六条用例照旧不红。
        # 每段开销含 ref 本身（`prompt_bytes` 那句「约 20 + len(ref)」），
        # **所以「每段一样贵」这个前提本身就不成立**。
        refs: list[str] = []
        spent = 0
        while spent <= MATERIAL_BYTES_BUDGET:
            ref = f"material:flood-{len(refs)}:v1"
            refs.append(ref)
            spent += budgeted_entry_cost(ref, body)
        return refs, body
    return [f"material:flood-{index}:v1" for index in range(count)], body


def test_a_stageless_frame_flooded_with_bare_refs_is_refused_at_sealing(
    scratch_repository: Path,
) -> None:
    """**用户 2026-09-11 点名要的那条探针，它今天不存在。**

    「正是它不存在，才让这个洞活到部署。」这里用的是**出事那个包的真实写法**——
    裸字符串 ref，一个阶段都不声明——因为换成任何更规整的写法，都验不到那条路。
    """

    refs, body = _over_budget_refs()
    document = _frame_with(list(refs))
    path = _seal(scratch_repository / "stageless-flood", document, {ref: body for ref in refs})

    with pytest.raises(ProfilePackageError, match="每个回合全给"):
        load_package(path)


@pytest.mark.parametrize(
    ("spelling", "make"),
    [
        ("裸字符串", lambda ref: ref),
        ("对象形但不写 availability", lambda ref: {"ref": ref}),
        ("proactive 但一个阶段都不点名", lambda ref: {"ref": ref, "availability": "proactive"}),
    ],
)
def test_every_spelling_of_unrestricted_counts_toward_the_one_turn(
    scratch_repository: Path, spelling: str, make: Any
) -> None:
    """**判据是「这条声明受不受维度约束」，不是它写成哪种拼法。**

    同一条教训这一轮已经付过一次学费（独立复审 2026-09-11，P1-3）：第一版只拒了
    字符串那一种拼法，同一条声明换个写法就过。三种拼法在选取层
    `_is_offered` 里走的是同两条无限制分支，所以在预算里也必须一样重。
    """

    refs, body = _over_budget_refs()
    document = _frame_with([make(ref) for ref in refs])
    path = _seal(scratch_repository / f"flood-{hash(spelling) & 0xFFFF}", document,
                 {ref: body for ref in refs})

    with pytest.raises(ProfilePackageError, match="每个回合全给"):
        load_package(path)


def test_a_small_stageless_package_still_seals(scratch_repository: Path) -> None:
    """**过度收紧的那一侧。** 修的是「没人数字节」，不是「这条路不许走」。

    今天在架的三个包**八个帧全都一个阶段不声明**，其中七个只是体量小才没超——
    它们必须继续封得了版，否则这一批就从修一个洞变成拆一条合法的路。
    """

    path = _seal_frame(
        scratch_repository,
        "small-stageless",
        ["material:a:v1", {"ref": "material:b:v1", "availability": "proactive"}],
    )

    assert load_package(path).document["target_frames"][0]["content_materials"]


def test_gated_material_is_not_counted_as_if_it_arrived_every_turn(
    scratch_repository: Path,
) -> None:
    """**设了闸的素材不算进「每个回合全给」那一桶**——但它们各有各的账。

    **名字与理由都改过**（独立审查 2026-09-11，P2-1）。原来叫
    `…_do_not_count_toward_the_one_turn`，docstring 说「定时器素材不在活回合出现」
    ——**那答的是另一个问题**：预算问的不是「它在不在活回合露面」，
    是「有没有哪一种回合装不下」。定时器回合走的是同一个模型请求。
    读起来像一条不变量，下一个人会以为修它是在破坏规则。

    现在这条只说它该说的那件事：这些素材**不进 `every_turn` 那一桶**。
    它们各自的上限由别处守——topic 侧是 `material_topic_concurrency` 的准备金
    （`test_an_undeclared_topic_concurrency_reserves_every_bucket`），
    定时器侧是按 `emit_kind` 各算一个回合
    （`test_a_timer_turn_that_cannot_fit_is_refused`）。
    """

    # **40 段摊在 10 个 topic 上、上限声明为 1**，合计 14,120 字节。
    # 若这些字节被当成「每个回合全给」，总量远超 6,132，封版会被拒；
    # 按各自的闸算，一个回合最多到场其中一个桶。**这条用例因此仍然吃重**——
    # 夹具第一版把 40 段全挂同一个 topic，那个包**本来就该被拒**
    # （客户一提那一类就 14,120 字节全给），拒它的正是本批新加的准备金。
    refs, body = _over_budget_refs()
    document = _frame_with(
        [
            {
                "ref": ref,
                "availability": "on_request",
                "topics": [f"话题{index % 10}"],
            }
            for index, ref in enumerate(refs)
        ]
        + [
            {"ref": "material:timer:v1", "availability": "timer", "emit_kinds": ["silence_check"]}
        ],
        material_topic_slot="concern_topic",
        material_topic_concurrency=1,
    )
    texts = {ref: body for ref in refs}
    texts["material:timer:v1"] = body
    path = _seal(scratch_repository / "gated-only", document, texts)

    assert load_package(path).document["target_frames"][0]["content_materials"]


def test_every_package_shipping_today_seals_under_the_new_rule() -> None:
    """**把一次性的盘点变成一条常驻的闸。**

    用户 2026-09-11 要求「用新逻辑把今天所有在架的包重新检一遍」。一次性地跑一遍
    只能回答今天；写成用例，下一个包也要回答。

    实测结果记在这里，因为它是这一批的产出之一：`introduce` 是唯一超限的帧
    （96,654 字节），其余七帧在限内——**但八帧全都一个阶段不声明，
    也就是八帧全都没被检查过，七帧只是体量小**。在限是运气不是闸。

    **走完整的 `load_package`，不是只调 `_check_stage_budget`**（独立审查
    2026-09-11，P2-3）。第一版直接调那个函数，于是**绕过了封版期的其余全部规则**
    ——本批新加的并列子桶检查、availability 的六条、`content_hash`、孤儿正文——
    对在架的 `internal-demo-sales` **一条都没跑过**（全仓确认：`conftest` 只加载
    `synthetic-service-desk`，没有任何用例提过这个包）。于是自审里那句
    「作者独立核过」**只回答今天**，而这正是这条用例自己反对的事。
    """

    root = Path(__file__).resolve().parents[2] / "profiles"
    packages = sorted(root.glob("*/profile.json"))
    for profile_path in packages:
        load_package(profile_path)
    assert len(packages) >= 3, f"只找到 {len(packages)} 个包，profiles/ 的形状可能变了"


def test_the_shipped_packages_pass_even_with_no_topic_concurrency_declared(
    tmp_path: Path,
) -> None:
    """**把 `material_topic_concurrency` 的声明整个去掉，在架的包仍然过得了闸。**

    ## 这一条是 T345 的收口

    T345（2026-09-14）量到的是：去掉那个声明（缺省 = 按**全部 topic 桶**算，
    也就是闸的最坏情形），`internal-demo-sales` 的 `introduce` 帧
    **有一个阶段超限 23 字节，而第二紧的只剩 29 字节余量**。
    那条登记的裁定是「重开预算链那一整批」。

    **那一批做完了**（`MATERIAL_BYTES_BUDGET` 5,630 → 32,420、
    `MAX_MODEL_INPUT_BYTES` 16,384 → 49,000），于是那个 23 与那个 29 都消失了。
    **而「消失了」是一句会过期的话** —— 预算再被收紧、或者演示包再长几段，
    它就又回来了，而今天没有任何东西在看着那一侧：
    **封版期那道闸跑的是声明值（3 组），不是最坏情形。**

    所以这一条把最坏情形那一侧钉住。它红的时候要读作：
    「**在架的某个包现在依赖它自己声明的并发度才装得下**」——
    那不一定是缺陷（声明本来就是为此存在的），但它必须是一次**有人看见的**变化，
    而不是像 T345 那次一样，等到有人去量才发现只剩 29 字节。

    ## 为什么改在副本上跑

    `load_package` 读的是路径。这里把三份包拷进 `tmp_path` 再删声明 ——
    **不动真包**：一条会改被测输入的用例，下一次失败会留下垃圾
    （`probe_harness` 那四条失败模式里的第三条）。
    `content_hash` 跟着重算，否则先红的是哈希那道闸，
    而那时量到的是哈希守卫，不是本条想问的东西。
    """

    import copy
    import json
    import shutil

    from fastagent_v2.profiles.registry import (
        MATERIAL_TEXTS_KEY,
        MATERIAL_TOPIC_CONCURRENCY_KEY,
        MATERIALS_FILENAME,
        canonical_hash,
    )

    root = Path(__file__).resolve().parents[2] / "profiles"
    packages = sorted(root.glob("*/profile.json"))
    assert len(packages) >= 3, f"只找到 {len(packages)} 个包，profiles/ 的形状可能变了"

    stripped_any = False
    for profile_path in packages:
        copied = tmp_path / profile_path.parent.name
        shutil.copytree(profile_path.parent, copied)
        target = copied / profile_path.name
        document = json.loads(target.read_text(encoding="utf-8"))

        for frame in document.get("target_frames", []):
            if frame.pop(MATERIAL_TOPIC_CONCURRENCY_KEY, None) is not None:
                stripped_any = True

        # **用仓里那套口径重算**，不自己拼一个：哈希盖的是
        # `profile.json` **与 `materials.json` 合并之后**的那份文档
        # （素材的词句在哈希里面，这正是它们算「被评审过的内容」的原因）。
        # 第一版在这里自己 `sha256` 了一遍 profile.json，当场被
        # `load_package` 的哈希闸拒掉 —— **那一红指着的是我的算法，不是被测的东西**。
        merged = copy.deepcopy(document)
        sidecar_path = copied / MATERIALS_FILENAME
        if sidecar_path.is_file():
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            merged[MATERIAL_TEXTS_KEY] = dict(sidecar["materials"])
        document["identity"]["content_hash"] = canonical_hash(merged)
        target.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        load_package(target)

    assert stripped_any, (
        "一份在架的包都没有声明 `material_topic_concurrency` —— "
        "那么这条用例与上面那条测的是同一件事，它没有存在的理由了。"
        " 要么是声明被删光了（那 T345 要重开），要么是键名改了。"
    )


def test_parallel_sub_buckets_are_refused_because_only_the_last_is_reachable(
    scratch_repository: Path,
) -> None:
    """**按预算切出来的并列子桶，前面那些一个回合都到不了。**

    `current_stage()` 取的是**最后一个** `after` 被满足的阶段。四个并列桶
    `after` 都写空时，实测它恒返回列表里最后那个——前三桶的素材可达段数是 0，
    而每一道门禁都是绿的。

    判据是精确的，不是保守估计：**阶段 i 可达 ⟺ 它后面没有任何阶段 j 的
    `after` 是 after_i 的子集**（取 `D = after_i` 即可构造出让 i 命中的会话）。
    """

    document = _frame_with(
        [{"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触-1入口"]}],
        material_stages=[
            {"name": "刚接触-1入口", "after": []},
            {"name": "刚接触-2渠道", "after": []},
        ],
    )
    path = _seal(scratch_repository / "parallel-buckets", document, {"material:a:v1": "文"})

    with pytest.raises(ProfilePackageError, match="永远到不了"):
        load_package(path)


def test_sub_buckets_chained_by_after_are_accepted(scratch_repository: Path) -> None:
    """**串起来就合法**——修的是「并列」，不是「切桶」本身。

    串行时依据仍然是「已发出的 ref」，是事实不是推断，而且推错会有东西变红：
    `after` 指的 ref 没发出去，阶段就不前进，是可观测的。
    """

    document = _frame_with(
        [
            {"ref": "material:a:v1", "availability": "proactive", "stages": ["刚接触-1入口"]},
            {"ref": "material:b:v1", "availability": "proactive", "stages": ["刚接触-2渠道"]},
        ],
        material_stages=[
            {"name": "刚接触-1入口", "after": []},
            {"name": "刚接触-2渠道", "after": ["material:a:v1"]},
        ],
    )
    path = _seal(
        scratch_repository / "chained-buckets",
        document,
        {"material:a:v1": "文", "material:b:v1": "文"},
    )

    assert load_package(path).document["target_frames"][0]["material_stages"]


# ---------------------------------------------------------------------------
# topic 侧进 admission（独立审查 2026-09-11，P1-1）
#
# 先前 topic 桶**整个不在 admission 里**，于是「一个回合塞不塞得下」这条不变量
# 只在 proactive 轴上成立。实测本批封的那个包：最大的阶段桶 2,902 字节在限，
# 而槽里累到 6 类时一个回合是 6,973 / 预算 6,132，8 类是 8,432（1.38 倍）。
# 匹配是包含式，写进槽的词此后每回合继续命中——累积不是偶发。
#
# 用户 2026-09-11 裁定：给累积封顶到三类，并把这个数写成 profile 里的字段。
# ---------------------------------------------------------------------------


def _topic_frame(concurrency: int | None, bucket_count: int = 8) -> dict[str, Any]:
    """一个帧：一个阶段 + `bucket_count` 个各占一段的 topic 桶。"""

    refs = [f"material:topic-{index}:v1" for index in range(bucket_count)]
    materials: list[Any] = [
        {"ref": "material:stage-a:v1", "availability": "proactive", "stages": ["01"]}
    ]
    materials += [
        {"ref": ref, "availability": "on_request", "topics": [f"话题{index}"]}
        for index, ref in enumerate(refs)
    ]
    overrides: dict[str, Any] = {
        "material_stages": [{"name": "01", "after": []}],
        "material_topic_slot": "concern_topic",
    }
    if concurrency is not None:
        overrides["material_topic_concurrency"] = concurrency
    return _frame_with(materials, **overrides)


def _topic_texts(bucket_count: int = 8) -> dict[str, str]:
    """每个 topic 桶一段，**合起来必定超过一个回合的素材预算**。

    **段长从 `MATERIAL_BYTES_BUDGET` 现算**（2026-09-15）：先前写死 `* 10`，
    那是按当时 5,630 的预算调的；预算抬到 32,420 之后八个桶轻松装得下，
    于是「不声明 concurrency 就按全部桶预留」那条用例不再红。
    **理由同 `_over_budget_refs`：夹具把一个可变量写成了字面量。**

    **一处合同上限要照顾**：`content_material_texts` 每条最多 4,000 字符
    （`merchant-profile.schema.json`），所以段长封在那以下，
    不够就靠桶数 —— 而桶数由调用方给。
    """

    from fastagent_v2.runtime.prompt_bytes import budgeted_entry_cost
    from fastagent_v2.worker.material_selection import MATERIAL_BYTES_BUDGET

    unit = "这一段占掉一个 topic 桶的额度，长度接近真实素材。"
    repeats = 10
    while repeats < 150:
        candidate = unit * repeats
        if len(candidate) > 3_900:
            break
        per = budgeted_entry_cost("material:topic-0:v1", candidate)
        if per * bucket_count > MATERIAL_BYTES_BUDGET:
            break
        repeats += 5
    body = unit * repeats
    texts = {f"material:topic-{index}:v1": body for index in range(bucket_count)}
    texts["material:stage-a:v1"] = body
    return texts


def test_an_undeclared_topic_concurrency_reserves_every_bucket(
    scratch_repository: Path,
) -> None:
    """**缺省必须落在安全那一侧。**

    不声明上限就按**全部** topic 桶算——「一个没人声明的上限」就是「一个没人守的
    上限」。缺省若取某个小数字，这条闸又会变成「只在有声明时才生效」，
    而那正是本批在修的那一类缺陷。
    """

    path = _seal(
        scratch_repository / "no-cap",
        _topic_frame(concurrency=None),
        _topic_texts(),
    )

    with pytest.raises(ProfilePackageError, match="topic 准备金"):
        load_package(path)


def test_a_declared_topic_concurrency_is_what_gets_reserved(
    scratch_repository: Path,
) -> None:
    """声明一个更小的数，准备金就按那个数算——同一个包因此过得了。

    这不是放宽：声明等于 profile 作者主张「goal 真的把累积封在这个数以内」。
    **那一致性今天没有东西守着**，所以它是一条登记（T274）而不是一条保证。
    """

    path = _seal(
        scratch_repository / "capped",
        _topic_frame(concurrency=1),
        _topic_texts(),
    )

    assert load_package(path).document["target_frames"][0]["material_topic_concurrency"] == 1


def test_a_staged_frame_puts_nothing_in_the_whole_bucket(
    scratch_repository: Path,
) -> None:
    """**注释点名的这条用例先前不存在**（独立审查 2026-09-11，P2-2）。

    `_check_stage_budget` 的注释说「帧声明了阶段时 `every_turn` 恒为 0，
    所以两条路不会互相重复计数」，并点名这条用例钉住它——全仓 grep，
    **那个名字只在那条注释里出现过**。而它声称被钉住的不变量是吃重的：
    分阶段那条路上 `every_turn` **算了但从不使用**，所以一旦有素材落进它
    又走了分阶段路，那段字节**哪儿都没被计入**——是漏计不是多计。

    今天让它不可达的是 `_check_material_availability` 的两条拒绝
    （声明了阶段的帧拒收裸 ref、proactive 必须点名阶段）——
    **那是另一段代码的副作用，不是这条路自己守住的**。这条用例钉的就是那两条拒绝。
    """

    # **只给本次迭代自己那两条 ref 的正文，并且逐条点名 `match=`**
    # （独立复审 2026-09-11，P2-2R）。第一版把三条 ref 的正文全交给 `_seal`，
    # 而每次迭代只声明两条——于是包**先被「孤儿正文」那条规则拒掉**，
    # 根本走不到被测的规则；再加上 `pytest.raises` 不带 `match=`，任何拒绝都算数。
    # 反向探针因此是绿的：**把它声称钉住的那条 `raise` 去掉，用例照样过**。
    #
    # **这正是 P2-2 原本那条 finding 的同一个形状**——名字对、位置对、
    # 但不会因为守卫消失而变红。同一个形状在同一条链上第四次出现。
    for material, expected in (
        ("material:bare:v1", "must declare an `availability`"),
        (
            {"ref": "material:no-stage:v1", "availability": "proactive"},
            "must name the stages it belongs to",
        ),
    ):
        ref = material if isinstance(material, str) else material["ref"]
        document = _frame_with(
            [
                {"ref": "material:staged:v1", "availability": "proactive", "stages": ["01"]},
                material,
            ],
            material_stages=[{"name": "01", "after": []}],
        )
        path = _seal(
            scratch_repository / f"whole-bucket-{isinstance(material, str)}",
            document,
            {"material:staged:v1": "文", ref: "文"},
        )
        with pytest.raises(ProfilePackageError, match=re.escape(expected)):
            load_package(path)


def test_a_timer_turn_that_cannot_fit_is_refused(scratch_repository: Path) -> None:
    """**定时器回合也是一个回合**（独立审查 2026-09-11，P2-1）。

    `select_materials` 在 `emit_kind` 非空时只给该 kind 的定时器素材，
    此后走的是同一个 `generate` 与同一个 `MAX_MODEL_INPUT_BYTES`。
    先前两条路都不数它：同样的字节写成定时器素材 **ACCEPTED**，
    写成裸 ref 放进无阶段帧 **REFUSED**——而两者都会让那一回合发不出东西。
    """

    refs, body = _over_budget_refs()
    document = _frame_with(
        [
            {"ref": "material:stage-a:v1", "availability": "proactive", "stages": ["01"]},
        ]
        + [
            {"ref": ref, "availability": "timer", "emit_kinds": ["silence_check"]}
            for ref in refs
        ],
        material_stages=[{"name": "01", "after": []}],
    )
    texts = {ref: body for ref in refs}
    texts["material:stage-a:v1"] = body
    path = _seal(scratch_repository / "fat-timer", document, texts)

    with pytest.raises(ProfilePackageError, match="定时器回合装不下"):
        load_package(path)


# ---------------------------------------------------------------------------
# 三种请求共用一个上限（运维报障 2026-09-11）
# ---------------------------------------------------------------------------


def test_the_platform_prompt_sizes_are_spelled_once_per_package() -> None:
    """`services/api` 不导 `services/worker`，所以两边各写一遍——**这条钉死它们相等。**

    先前 `MATERIAL_BYTES_BUDGET` 就是这么办的，而今天那条用例**当场抓到了**
    我只改了 worker 那一边（改预算时崩在 print 上，api 侧没执行）。
    平台提示词的尺寸同理：它们是 admission 算固定底的输入，
    一旦两边错开，admission 会拿一个不存在的数去判。
    """

    import json as _json

    from fastagent_v2.profiles.registry import (
        MAX_MODEL_INPUT_BYTES as api_limit,
    )
    from fastagent_v2.profiles.registry import (
        PLATFORM_PROMPT_BYTES,
    )
    from fastagent_v2.worker.cost import MAX_MODEL_INPUT_BYTES as worker_limit
    from fastagent_v2.worker.inference_model import (
        _GENERATE_SYSTEM,
        _PLAN_SYSTEM,
        _UNDERSTAND_SYSTEM,
    )

    assert api_limit == worker_limit

    def wire(text: str) -> int:
        return len(
            _json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    assert PLATFORM_PROMPT_BYTES == {
        "UNDERSTAND_VALUE": wire(_UNDERSTAND_SYSTEM),
        "PLAN_CAPABILITY": wire(_PLAN_SYSTEM),
        "GENERATE_VALUE": wire(_GENERATE_SYSTEM),
    }, "改了某个平台提示词就要同步这张表——admission 用它算固定底"

    # **响应 schema 同理**（用户 2026-09-11 指定并进本批）：它是固定底的一部分，
    # 而且 plan-result 比它的 system prompt 还大近一倍。三个角色的 binding 都是
    # `openai-responses-v1`，那一支把 schema 放进 payload——这一条一并钉住。
    from fastagent_v2.profiles.registry import PLATFORM_RESPONSE_SCHEMA_BYTES
    from fastagent_v2.runtime.contracts import model_output_schema
    from fastagent_v2.worker.model_policy import active_release

    release = active_release()
    for role, declared in PLATFORM_RESPONSE_SCHEMA_BYTES.items():
        binding = release.role(role)
        assert binding.api_compatibility == "openai-responses-v1", (
            f"{role} 不再走 responses API，那 schema 就不在 payload 里了——"
            "这张表的前提变了，固定底要重算"
        )
        schema = model_output_schema(binding.output_contract)
        assert declared == len(
            _json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ), f"{role} 的响应 schema 尺寸变了，admission 的固定底要跟着改"


def test_a_goal_too_long_for_the_plan_request_is_refused(
    scratch_repository: Path,
) -> None:
    """**understand 与 plan 的请求各带整段 `goal`，而先前没有任何东西在检。**

    运维 2026-09-11：计划调用在本地被拒（`ProviderInputTooLarge`），回落保守计划，
    于是不发消息——**而每一道门禁都是绿的**。根因有两半，这是没被检的那一半：
    `MAX_MODEL_INPUT_BYTES` 是三种请求共用的一个上限，admission 却只建模了
    generate 那一种的 `content_materials` 那一项。

    **同一个洞的第三次发作**：某一处建了闸，另一处没有，两处共用同一个上限。
    """

    from fastagent_v2.profiles.registry import (
        MAX_MODEL_INPUT_BYTES,
        PLATFORM_PROMPT_BYTES,
        PLATFORM_RESPONSE_SCHEMA_BYTES,
    )

    # **固定底本身越过上限**，这是这条闸今天唯一能诚实断言的那一半
    # （回合余量要多大取决于真实 understanding 的分布，登记 T275）。
    room = (
        MAX_MODEL_INPUT_BYTES
        - PLATFORM_PROMPT_BYTES["PLAN_CAPABILITY"]
        - PLATFORM_RESPONSE_SCHEMA_BYTES["PLAN_CAPABILITY"]
    )
    # **`room` 已经是字节数**，而「目」每字 3 字节——先前这里写 `"目" * room`，
    # 于是 goal 是 29,301 字节、超限三倍（独立审查 2026-09-11，P1-1）。
    #
    # **三倍超限的夹具钉不住这一批新加的那一项**：把响应 schema 那一项
    # 从固定底的和里删掉，它照样超、照样红不了。审查方的反向探针证过：
    # 删掉 `+ PLATFORM_RESPONSE_SCHEMA_BYTES[role]` 之后 `tests/contracts`
    # **与未变异基线逐位相同，一条都没红**。
    #
    # **这正是本批自审 §4.1 自己写下的那条教训在同一批里的第二次发作**
    # （「常量对不对」与「有没有人用它」是两个问题）——探针 C 那次付的学费，
    # 在新加的这一项上没有花出去。
    #
    # 改成刚好落进**可判别区间**：`// 3 + 1` 让 goal 恰好比 `room` 多一个字节，
    # 于是「算上 schema 才超、不算就不超」——**这条用例此后只为 schema 那一项而红**。
    # **2026-09-15（批 2）：支点从 `goal` 换成 `exit_conditions`。**
    #
    # 那一批给 `goal` 裁了 `maxLength = 1200`，于是这个夹具原来那个
    # 一万多字的 goal **在契约校验就被拒**，`load_package` 根本走不到字节那道闸 ——
    # **这条用例会变成一条「契约拒了超长字符串」的用例，
    # 而它本来守的是「响应 schema 那一项有没有被算进固定底」。**
    #
    # **加一条界让一条旧守卫变空**，是这一批撞到的那个形状：
    # 新约束落在旧夹具的杠杆上，用例照旧红（或照旧绿），而它证的东西变了。
    # 处置不是放宽那条界，是**换一个今天仍然无界的杠杆** ——
    # `exit_conditions` 同样逐字进 plan 的 `user_prompt`，而它没有 `maxItems`。
    #
    # **2026-09-24（exit/tone 批）：杠杆的用法变了，杠杆本身没换。**
    # exit_conditions 上了闭合形状（本批建执行时钉的：{slot, present|equals}，
    # equals ≤ 200 字符）—— 原来那条「一个键装下整个 room」的写法在契约层
    # 就被拒，正是这段注释预言过的「加一条界让一条旧守卫变空」。
    # 处置照旧例：**数组仍然没有 maxItems**，改用 N 条逐条合法的谓词凑总量 ——
    # 每条 equals 66 个中文字（≤200 界内），slot 用本帧真有的那个
    # （不然先撞死锁检查，红的又不是本条要证的那道）。
    # 精度：总量超出 room 至多一条谓词的大小（~250 字节），
    # 远小于 schema 那一项（2,000+），**可判别区间没有因为换法而消失**。
    import json as _j
    one = {"slot": "concern_topic", "equals": "目" * 66}
    per = len(_j.dumps(one, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    document = _frame_with(
        [{"ref": "material:a:v1", "availability": "proactive", "stages": ["01"]}],
        material_stages=[{"name": "01", "after": []}],
        exit_conditions=[dict(one) for _ in range(room // per + 1)],
    )
    path = _seal(scratch_repository / "fat-goal", document, {"material:a:v1": "文"})

    with pytest.raises(ProfilePackageError, match="光固定底就装不下"):
        load_package(path)


def test_a_goal_that_fits_is_still_accepted(scratch_repository: Path) -> None:
    """过度收紧的那一侧：今天在架的包必须继续封得了版。

    **实测在架三个包的固定底（含响应 schema，线路口径，本批 HEAD 的常量）：**

    | 包 | understand | plan | plan 余量 |
    |---|---:|---:|---:|
    | `internal-demo-sales` | 7,257 | **8,416** | 7,968 |
    | `synthetic-carrier` | 5,947 | 7,106 | 9,278 |
    | `synthetic-third` | 5,627 | 6,786 | 9,598 |

    **先前这里写的是 understand 6,792 / plan 6,766**（独立审查 2026-09-11，P2-1）：
    那是 `01882f6` 时的旧值，**恰好是不含响应 schema 的那两个数** ——
    响应 schema 并进固定底之后没跟着改。

    **偏差的方向是把余量说大**：按 6,766 读会以为 plan 还剩 9.6 KB 给
    `understanding`，而**这条 docstring 正是下一个人判断「T275 要留多少」时
    会读的那个数**。
    这一条守的是「这条闸不是把所有中文 goal 都拒掉」。
    """

    document = _frame_with(
        [{"ref": "material:a:v1", "availability": "proactive", "stages": ["01"]}],
        material_stages=[{"name": "01", "after": []}],
        goal="这是一段正常长度的目标描述，讲清楚要达成什么、怎么填槽。" * 20,
    )
    path = _seal(scratch_repository / "ok-goal", document, {"material:a:v1": "文"})

    assert load_package(path).document["target_frames"][0]["goal"]


def test_the_admission_measure_is_the_wire_not_the_escaped_form() -> None:
    """admission 的固定底按**线路**算，不按转义算。

    独立审查 2026-09-11，P1-2：当时那个函数的 docstring 写着
    「Measuring the escaped form here would repeat the defect this check exists
    to catch, one layer up」——**而那句话没有任何东西守着**。
    探针把它改成 `ensure_ascii=True`，`tests/contracts` 无一变红。

    量级不是小事：中文按 6 字节/字算，一个 2,700 字左右的 goal 就能把 plan
    的固定底从 11 KB 推过 16 KB，**把一个跑得好好的包拒在封版处** ——
    症状与相反方向那次一模一样：**门禁全绿、东西发不出去。**

    **2026-09-15 改成从固定底那一侧断言，而不是从那个工具函数。**
    先前断言的是 `registry._wire_bytes("中") == 5`；那个函数已经删掉了
    （固定底改走 `request_floor_bytes` → `runtime.prompt_bytes.document_cost`，
    单层对两层那一笔正是它少算的三笔之一）。
    **断言搬到行为那一侧反而更强**：它量的是「同一个包，goal 多一个汉字，
    固定底涨几个字节」——涨 3 是线路口径，涨 6 才是转义口径。
    """

    base = {
        "id": "f",
        "goal": "a",
        "information_slots": [],
        "allowed_strategy_set": [],
        "exit_conditions": [],
    }
    one_more_cjk = dict(base, goal="a中")
    floors = request_floor_bytes(base, document={})
    grown = request_floor_bytes(one_more_cjk, document={})

    for role in ("UNDERSTAND_VALUE", "PLAN_CAPABILITY"):
        delta = grown[role] - floors[role]
        assert delta == 3, (
            f"{role}: goal 多一个汉字，固定底涨了 {delta} 字节。"
            "线路口径应当是 3；涨 6 说明口径退回了转义形式（六个 ASCII 字节的 u-escape），"
            "而那会把在架的包拒在封版处"
        )

    # `goal` 在 understand 与 plan 里**各带一遍**，所以两种请求都涨 ——
    # 而 generate 的 `user_prompt` 里没有配包那一半，它不涨。
    assert grown["GENERATE_VALUE"] == floors["GENERATE_VALUE"]
