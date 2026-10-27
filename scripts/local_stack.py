#!/usr/bin/env python
"""One checkout, one local stack: the project name, and three usable host ports.

**The problem this closes has bitten eight times.** `infrastructure/local/compose.yml`
had a fixed project name and three fixed host ports, so two gate runs on one
machine did not fail to start -- they started and **shared one PostgreSQL and one
pair of Redises**, while `seeded_database` truncates every table in every test.
The failures that produced never looked like what they were: 150 connection
errors one week, 185 the next, and most recently 195 `password authentication
failed for user "fastagent_migrator"` -- because the other run had rebuilt the
database together with *its* ephemeral password. A whole screen of
`OperationalError` reads like a code defect, which is what makes this more
expensive than an outright refusal.

**Identity is the project name; the ports only have to be free.** That split is
the correction the first version of this file needed, and the gate found it:
ports derived purely from a hash landed on **55204**, which Windows had reserved
(Hyper-V and WinNAT exclude blocks of the dynamic range), and Docker refused with
`An attempt was made to access a socket in a way forbidden by its access
permissions` -- a message that says nothing about reserved ranges. A derived port
is therefore a *starting point*, not an answer: the search walks forward from it
until a port actually binds.

Nothing is lost by that, because the port was never what identified a stack. The
**project name** is stable, derived, and is what `docker compose -p` uses to find
containers to tear down or to refuse over. The ports can differ between two runs
in the same directory and both runs still find their own containers.

Both gate scripts call this and read the answer back; neither computes anything
itself. `ci.sh` and `ci.ps1` have drifted from each other before (the
`Idempotency-Key` pattern, the sink address), and two copies of a derivation that
must agree is a defect waiting for a Tuesday.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import socket
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

#: The first port of each service's search band, and the width they share.
#:
#: Disjoint by construction: 20000-20899, 21000-21899, 22000-22899.
#:
#: Two constraints decide these numbers, and the second one was learned the
#: expensive way.
#:
#: **One: entirely below the operating system's dynamic port range**
#: (T165's residual, 2026-09-06). The bands used to be 55000 / 56000 / 57000,
#: which sit *inside* Windows' dynamic range -- measured on the machine this
#: gate runs on:
#:
#:     netsh int ipv4 show dynamicport tcp  ->  start 49152, 16384 ports
#:
#: So every port the gate derived was one the operating system could hand to any
#: outbound connection at any moment. `_bindable()` can only answer "free right
#: now", and `docker compose up -d --wait` binds its last container **seconds**
#: later, after health checks. Two gate runs failed exactly that way -- `address
#: already in use` on 57154 and on 55452, each with no holder visible a moment
#: afterwards. The same measurement showed the old bands were badly sited in a
#: second way: this machine reserves 55121-55220 outright (`netsh interface ipv4
#: show excludedportrange protocol=tcp`), a block inside the old PostgreSQL band.
#:
#: 32768 rather than 49152 is the bar the test holds these to, because Linux's
#: default `ip_local_port_range` starts there and this file should not encode
#: one platform's number.
#:
#: **Two: clear of this repository's own Stage ports** (independent review
#: P1-1). The first attempt at the fix used 15432 / 16379 / 17379 and landed the
#: gate's whole port space **on top of the deployment**: `stage_preflight
#: .PORT_DEFAULTS` fixes 18080 / 18081 / 15432 / 16379 / 16380, ADR-0046 §五.5
#: rules that those numbers stay, and ADR-0046 §2.1 puts the Stage host on
#: **this same machine** (WSL `Ubuntu-24.04`). Two of the three bases *were*
#: Stage ports, all five fell inside the bands, and the very first run on the
#: new numbers took 16380 -- Stage's cache-redis port. Solving one collision by
#: creating another is not solving it.
#:
#: That is why `test_the_bands_avoid_this_repositorys_own_stage_ports` reads
#: `PORT_DEFAULTS` rather than restating the five numbers: two constants that
#: must not overlap should not be able to move independently.
#:
#: **Known collisions this does not remove** -- the list is what is known, not a
#: proof that nothing else exists:
#:
#:   * a port a *previous run of this same project* has not fully released;
#:   * a stack another checkout is genuinely using -- what
#:     `_docker_published_ports()` sees, and only within this Docker daemon;
#:   * anything published by a **different** daemon. `desktop-linux` (this gate)
#:     and the distribution's own `dockerd` (where Stage runs) do not see each
#:     other's published ports, so the static separation above is what keeps
#:     those two apart, not a runtime check.
#:
#: The defaults in `compose.yml` (55432 / 56379 / 56380) are **not** changed by
#: this and are no longer inside any band. They are what a hand-run `docker
#: compose up` gets, and they have the ephemeral-range defect for the same
#: reason -- registered as T168 rather than changed here, because moving a
#: developer-facing default is a different decision from fixing the gate.
POSTGRES_BASE = 20000
QUEUE_REDIS_BASE = 21000
CACHE_REDIS_BASE = 22000
BAND_WIDTH = 900

PROJECT_PREFIX = "fastagent-m0-local"


def _fingerprint(root: Path) -> str:
    """A stable digest of the checkout path.

    Case-folded and resolved, because Windows hands the same directory back with
    different capitalisation depending on who asked, and two spellings of one
    path must not produce two stacks.
    """

    resolved = str(root.resolve()).casefold()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def _bindable(port: int) -> bool:
    """Can this process actually listen here right now?

    Bound on `0.0.0.0` because that is what Docker's port publishing does; a
    probe on `127.0.0.1` would pass for ports the daemon then cannot use.

    Two failures are being detected and they are not the same: a port already
    in use (another stack, another program) and a port **reserved by Windows**
    (`WinNAT`/Hyper-V exclusion ranges, which move on reboot). The second is the
    one that cost this batch a gate run, and it surfaces from Docker as a
    permissions message that mentions nothing about reservations.

    `SO_REUSEADDR` is deliberately not set: on Windows it lets a socket bind a
    port another process is already using, which would make this probe answer
    yes for exactly the ports it is meant to reject.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))  # noqa: S104 - matching Docker's own binding
    except OSError:
        return False
    finally:
        probe.close()
    return True


