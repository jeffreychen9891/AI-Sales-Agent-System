from __future__ import annotations

import os
import secrets
import socket
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.auth import hash_password, sha256_token
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel_sink import app as channel_sink_app
from psycopg import Connection
from psycopg.types.json import Jsonb
from redis import Redis

ROOT = Path(__file__).resolve().parents[1]
TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EMPLOYEE_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
EMPLOYEE_B = UUID("bbbbbbbb-0000-4000-8000-000000000001")
PROFILE_RELEASE = UUID("11111111-1111-4111-8111-000100000004")
CHANNEL_TOKEN = "test-only-channel-token-with-32-bytes"
LOGIN_PASSWORD = "local-only-strong-password"


SINK_READY_TIMEOUT_SECONDS = 15.0


def _reserve_loopback_socket() -> socket.socket:
    """A listening socket on a port the operating system picked.

    Binding to port 0 and then handing **this same socket** to uvicorn is what
    makes the allocation safe: asking the OS for a free port and then binding it
    again by number leaves a window in which another process on the machine can
    take it, which is the class of race this fixture exists to end. `SO_REUSEADDR`
    is deliberately not set -- on Windows it lets a second process bind a port
    that is already in use, which would reintroduce exactly the sharing this
    removes.
    """

    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen()
    return reserved


def _wait_until_serving(base_url: str, worker: Thread) -> None:
    deadline = time.monotonic() + SINK_READY_TIMEOUT_SECONDS
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            if not worker.is_alive():
                raise RuntimeError(f"channel sink exited before serving {base_url}")
            try:
                if client.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.02)
    raise RuntimeError(f"channel sink did not become ready at {base_url}")


@pytest.fixture(scope="session", autouse=True)
def channel_sink() -> Iterator[str]:
    """Run the M0 channel simulator for this session, on a port nobody else has.

    The address used to be the literal `127.0.0.1:58090` written out in three
    unconnected places -- here, `scripts/ci.sh` and `scripts/ci.ps1` -- and
    `FASTAGENT_CHANNEL_SEND_URL` did not reach the one here at all. Two
    consequences, both of which have cost real runs (registered as T82):

    - two checkouts on one machine could not run their gates at the same time,
      because the second sink could not bind and the second gate refused to start;
    - worse, when they *did* line up, one run was tested against a sink the other
      had started. A pass obtained that way is not evidence, and one has already
      had to be thrown out.

    Owning the sink here removes both by construction rather than by probing for
    them: the port comes from the OS, no other process is told about it, and
    `FASTAGENT_CHANNEL_SEND_URL` is written from the socket that is actually
    listening -- so "the address the tests use" and "the address the sink is on"
    are the same fact rather than two literals that have to agree. It also means
    `pytest` runs on its own, without a sink started by hand beforehand.
    """

    reserved = _reserve_loopback_socket()
    base_url = "http://{}:{}".format(*reserved.getsockname())
    server = uvicorn.Server(
        uvicorn.Config(channel_sink_app, log_level="warning", access_log=False)
    )
    worker = Thread(target=server.run, kwargs={"sockets": [reserved]}, daemon=True)
    worker.start()
    previous = os.environ.get("FASTAGENT_CHANNEL_SEND_URL")
    try:
        _wait_until_serving(base_url, worker)
        # Set after the sink answers, so nothing can be pointed at an address
        # that is not yet serving.
        os.environ["FASTAGENT_CHANNEL_SEND_URL"] = f"{base_url}/send"
        yield base_url
    finally:
        if previous is None:
            os.environ.pop("FASTAGENT_CHANNEL_SEND_URL", None)
        else:
            os.environ["FASTAGENT_CHANNEL_SEND_URL"] = previous
        server.should_exit = True
        worker.join(timeout=SINK_READY_TIMEOUT_SECONDS)
        reserved.close()


