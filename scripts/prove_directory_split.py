from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = "99012cb0e8db417f05bb894c94742eb611e32a69"
DEFAULT_CANDIDATE = "5d649f2e5c09c2a29ab7e51e48fd1239044d564f"
RUNTIME_MODULES = {
    "__init__.py",
    "auth.py",
    "contracts.py",
    "cos_storage.py",
    "db.py",
    "queue.py",
    "redaction.py",
    "settings.py",
}
PATH_DECLARATIONS = {"CONTRACT_ROOT", "MODEL_POLICY_ROOT"}
PACKAGE_RENAMES = {
    "fastagent.api_app": "fastagent_v2.api.app",
    "fastagent.ingress_app": "fastagent_v2.ingress.app",
    "fastagent.ingress": "fastagent_v2.ingress.adapter",
    "fastagent.wecom_proxy": "fastagent_v2.ingress.wecom_proxy",
    "fastagent.auth": "fastagent_v2.runtime.auth",
    "fastagent.contracts": "fastagent_v2.runtime.contracts",
    "fastagent.cos_storage": "fastagent_v2.runtime.cos_storage",
    "fastagent.db": "fastagent_v2.runtime.db",
    "fastagent.queue": "fastagent_v2.runtime.queue",
    "fastagent.redaction": "fastagent_v2.runtime.redaction",
    "fastagent.settings": "fastagent_v2.runtime.settings",
}
WORKER_MODULES = (
    "channel",
    "channel_sink",
    "cli",
    "cost",
    "inference",
    "inference_model",
    "inference_validation",
    "metrics",
    "model_policy",
    "pipeline",
    "providers",
    "scheduler",
    "strategies",
)
PACKAGE_RENAMES.update(
    {f"fastagent.{name}": f"fastagent_v2.worker.{name}" for name in WORKER_MODULES}
)
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git executable is required for the directory-split proof")
#: Narrowed once, here, so every call site gets `str` rather than `str | None`.
GIT: str = _GIT


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        [GIT, *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def _destination(source: str) -> Path:
    relative = source.removeprefix("src/fastagent/")
    if relative.startswith("wecom_proxy/"):
        return ROOT / "services" / "ingress" / "src" / "fastagent_v2" / "ingress" / relative
    if relative == "api_app.py":
        return ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"
    if relative == "ingress.py":
        return ROOT / "services" / "ingress" / "src" / "fastagent_v2" / "ingress" / "adapter.py"
    if relative == "ingress_app.py":
        return ROOT / "services" / "ingress" / "src" / "fastagent_v2" / "ingress" / "app.py"
    if relative in RUNTIME_MODULES:
        return ROOT / "packages" / "runtime" / "src" / "fastagent_v2" / "runtime" / relative
    return ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / relative


class SemanticNormalizer(ast.NodeTransformer):
    def visit_Import(self, node: ast.Import) -> None:  # noqa: ARG002 - ast.NodeVisitor 的 visit_* 覆写，签名由基类定
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: ARG002 - 同上，NodeVisitor 覆写
        return None

    # `generic_visit` is typed as returning `AST`, which is what these really
    # return -- a visitor may replace a node with a different kind. Declaring the
    # narrower type was a claim the code does not make (T68).
    def visit_Assign(self, node: ast.Assign) -> ast.AST | None:
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if names & PATH_DECLARATIONS:
            return None
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if isinstance(node.target, ast.Name) and node.target.id in PATH_DECLARATIONS:
            return None
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if not isinstance(node.value, str):
            return node
        normalized = node.value
        for legacy, current in PACKAGE_RENAMES.items():
            normalized = normalized.replace(current, legacy)
        return ast.copy_location(ast.Constant(value=normalized), node)


def _normalized(source: str, filename: str) -> str:
    tree = ast.parse(source, filename=filename)
    normalized = SemanticNormalizer().visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, annotate_fields=True, include_attributes=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    arguments = parser.parse_args()
    sources = sorted(
        path
        for path in _git("ls-tree", "-r", "--name-only", arguments.baseline, "src/fastagent")
        .splitlines()
        if path.endswith(".py")
    )
    failures = 0
    for source_path in sources:
        destination = _destination(source_path)
        destination_path = destination.relative_to(ROOT).as_posix()
        before = _normalized(
            _git("show", f"{arguments.baseline}:{source_path}"), source_path
        )
        after = _normalized(
            _git("show", f"{arguments.candidate}:{destination_path}"), destination_path
        )
        digest = hashlib.sha256(after.encode("utf-8")).hexdigest()
        if before == after:
            print(f"PASS {source_path} -> {destination_path} sha256={digest}")
            continue
        failures += 1
        print(f"FAIL {source_path} -> {destination_path}")
        print(
            "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"{arguments.baseline}:{source_path}:normalized-ast",
                    tofile=f"{arguments.candidate}:{destination_path}:normalized-ast",
                    lineterm="",
                )
            )
        )
    if len(sources) != 29:
        print(f"FAIL expected 29 baseline modules, found {len(sources)}")
        return 1
    if failures:
        print(f"FAIL {failures}/{len(sources)} modules differ beyond imports/package paths")
        return 1
    print("29/29 模块内容等价,仅路径类改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
