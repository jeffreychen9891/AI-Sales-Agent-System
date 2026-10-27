"""Stamp a profile package's `identity.content_hash` and prove it loads.

**Why this exists rather than a paragraph explaining the recipe.** The hash now
covers `profile.json` *together with* `materials.json`, which means two people
editing two files have to agree on one number. Writing the recipe down and
letting each side implement it is how two sides come to disagree; the internal
demo package was already being stamped by hand in one window while this
mechanism was built in another. So there is one implementation, and the other
window runs it instead of computing anything.

It refuses to leave a package it cannot load. Stamping and validating are the
same act here: a hash over a document that `load_package` would reject is a
number that certifies nothing.

    python scripts/seal_profile_package.py profiles/internal-demo-sales
    python scripts/seal_profile_package.py profiles/internal-demo-sales --check

`--check` writes nothing and exits non-zero if the stamp is stale -- what a gate
or a pre-commit hook wants.

**It prints no material text**, only refs and counts. The words are
customer-visible content; a script that echoed them into a terminal transcript
would be moving them somewhere nobody decided to put them.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# **Self-contained on purpose**, unlike the other scripts here. They are run by
# `ci.sh`, which exports `PYTHONPATH` first; this one is run by hand, by whoever
# is editing the words -- and "set five directories on your PYTHONPATH before
# stamping" is exactly the instruction that ends with somebody computing the
# hash their own way instead.
for _source in ("packages/runtime/src", "packages/channels/src", "services/api/src"):
    sys.path.insert(0, str(ROOT / _source))

from fastagent_v2.profiles.protocol import ProfilePackageError  # noqa: E402
from fastagent_v2.profiles.registry import (  # noqa: E402
    MATERIAL_TEXTS_KEY,
    MATERIALS_FILENAME,
    PACKAGE_FILENAME,
    canonical_hash,
    load_package,
)


def stamped_hash(package: Path) -> str:
    """The hash `load_package` will check this package against.

    **Computed from the merged document**, exactly as the loader merges it --
    the sibling material file's words are inside the hash, which is what makes
    them reviewed content rather than a file that happens to sit nearby.
    """

    document: dict[str, Any] = json.loads(
        (package / PACKAGE_FILENAME).read_text(encoding="utf-8")
    )
    materials_path = package / MATERIALS_FILENAME
    merged = copy.deepcopy(document)
    if materials_path.is_file():
        sidecar = json.loads(materials_path.read_text(encoding="utf-8"))
        # 带信息的报错，而不是一个裸 `KeyError` traceback：改词的那一侧多半
        # 不读 Python 栈（独立审查 2026-09-11，P3-3）。
        if not isinstance(sidecar, dict) or "materials" not in sidecar:
            present = (
                sorted(sidecar)[:20]
                if isinstance(sidecar, dict)
                else type(sidecar).__name__
            )
            raise SystemExit(
                f"{materials_path} 里没有 `materials` 这个键，它现在有的是 {present}"
            )
        merged[MATERIAL_TEXTS_KEY] = dict(sidecar["materials"])
    if "identity" not in document:
        raise SystemExit(f"{package / PACKAGE_FILENAME} 里没有 `identity`，盖不了章")
    return canonical_hash(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="directory holding profile.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the stamp without writing; non-zero exit if it is stale",
    )
    arguments = parser.parse_args()

    package: Path = arguments.package
    profile_path = package / PACKAGE_FILENAME
    if not profile_path.is_file():
        print(f"no {PACKAGE_FILENAME} in {package}", file=sys.stderr)
        return 2

    document: dict[str, Any] = json.loads(profile_path.read_text(encoding="utf-8"))
    declared = str(document.get("identity", {}).get("content_hash", ""))
    calculated = stamped_hash(package)

    if arguments.check:
        if declared != calculated:
            print(
                f"stale stamp: {profile_path} declares {declared or '<none>'} "
                f"but the package hashes to {calculated}",
                file=sys.stderr,
            )
            return 1
        # **`--check` 也要过一遍 `load_package`**（独立审查 2026-09-11，P2-5）。
        # 第一版比完哈希就 return，于是**一个哈希正确但加载不了的包拿到 exit 0**
        # ——而 `--check` 恰恰是这份 docstring 推荐给门禁 / pre-commit 的那个模式，
        # 也就是最不该只验一半的那个入口。它本来就不写文件，没有理由少验。
        try:
            load_package(profile_path)
        except ProfilePackageError as error:
            print(f"stamp is current, but the package does not load: {error}", file=sys.stderr)
            return 1
        print(f"stamp is current and the package loads: {calculated}")
        return 0

    if declared != calculated:
        document["identity"]["content_hash"] = calculated
        # **`newline="\n"` 不是洁癖，是这个脚本的正确性**：Windows 上
        # `write_text` 默认把每个 `\n` 翻成 `\r\n`，于是**盖章的那次写入
        # 本身就改掉了被盖章的内容** —— 章是按 LF 算的，文件落地成 CRLF。
        # `test_synthetic_carrier_corpus.py` 那条「内容寻址的文件里不许有 CR」
        # 因此会红，而红的地方离这里很远（2026-09-16 实测，重封 11d 的配包时）。
        profile_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"stamped {profile_path}: {declared or '<none>'} -> {calculated}")
    else:
        print(f"already stamped: {calculated}")

    # **Stamping is not the job; a loadable package is.** Every rule the loader
    # applies beyond the hash -- material coverage in both directions, the
    # availability rules, closing_frame_ref, rule-id uniqueness -- is checked
    # here too, because a correctly stamped package that cannot be published is
    # not progress.
    try:
        loaded = load_package(profile_path)
    except ProfilePackageError as error:
        print(f"stamped, but the package does not load: {error}", file=sys.stderr)
        return 1

    texts = loaded.document.get(MATERIAL_TEXTS_KEY) or {}
    print(
        f"loads clean: {loaded.profile_id} {loaded.version}, "
        f"{len(loaded.document.get('target_frames', []))} frames, "
        f"{len(texts)} materials with text"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
