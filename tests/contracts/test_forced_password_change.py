"""ADR-0048 §二.5 (K8): exactly one route may run while a password change is owed.

The enforcement lives in the authentication dependency, which is what makes it
inherited: a route added next year gets the 403 without knowing the rule exists.
That design has one failure mode, and it is quiet -- a second route reaching for
`authenticated_pending_password_change` would work while its caller is locked
out, and nothing about that route's own diff would look wrong.

So this reads `app.py` and counts. It is a birth gate, not a behaviour test:
`tests/integration/test_password_reset_route.py` covers what the 403 does; this
covers the one thing a behaviour test cannot see, which is a route that has not
been written yet.

Parsed rather than grepped, for the reason `check_static.py`'s route gate gives:
a dependency named in a comment or a docstring is not a dependency, and a string
search cannot tell the difference.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"

#: The dependency that skips the `must_change_password` check.
LENIENT = "authenticated_pending_password_change"

#: The one route allowed to use it -- freeze B1 #30, added by ADR-0052.
PERMITTED_ROUTE = "/auth/change-password"


def _route_path(function: ast.FunctionDef) -> str | None:
    """The path from an `@app.<method>("...")` decorator, if there is one."""

    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if not isinstance(target, ast.Attribute):
            continue
        holder = target.value
        if not isinstance(holder, ast.Name) or holder.id != "app":
            continue
        if not decorator.args:
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _dependencies(function: ast.FunctionDef) -> set[str]:
    """Every `Depends(<name>)` appearing among this function's parameter defaults."""

    found: set[str] = set()
    defaults = [*function.args.defaults, *function.args.kw_defaults]
    for default in defaults:
        if not isinstance(default, ast.Call):
            continue
        if not isinstance(default.func, ast.Name) or default.func.id != "Depends":
            continue
        for argument in default.args:
            if isinstance(argument, ast.Name):
                found.add(argument.id)
    return found


def _functions() -> list[ast.FunctionDef]:
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def test_only_the_change_password_route_skips_the_forced_change_check() -> None:
    """One user, and it is the route whose whole job is to end the condition.

    A second user would be a route that answers normally to somebody who,
    by K8, may do nothing until they have replaced a password chosen for them
    by someone else -- which is the guarantee, not a detail of it.
    """

    users = {function.name for function in _functions() if LENIENT in _dependencies(function)}

    assert users == {"change_password"}, (
        f"{LENIENT} must be used by exactly one route; found {sorted(users)}. "
        "A route that skips the forced-password-change check while its caller "
        "owes one is ADR-0048 K8 undone"
    )


def test_the_permitted_user_is_the_route_it_is_named_for() -> None:
    """`change_password` must actually be `POST /auth/change-password`.

    The check above is by function name, so a rename that moved the decorator
    elsewhere would keep it green. This pins the pair.
    """

    matches = [
        function
        for function in _functions()
        if function.name == "change_password" and _route_path(function) == PERMITTED_ROUTE
    ]

    assert len(matches) == 1, (
        f"expected exactly one route function named change_password serving "
        f"{PERMITTED_ROUTE!r}; found {len(matches)}"
    )
    assert LENIENT in _dependencies(matches[0])


def test_every_other_authenticated_route_takes_the_enforcing_dependency() -> None:
    """The other half: no route may quietly take neither.

    A route whose only dependency is `bearer` -- or one that decoded the token
    itself -- would authenticate without ever consulting `must_change_password`,
    and the two checks above would both stay green. So every route function that
    depends on anything at all must depend on one of the two, and only one of
    them is lenient.

    Routes with no dependency at all are listed rather than inferred: they are
    the ones that face a caller with no subject, and each is already exempted by
    name in `check_static.py`'s `ROUTES_WITHOUT_INLINE_AUTHORIZE` for the same
    underlying reason.
    """

    # **`/auth/refresh` joins this list, and the reason is worth stating.**
    # It faces a caller with no subject *and no bearer token* -- the access
    # token it exists to replace has expired -- so there is no `AuthContext` for
    # either dependency to build on. K8 is not weakened by that: a refreshed
    # access token opens nothing while `must_change_password` is true, because
    # **every route that reads or writes anything still takes the enforcing
    # dependency and still answers 403**. What refresh keeps alive is the
    # session, not the access; the person still cannot do anything except change
    # their password.
    without_a_subject = {"/healthz", "/auth/login", "/auth/refresh", "/ws"}
    offenders = []
    examined = 0
    for function in _functions():
        path = _route_path(function)
        if path is None or path in without_a_subject:
            continue
        examined += 1
        dependencies = _dependencies(function)
        if "authenticated" not in dependencies and LENIENT not in dependencies:
            offenders.append(f"{function.name} ({path})")

    # **The control.** A `_route_path` that quietly stopped matching would make
    # the loop above examine nothing and this test pass for the wrong reason --
    # the exact shape of false green this repository keeps finding. The floor is
    # deliberately well below today's count so that deleting a route is not a
    # failure, and well above zero so that seeing none is.
    assert examined >= 15, (
        f"only {examined} authenticated routes were examined; the decorator walk "
        "has stopped matching and this test is passing vacuously"
    )

    assert not offenders, (
        f"these routes authenticate through neither dependency: {sorted(offenders)}. "
        "K8's 403 is inherited from the dependency, so a route that bypasses it "
        "bypasses the rule"
    )