#: `0.0.0.0:55221->5432/tcp` and `[::]:55221->5432/tcp` are both shapes Docker
#: prints; the host port is the number before the arrow.
_PUBLISHED = re.compile(r":(\d+)->")


@lru_cache(maxsize=1)
def _docker_published_ports() -> frozenset[int]:
    """Host ports **this Docker daemon** has already published.

    Narrower than it used to claim (independent review P2-1). `docker` here is
    whichever context is current -- `desktop-linux` on this machine -- and a
    container published by another daemon is invisible to it. That matters
    because ADR-0046 §2.1 puts the Stage host on this same machine, in a WSL
    distribution running **its own** dockerd: the two never see each other, and
    `_bindable()` does not close the gap either (it probes Windows-side
    `0.0.0.0`, the other side binds inside the distribution). Keeping the
    bands clear of `stage_preflight.PORT_DEFAULTS` is what separates those two,
    not this function.

    What it does answer, and `_bindable` cannot:

    **This exists because the bind probe answered "free" for a port another
    checkout's stack was holding, and the gate then ran its whole suite against
    that stack's database** (T165, 2026-09-06). On Windows, Docker publishes
    through a WinNAT proxy, and an ordinary process can `bind()` a port Docker
    has published -- so the probe below is not wrong, it is *blind*, and the
    walk in `_pick_port` never fired for the one kind of collision it exists to
    avoid.

    The failure that produced was 51 tests reporting
    `channel authentication failed`: the suite had connected to a database
    belonging to a different checkout, with none of this run's seed data. That
    is the eight-incident "two checkouts, one database" failure returning
    through a different door, and the symptom pointed nowhere near the cause.

    **A missing or failing `docker` is not an error here.** This is a second
    opinion, not the authority: if it cannot be obtained, the bind probe still
    applies and behaviour is exactly what it was before this function existed.
    Refusing to derive a stack because `docker ps` was slow would replace a rare
    wrong answer with a common outright failure.
    """

    docker = shutil.which("docker")
    if docker is None:
        return frozenset()
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [docker, "ps", "--format", "{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(int(match) for match in _PUBLISHED.findall(result.stdout))


def _usable(port: int) -> bool:
    """Both opinions have to agree, and they answer different questions.

    `_bindable` asks "can this process listen here right now", which catches
    ports held by ordinary programs and ports Windows has reserved.
    `_docker_published_ports` asks "has Docker already given this port to a
    container", which is the question the first one cannot answer.
    """

    return port not in _docker_published_ports() and _bindable(port)


def _pick_port(base: int, offset: int) -> int:
    """The derived port if it works, otherwise the next one in the band that does.

    Deterministic given the machine's state: the walk always starts at the same
    place and always goes the same way, so two runs in one directory get the same
    port unless something has changed about which ports are usable.

    Raises rather than falling outside the band. A search that wandered out would
    trade a loud failure for a port nobody reasoned about -- and the band is 900
    wide, so exhausting it means something is wrong that a different port will
    not fix.
    """

    for step in range(BAND_WIDTH):
        candidate = base + (offset + step) % BAND_WIDTH
        if _usable(candidate):
            return candidate
    raise RuntimeError(
        f"no usable port in {base}..{base + BAND_WIDTH - 1}. Either a great many "
        "stacks are running (docker ps -- `devloop` does not clean up after "
        "itself), or this range is reserved on this machine "
        "(on Windows: netsh interface ipv4 show excludedportrange protocol=tcp)"
    )


def stack_identity(root: Path, *, probe: bool = True) -> dict[str, str]:
    """Everything a gate run needs to own its own stack, keyed for `KEY=VALUE` output.

    `probe=False` exists for the tests, which are about the derivation and must
    not depend on which ports happen to be free on the machine running them.
    """

    digest = _fingerprint(root)
    # 16 bits is plenty for a 900-wide band and keeps the arithmetic readable.
    offset = int(digest[:4], 16) % BAND_WIDTH
    bands = (
        ("FASTAGENT_LOCAL_PG_PORT", POSTGRES_BASE),
        ("FASTAGENT_LOCAL_QUEUE_REDIS_PORT", QUEUE_REDIS_BASE),
        ("FASTAGENT_LOCAL_CACHE_REDIS_PORT", CACHE_REDIS_BASE),
    )
    identity = {"FASTAGENT_COMPOSE_PROJECT": f"{PROJECT_PREFIX}-{digest[:8]}"}
    for name, base in bands:
        identity[name] = str(_pick_port(base, offset) if probe else base + offset)
    return identity


def main() -> None:
    """Print `KEY=VALUE` lines for the checkout named on the command line.

    Plain lines rather than JSON or shell syntax: `ci.sh` reads them with `read`
    and `ci.ps1` with `-split '='`, and neither needs a parser or an `eval`. An
    `eval` of script-generated text in the one script that must not be
    surprising is a bad trade for four values.

    **Bytes and an explicit LF, and that is the whole reason the POSIX gate
    worked at all** (independent review 2026-09-05, P1-1). On Windows, Python's
    text stdout turns a line feed into CR LF **including through a pipe**, and
    `ci.sh` reads these lines with `while IFS='=' read -r`, which keeps the CR
    inside the value. Every derived value then carried one: a project name
    ending in CR (compose validates project names), a port mapping reading
    `"55224<CR>:5432"`, and a DSN reading `...@localhost:55224<CR>/fastagent`
    -- three separate failures, none of whose messages mentions a carriage
    return.

    `ci.ps1` was unaffected, because PowerShell splits a child process's output
    into lines before handing them over. That is exactly why the batch's own
    1099 was green and this was still broken: **the evidence only ever exercised
    half the fix.**
    """

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    lines = "".join(f"{key}={value}\n" for key, value in stack_identity(root).items())
    sys.stdout.buffer.write(lines.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