#: 本机缺了就补的那几个 —— **恰好是 `Settings.from_env("api")` 的必填集**，
#: 一个不多。实测方法是逐个补到它建得出来为止（见 `signing_key_default` 的注释）。
#:
#: **两类东西，给值的理由不同**：
#: * `FASTAGENT_ACCESS_SIGNING_KEY` / `FASTAGENT_ALLOWED_WS_ORIGINS` ——
#:   一个是密钥（本机只用来发/验自己签的 token，任何够长的值都对），
#:   一个是配置串。给它们一个值**不隐藏任何东西**。
#: * `FASTAGENT_DATABASE_URL` / `FASTAGENT_CACHE_REDIS_URL` —— **连接串**。
#:   给它们假值的风险是把「你没有配置」变成「你没有数据库」，
#:   而后者报得更晚、更难读。所以这两个用的是**自报身份的哨兵地址**：
#:   谁真去连它，报错里就有 `fixture-not-a-real-host`，一眼看出是谁给的。
#:   （本机受影响的那 9 条用例一个都不连库 —— 它们只建 settings、发/验 token。）
API_ROLE_DEFAULTS = {
    "FASTAGENT_ACCESS_SIGNING_KEY": None,  # 运行时生成，见下
    "FASTAGENT_ALLOWED_WS_ORIGINS": "http://localhost:3000",
    "FASTAGENT_DATABASE_URL": "postgresql://fixture:fixture@fixture-not-a-real-host:5432/none",
    "FASTAGENT_CACHE_REDIS_URL": "redis://fixture-not-a-real-host:6379/0",
}


def api_role_env_defaults(environ: Mapping[str, str]) -> dict[str, str]:
    """本机要补的那几个变量与值；**已经有的一个都不动**。

    **写成纯函数，好让「不覆盖」这件事能被单独钉住。** 下面那个 fixture 是
    session 作用域、一轮只跑一次，用它自己没法在一次跑里同时验
    「缺席时补」与「已有时不动」——
    而「在门禁里不覆盖」正是这条机制能不能算数的全部
    （`tests/contracts/test_api_role_env_defaults.py`）。

    签名密钥每次生成一个新的（`Settings` 要求至少 32 字节），
    而不是写一个常量：一个写死在仓里的测试密钥，早晚会有人拿去别处用。
    """

    missing: dict[str, str] = {}
    for name, value in API_ROLE_DEFAULTS.items():
        if environ.get(name):
            continue
        missing[name] = secrets.token_urlsafe(36) if value is None else value
    return missing


@pytest.fixture(scope="session", autouse=True)
def api_role_settings_are_buildable() -> Iterator[None]:
    """本机补齐 `Settings.from_env("api")` 的必填集，跑完还原。

    **为什么不是 `skipif`。** 在此之前，本机跑 `tests/contracts/` 固定 **9 条红**
    （`test_superuser_allowlist` 全部），成因是它们调 `Settings.from_env("api")`
    而本机没有那几个变量。于是每次读失败数都要先减掉 9 ——
    **而「9 是预期」这个心算就是出错的地方**（2026-09-13 实测两次：
    一次把 10 failed 读成预期，并把「1512 passed」写进了提交说明）。
    `skipif` 消掉的是那 9 条本身，本 fixture 消掉的是那个心算：
    **本机失败数从此等于真实失败数，而覆盖一条不少。**

    **它在门禁里什么都不做，而这是机制保证的，不是靠人记得配对**：
    `ci.sh` 与 `ci.ps1` 都 `export` 了真值，于是 `api_role_env_defaults` 回空字典，
    本 fixture 一个变量都不设。「被绕过的东西在门禁里必须仍然跑」那条约束
    因此自动成立 —— **没有任何用例被关掉，门禁里跑的仍是门禁给的那些值。**

    **本 fixture 只管这一个角色的必填集，其余缺省是有意留着的。**
    本机全树还有 40 个采集错误：39 个也是签名密钥，但它们发生在**导入期**
    （模块级就建 settings），而 fixture 跑在采集之后，所以本 fixture 碰不到它们 ——
    它们属于 `tests/integration/`，本来就要栈；剩下 1 个缺的是
    `FASTAGENT_INGRESS_DATABASE_URL`，同理。
    **本机全绿不是目标**：把「要栈的东西在没有栈时红」抹掉才是坏事。

    **它代偿掉的那条要求，本批一起补上了。** 在此之前，「缺这几个变量时
    `Settings.from_env` 必须报错」这条行为**全仓没有任何用例** ——
    那 9 条红是它唯一的证据，而本 fixture 正是要拿掉那个证据。
    所以同一批加了 `test_api_role_env_defaults.py` 里那条
    `test_settings_still_refuses_to_start_without_the_signing_key`。
    （`AGENTS.md`「一个长期存在的失败，可能正在代偿一条没有用例的要求」。）
    """

    additions = api_role_env_defaults(os.environ)
    previous = {name: os.environ.get(name) for name in additions}
    os.environ.update(additions)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="session")
