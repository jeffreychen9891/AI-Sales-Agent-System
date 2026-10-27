"""Every importable package is registered for packaging, in **both** places.

**This exists because the gate structurally cannot see the thing it guards**
(independent review 2026-09-09, P2-1). Three facts lock together:

1. `[tool.setuptools] packages` is an **explicit list**, not `find:` -- a package
   omitted from it simply is not installed;
2. `Dockerfile` installs with `pip install --no-deps .`, so **the image really is
   built from that list**;
3. `scripts/ci.sh` does **not** install the project at all -- it exports
   `PYTHONPATH` pointing at the four `src` trees.

So the tree the gate tests can never disagree with the tree the image ships,
because the gate never reads the list. That is the repository's recurring
"evidence taken from an environment other than the real one" shape, and B2-5's
own self-review named this as the batch's largest risk -- while a probe deleting
both registration lines left all 61 of its cases green.

**The failure is not hypothetical and not immediate, which is the worst
combination**: an unregistered `fastagent_v2.channels.wecom_proxy` breaks nothing
until B2-3 wires the client into the dispatcher, and then it breaks as *the
worker not starting in the image*, a long way from the line that caused it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

#: The `src` roots that hold first-party code. Kept explicit rather than globbed
#: so that a new service tree is a deliberate addition here, not something this
#: case silently starts or stops covering.
SRC_ROOTS = (
    ROOT / "packages" / "runtime" / "src",
    ROOT / "packages" / "channels" / "src",
    ROOT / "services" / "api" / "src",
    ROOT / "services" / "worker" / "src",
    ROOT / "services" / "ingress" / "src",
)


def _importable_packages() -> dict[str, Path]:
    """Every directory under the src roots that is an importable package.

    "Has an `__init__.py`" is the test, because that is exactly what makes
    setuptools' explicit list load-bearing: a directory without one is a
    namespace package and travels with its parent; one with an `__init__.py`
    does not, and must be named.
    """

    found: dict[str, Path] = {}
    for root in SRC_ROOTS:
        for init in root.rglob("__init__.py"):
            relative = init.parent.relative_to(root)
            found[".".join(relative.parts)] = init.parent
    return found


def test_every_importable_package_is_registered_for_packaging() -> None:
    """The list the image is built from must name every package that exists.

    Asserted in **both** directions, because they fail differently:

    * a package on disk but not in the list ships a broken wheel;
    * a name in the list with nothing on disk makes `pip install` fail outright,
      which is loud but still worth catching here rather than in a build.
    """

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    declared = set(setuptools["packages"])
    directories = set(setuptools["package-dir"])

    on_disk = set(_importable_packages())
    assert on_disk, "found no packages at all; SRC_ROOTS is wrong and this case is inert"

    missing = on_disk - declared
    assert not missing, (
        f"these packages exist but are not in [tool.setuptools] packages: {sorted(missing)}. "
        "The gate runs off PYTHONPATH and will stay green; the installed image will not have them."
    )

    phantom = declared - on_disk
    assert not phantom, f"declared but not on disk: {sorted(phantom)}"

    # **Both places.** `packages` decides what is installed; `package-dir` decides
    # where each one is read from. A name in one and not the other is the same
    # bug wearing a different hat.
    assert declared == directories, (
        "packages and package-dir disagree: "
        f"only in packages {sorted(declared - directories)}, "
        f"only in package-dir {sorted(directories - declared)}"
    )


def test_each_declared_package_directory_is_where_it_claims_to_be() -> None:
    """`package-dir` values are paths; a stale one installs the wrong tree.

    Cheap to check and it closes the remaining way for the two lists to agree
    with each other while both being wrong.
    """

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    package_dir = config["tool"]["setuptools"]["package-dir"]
    on_disk = _importable_packages()

    for name, relative in package_dir.items():
        declared_path = (ROOT / relative).resolve()
        assert declared_path.is_dir(), f"{name} -> {relative} does not exist"
        assert (declared_path / "__init__.py").is_file(), (
            f"{name} -> {relative} has no __init__.py; it is not an importable package"
        )
        assert declared_path == on_disk[name].resolve(), (
            f"{name} is registered as {relative} but was found at {on_disk[name]}"
        )


def test_the_operator_tools_can_still_import_what_they_use() -> None:
    """Scripts build their **own** `sys.path`, and the gate never exercises it.

    `scripts/wecom_proxy_bind.py` inserts a hard-coded list of source roots
    before importing the client. Under the gate that list is irrelevant --
    `ci.sh` exports its own `PYTHONPATH`, so the import resolves no matter what
    the script says -- and under mypy it is irrelevant too, because `mypy_path`
    decides. **The list only matters when a human runs the tool**, which is the
    one situation nothing in this repository reproduces.

    B2-5 proved that is not hypothetical: moving the client to
    `packages/channels` left this script listing `services/ingress/src`, and it
    would have died with `ModuleNotFoundError: No module named
    'fastagent_v2.channels'` **the first time an operator ran it** -- while
    every gate stayed green.

    So this runs the tool the way an operator would: a fresh interpreter with
    `PYTHONPATH` **removed**, so only the script's own list is in play.
    `--help` is enough, because the imports happen at module scope.
    """

    import os
    import subprocess
    import sys

    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # Not inherited either: a stray value would defeat the whole point.
    environment.pop("PYTHONHOME", None)

    finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(ROOT / "scripts" / "wecom_proxy_bind.py"), "--help"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
        timeout=60,
    )

    assert finished.returncode == 0, (
        "the operator tool cannot import its own dependencies with only the "
        f"source roots it names:\n{finished.stderr}"
    )