def settings(channel_sink: str) -> Settings:
    """Process settings, built after the sink exists.

    The dependency is declared rather than left to the autouse ordering: this
    fixture reads `FASTAGENT_CHANNEL_SEND_URL`, and the sink is what puts the
    real one there.
    """

    assert channel_sink  # the sink is a precondition, not an unused argument
    return Settings.from_env()


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


#: How far the database clock may lead or trail this process before the suite
#: refuses to run. **Not a tolerance for correctness** -- the fixtures are
#: written so that no test depends on the two clocks agreeing. It is a
#: diagnosis: past this, something that compares a stored timestamp against
#: `datetime.now()` will misbehave somewhere, and a named failure here is worth
#: more than whatever unrelated assertion happens to break first.
CLOCK_SKEW_LIMIT_SECONDS = 2.0


def _assert_clocks_agree(connection: Connection) -> None:
    """Fail as a clock problem, rather than as whatever it happens to break (T186).

    Cost this repository real time once already: the database clock ran ahead of
    the process clock, freshly seeded channel bindings were therefore "not yet
    valid", and every affected test reported `channel authentication failed` --
    which reads as a credential, rotation or RLS fault. The investigation went
    to hashing first, and the offset was 430 milliseconds.

    The set of tests that break moves with the offset (it depends on how long
    each one waits between seeding and sending), so the same cause looks like a
    different flaky test each run. That is the property this check exists to
    remove.
    """

    process_before = time.time()
    database_now = float(
        connection.execute("SELECT extract(epoch from now())").fetchone()[0]
    )
    process_after = time.time()
    # Compare against the interval the query ran in, so the round trip itself is
    # not counted as skew.
    if database_now < process_before:
        skew = database_now - process_before
    elif database_now > process_after:
        skew = database_now - process_after
    else:
        return
    if abs(skew) > CLOCK_SKEW_LIMIT_SECONDS:
        # **The remedy this message used to name does not exist** (2026-09-07).
        # It said to run `wsl -d docker-desktop -e hwclock -s`; that distribution
        # has no `hwclock`, so the advice failed with `execvpe(hwclock) failed`
        # for whoever followed it. Setting the clock directly
        # (`wsl -d docker-desktop -e date -u -s ...`) does run, and does not
        # help: the offset was back over a second within twelve seconds.
        #
        # What is measured (T193): the offset oscillates rather than drifting one
        # way -- `-0.858s` to `+1.538s` over sixty samples, roughly a thirty
        # second period, and it has been seen past this limit under sustained
        # load. So the honest advice is to say what is known and stop there,
        # rather than send the next person after a command that cannot work.
        raise AssertionError(
            f"database clock differs from this process by {skew:+.3f}s "
            f"(limit {CLOCK_SKEW_LIMIT_SECONDS}s). This is a host clock problem, "
            "not a test failure. On Docker Desktop the VM clock oscillates "
            "against the host (T193: measured -0.9s..+1.5s on a ~30s period, "
            "wider under sustained load). No in-VM fix is known: `hwclock` is "
            "not present, and `date -s` does not hold. Restarting the Docker "
            "Desktop VM re-syncs it -- but that kills every running stack, so "
            "check `docker ps` for another checkout's gate first."
        )


@pytest.fixture()
def seeded_database(settings: Settings, channel_sink: str) -> Iterator[dict[str, object]]:
    # The profile is published through the production path (registry -> publisher
    # -> app database role), not inserted by the fixture. Before 006 the only
    # INSERT into `profile_releases` in the repository was here, which is exactly
    # the gap T10 closes; seeding through the real path means every test run
    # exercises it.
    package = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    )
    # **`platform_audit_events` is here for the same reason `audit_events` is, and
    # it was added the day the table was** (B1 #8). It is not reached by
    # `CASCADE` -- nothing references `tenants` from it, deliberately, because a
    # platform event names no tenant -- so without naming it explicitly its rows
    # survive every reset. The first symptom is a count assertion that passes
    # alone and fails in a full run, which reads as a flaky test rather than as
    # a table nobody clears.
    with Connection.connect(_migration_url()) as connection:
        _assert_clocks_agree(connection)
        connection.execute(
            """
            TRUNCATE audit_events, platform_audit_events, delivery_receipts,
                     outbox_messages, policy_decisions,
                     intent_records, manual_tasks, scheduled_events, inbox_events,
                     conversation_aggregates,
                     ws_tickets, employee_sessions, channel_bindings, employees,
                     profile_releases, tenants CASCADE
            """
        )
        connection.execute(
            "INSERT INTO tenants(id, code) VALUES (%s, 'tenant-a'), (%s, 'tenant-b')",
            (TENANT_A, TENANT_B),
        )
        # Committed before publishing: the publisher connects as the API role on
        # its own connection and cannot see an uncommitted tenant row.
        connection.commit()

        app_database = Database(settings.database_url)
        for tenant_id in (TENANT_A, TENANT_B):
            published = publish_release(
                database=app_database, tenant_id=tenant_id, package=package
            )
            assert published.created
            assert published.release.release_id == PROFILE_RELEASE

        password_hash = hash_password(LOGIN_PASSWORD)
        # **`session.read` was here until B1 #16** and is gone with the action
        # itself: `GET /auth/me` stopped asking for a permission (T125), so the
        # catalog no longer carries one for it and a seed granting it would be
        # granting a string nothing consults. `employee.list` replaces it as the
        # seed's ordinary tenant-scoped read -- several cases withhold a
        # permission from this set and assert the withholding actually removed
        # something, which a dead string cannot do.
        permissions = Jsonb(["employee.list", "websocket_ticket.create"])
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            )
            VALUES (%s, %s, 'operator-a@example.com', %s, %s, 'Operator A'),
                   (%s, %s, 'operator-b@example.com', %s, %s, 'Operator B')
            """,
            (
                TENANT_A,
                EMPLOYEE_A,
                password_hash,
                permissions,
                TENANT_B,
                EMPLOYEE_B,
                password_hash,
                permissions,
            ),
        )
        connection.execute(
            """
            -- **`current_valid_from` is pinned into the past on purpose** (T186).
            --
            -- Its default is the *database's* `now()`, while `accept()` compares
            -- against the *process's* `datetime.now(UTC)`. Whenever the database
            -- clock runs even slightly ahead -- 430ms was enough, measured -- a
            -- binding seeded here is "not yet valid" for the first tests that
            -- use it, and they fail as `channel authentication failed`. The set
            -- that fails moves with the offset, so it reads as flaky while being
            -- entirely deterministic.
            --
            -- An hour is chosen to be larger than any plausible offset between a
            -- container's clock and the host's, and it costs nothing: no test
            -- asserts on this column's value.
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash,
                current_valid_from
            ) VALUES ('config-a', %s, %s, %s, now() - interval '1 hour')
            """,
            (TENANT_A, PROFILE_RELEASE, sha256_token(CHANNEL_TOKEN)),
        )
        connection.commit()

    queue = Redis.from_url(settings.queue_redis_url, decode_responses=True)
    queue.flushdb()
    queue.close()
    # **The failed-attempt windows too.** They live in the cache Redis for
    # fifteen minutes by design, so without this a case that spends attempts
    # leaves them for whatever runs next -- and the resulting 429 would surface
    # in an unrelated file, reading like a limiter defect rather than test
    # coupling. Keys only, not `flushdb`: this Redis is the cache, and a future
    # user of it should not have its data dropped by an employee fixture.
    cache = Redis.from_url(settings.cache_redis_url, decode_responses=True)
    for key in cache.scan_iter("fastagent:v2:throttle:*"):
        cache.delete(key)
    cache.close()
    with httpx.Client(trust_env=False) as client:
        # From the fixture that owns the sink, not from a literal. The literal
        # here used to be a second, unconnected copy of the address -- so a run
        # whose sink was somewhere else reset *nothing* and still went green,
        # while a run that shared the port reset another run's state mid-flight.
        client.post(f"{channel_sink}/test/reset", timeout=5).raise_for_status()
    yield {
        "tenant_a": TENANT_A,
        "tenant_b": TENANT_B,
        "employee_a": EMPLOYEE_A,
        "employee_b": EMPLOYEE_B,
        "profile_release": PROFILE_RELEASE,
        "channel_token": CHANNEL_TOKEN,
        "login_password": LOGIN_PASSWORD,
    }


@pytest.fixture()
def quiet_hours_out_of_the_way(seeded_database: dict[str, object]) -> None:
    """Rebind the seeded tenant to a release whose quiet window cannot bite.

    **Named in a signature rather than applied globally, because it changes what
    the profile says** and a reader of one of these cases has to be able to see
    that.

    Both shipped profiles declare `22:30-08:00 Asia/Shanghai`. Until B4-5 that
    was inert -- nothing read `quiet_hours`. Since B4-5 it decides two things: a
    timer turn inside the window **sends nothing at all**, and from 22:00 the
    window's opening becomes the row's `window_expires_at` (it is the earlier of
    that and the 30-minute active window). **So every case that fires a timer
    and expects a message silently acquired a "not at night" precondition**, and
    a gate run after 22:30 Shanghai would have failed on work that is correct.

    That is not hypothetical: it surfaced at 22:10 during B4-5's own probe run,
    on a case that had passed every earlier gate purely because they finished
    before 22:00.

    The window is pushed six hours out rather than removed -- `quiet_hours` is a
    required part of the contract and an absent one is refused at parse time, so
    "no quiet hours" is not a state a profile can be in. Cases that are *about*
    quiet hours publish their own window instead of taking this.
    """

    import copy  # noqa: PLC0415 - test-only
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415 - test-only

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    now = datetime.now(UTC)
    opens = (now + timedelta(hours=6)).replace(second=0, microsecond=0)
    lifts = (now + timedelta(hours=8)).replace(second=0, microsecond=0)
    with Connection.connect(_migration_url()) as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (TENANT_A, PROFILE_RELEASE),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row[0])
        profile["proactivity_policy"]["quiet_hours"] = {
            "timezone": "UTC",
            "ranges": [{"start": opens.strftime("%H:%M"), "end": lifts.strftime("%H:%M")}],
        }
        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        profile["identity"]["version"] = "1.0.9"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (TENANT_A, release_id, row[1], "1.0.9", canonical_hash(profile), Jsonb(profile)),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, TENANT_A),
        )
        connection.commit()
