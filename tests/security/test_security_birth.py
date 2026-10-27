from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import IngressAuthenticationError, WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.redaction import (
    REDACTED,
    REDACTION_FAILED,
    is_sensitive_key,
    is_url_key,
    redact,
    redact_url,
    redacted_repr,
)
from fastagent_v2.runtime.settings import OFFLINE_MODEL_PROVIDERS, Settings, SettingsError
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import callback


@pytest.mark.security
def test_recursive_redaction_covers_nested_and_camel_case_values() -> None:
    value = {
        "password": "plain",
        "nested": [{"accessToken": "access", "safe": "visible"}],
        "headers": {"Authorization": "Bearer secret"},
    }
    result = redact(value)
    assert result["password"] == REDACTED
    assert result["nested"][0]["accessToken"] == REDACTED
    assert result["nested"][0]["safe"] == "visible"
    assert result["headers"]["Authorization"] == REDACTED
    assert "token=ok" not in redact_url("https://example.test/callback?token=ok&safe=yes")
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in redact_url(
        "https://example.test/wecom_proxy/config-a/abcdefghijklmnopqrstuvwxyz0123456789"
    )


DB_PASSWORD = "sentinel-database-password"  # noqa: S105 - fixture sentinel, not a credential
REDIS_PASSWORD = "sentinel-redis-password"  # noqa: S105 - fixture sentinel, not a credential
SIGNING_KEY = "sentinel-signing-key-0123456789abcdef"  # noqa: S105 - fixture sentinel


def _settings_with_sentinels() -> Settings:
    return Settings(
        environment="ci",
        database_url=f"postgresql://fastagent_app:{DB_PASSWORD}@localhost:55432/fastagent",
        ingress_database_url=f"postgresql://fastagent_ingress:{DB_PASSWORD}@localhost:55432/f",
        worker_database_url="",
        scheduler_database_url="",
        queue_redis_url=f"redis://:{REDIS_PASSWORD}@localhost:56379/0",
        cache_redis_url="",
        access_signing_key=SIGNING_KEY,
        model_provider="mock",
        channel_send_url="http://channel-sink.invalid/send",
        allowed_ws_origins=frozenset({"http://localhost:3000"}),
    )


@pytest.mark.security
def test_settings_never_render_a_credential_in_any_string_form() -> None:
    """T13. The generated dataclass repr printed all seven credentials in the clear.

    It reaches archived output nobody chose to send it to: a fixture that raises
    prints the whole `Settings` object in the pytest traceback.
    """

    settings = _settings_with_sentinels()
    for rendered in (repr(settings), str(settings), f"{settings}", format(settings)):
        assert DB_PASSWORD not in rendered
        assert REDIS_PASSWORD not in rendered
        assert SIGNING_KEY not in rendered
        assert rendered.count(REDACTED) >= 4
    # Still useful: everything that is not a credential survives, so the line can
    # answer "which database was this process pointed at".
    assert "fastagent_app" in repr(settings)
    assert "localhost:55432/fastagent" in repr(settings)
    assert "ingress_database_url=" in repr(settings)
    assert "environment='ci'" in repr(settings)
    # A role that is not issued a URL shows an empty one rather than a redaction.
    assert "worker_database_url=''" in repr(settings)


@pytest.mark.security
def test_redaction_covers_url_credentials_and_key_shaped_names() -> None:
    """The two gaps that made `redact()` unable to back the fix above.

    `redact_url` returned the authority component verbatim, and `_SENSITIVE_KEYS`
    matched by equality, so `access_signing_key` was not any of its words.
    """

    assert DB_PASSWORD not in redact_url(f"postgresql://app:{DB_PASSWORD}@host:5432/db")
    assert redact_url(f"postgresql://app:{DB_PASSWORD}@host:5432/db").startswith(
        f"postgresql://app:{REDACTED}@host:5432/db"
    )
    # A URL with a user but no password keeps the user and gains no redaction.
    assert redact_url("postgresql://app@host:5432/db") == "postgresql://app@host:5432/db"

    result = redact(
        {
            "access_signing_key": SIGNING_KEY,
            "api_key": "k",
            "database_url": f"postgresql://app:{DB_PASSWORD}@host:5432/db",
            "channel_send_url": "http://channel-sink.invalid/send",
            "safe": "visible",
        }
    )
    assert result["access_signing_key"] == REDACTED
    assert result["api_key"] == REDACTED
    assert DB_PASSWORD not in result["database_url"]
    assert result["database_url"].endswith("@host:5432/db")
    assert result["channel_send_url"] == "http://channel-sink.invalid/send"
    assert result["safe"] == "visible"


@pytest.mark.security
def test_redaction_of_a_settings_object_matches_its_repr() -> None:
    """`redact()` is the repository's designated log-redaction entrance.

    It is not, today, what logging actually calls -- a full-repo grep finds no
    production caller, and `wecom_proxy/client.py` hands a raw dict to `logger`
    without passing through here (independent review 2026-09-02, P2-3; wiring
    that up is registered as **T20**). The claim this test makes is narrower and
    true: `redact()` and `repr` both lead to the same archived output, so
    neither may be the weaker one.
    """

    redacted = str(redact(_settings_with_sentinels()))
    assert DB_PASSWORD not in redacted
    assert REDIS_PASSWORD not in redacted
    assert SIGNING_KEY not in redacted


@dataclass(frozen=True)
class _FutureSettings:
    """Field names nobody thought to list -- which is the whole point.

    None of these end in `_url` or `_key`, so the first version of
    `Settings.__repr__` rendered all three in the clear while `redact()` hid
    them (independent review 2026-09-02, P1-1).
    """

    base_url: str
    channel_app_secret: str
    bot_token: str
    redis_password: str
    access_token_ttl_seconds: int


@pytest.mark.security
def test_the_repr_renderer_hides_credentials_it_was_never_told_about() -> None:
    """The structural half of P1-1: no manual list inside the fix.

    `redacted_repr` is applied to a dataclass this repository does not contain,
    whose field names match none of the suffixes the first draft tested. Whatever
    credential field gets added to `Settings` next is covered by construction,
    not by someone remembering to extend a list.
    """

    rendered = redacted_repr(
        _FutureSettings(
            base_url="https://api.example.test/v1",
            channel_app_secret=SIGNING_KEY,
            bot_token=SIGNING_KEY,
            redis_password=REDIS_PASSWORD,
            access_token_ttl_seconds=900,
        )
    )
    assert SIGNING_KEY not in rendered
    assert REDIS_PASSWORD not in rendered
    assert rendered.count(REDACTED) == 3
    # The `isinstance(value, str)` guard: an integer knob whose name carries the
    # `token` marker keeps its value, because an integer is not a credential.
    assert "access_token_ttl_seconds=900" in rendered
    assert "base_url='https://api.example.test/v1'" in rendered


@pytest.mark.security
def test_every_settings_field_judged_sensitive_is_absent_from_the_repr() -> None:
    """The same property, held over the real `Settings` field set.

    Walks `fields(Settings)` rather than naming fields, so adding one cannot
    quietly escape the check.
    """

    settings = _settings_with_sentinels()
    rendered = repr(settings)
    for field in fields(settings):
        value = getattr(settings, field.name)
        if not isinstance(value, str) or not value:
            continue
        if is_sensitive_key(field.name):
            assert value not in rendered, field.name
        elif is_url_key(field.name):
            # A URL with no credential in it is rendered unchanged -- that is
            # the point of `redact_url`. What must hold is that the value went
            # through it, so a credential-bearing one could not survive.
            assert redact_url(value) in rendered, field.name
            if redact_url(value) != value:
                assert value not in rendered, field.name


@pytest.mark.security
def test_redact_url_covers_the_fragment_and_does_not_invent_a_password() -> None:
    """P2-2, P3-1 and P3-2, which are all `redact_url` telling small lies."""

    # P2-2: the fragment is the same hole as the authority. OAuth implicit flows
    # put access tokens there.
    fragment = redact_url(f"https://app.example.test/cb#access_token={SIGNING_KEY}&state=ok")
    assert SIGNING_KEY not in fragment
    assert "state=ok" in fragment
    # A fragment that is not key=value pairs is left alone rather than mangled.
    assert redact_url("https://app.example.test/docs#section-2") == (
        "https://app.example.test/docs#section-2"
    )
    # P3-1: an empty password must not be reported as a redacted one.
    assert redact_url("postgresql://app:@host:5432/db") == "postgresql://app@host:5432/db"
    # P3-2: the marker stays greppable instead of becoming %5BREDACTED%5D.
    assert REDACTED in redact_url(f"https://app.example.test/cb?password={DB_PASSWORD}")


@pytest.mark.security
def test_a_dsn_is_redacted_by_its_shape_whatever_the_key_is_called() -> None:
    """P3-4: key names are a heuristic; the authority section is the shape.

    The 2026-09-01 leak was a DSN in a log line. `conn`, a bare list element and
    `database_urls` (plural) all miss every marker list, and all three used to
    come back with the password intact.
    """

    dsn = f"postgresql://app:{DB_PASSWORD}@host:5432/db"
    result = redact({"conn": dsn, "database_urls": [dsn], "note": "no credentials here"})
    assert DB_PASSWORD not in result["conn"]
    assert DB_PASSWORD not in result["database_urls"][0]
    assert result["note"] == "no credentials here"
    assert DB_PASSWORD not in str(redact([dsn]))


@pytest.mark.security
def test_mock_model_is_rejected_outside_local_and_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTAGENT_MODEL_PROVIDER", "mock")
    for environment in ("stage-m0", "stage", "production"):
        monkeypatch.setenv("FASTAGENT_ENVIRONMENT", environment)
        with pytest.raises(SettingsError, match="mock model"):
            Settings.from_env()
    monkeypatch.setenv("FASTAGENT_ENVIRONMENT", "prod")
    with pytest.raises(SettingsError, match="approved environment"):
        Settings.from_env()


@pytest.mark.security
def test_scripted_replay_model_is_rejected_outside_local_and_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与上面那条同形，挡的是回放替身（`worker/inference_scripted.py`）。

    **为什么它需要自己的一条，而不是把上面那条的参数化一下**：
    `scripted` 比 `mock` 危险一档。`mock` 进了生产，客户收到的是
    「Message received.」—— 一眼就看得出坏了。
    **`scripted` 进了生产，客户收到的是一段写得很像销售的话** ——
    它看起来完全正常，而它答的是别人那一轮的对话。
    """

    monkeypatch.setenv("FASTAGENT_MODEL_PROVIDER", "scripted")
    for environment in ("stage-m0", "stage", "production"):
        monkeypatch.setenv("FASTAGENT_ENVIRONMENT", environment)
        with pytest.raises(SettingsError, match="scripted replay model"):
            Settings.from_env()

    # **另一侧**：`local` / `ci` 里它不该被这道闸拦。
    #
    # 这一半**不断言 `Settings` 建得出来** —— 那取决于本机有没有配连接串
    # （`role="all"` 还要 ingress / worker / scheduler 三个库与队列 Redis），
    # 而那与本条要验的事无关。它断言的是**被拒的理由不是这一条**：
    # 一道「什么都拒」的闸也能让上面那三个断言绿，而它会让整个本地开发停摆。
    for environment in ("local", "ci"):
        monkeypatch.setenv("FASTAGENT_ENVIRONMENT", environment)
        try:
            Settings.from_env()
        except SettingsError as error:
            assert "scripted replay model" not in str(error)


@pytest.mark.security
def test_every_offline_model_provider_is_named_in_this_file() -> None:
    """表里每一支离线实现，上面都要有一条用例钉它被挡在生产之外。

    **按等值判，不按包含判**（`AGENTS.md`「计数类断言写等值，不要写上界」）：
    写成「表里至少有 mock 和 scripted」时，**第三支离线实现加进去不会红**，
    于是它可以在没有任何用例的情况下进生产。

    ⚠️ **这条红的时候不是坏了，是到期了。** 它会在有人往
    `OFFLINE_MODEL_PROVIDERS` 里加第四支实现的那一刻红 —— 那正是该有人
    为那一支写一条「它在 stage/production 被拒」的时刻。
    做法：在本文件照上面两条的形状加一条，然后把那个名字加进下面这个集合。
    """

    assert set(OFFLINE_MODEL_PROVIDERS) == {"mock", "scripted"}, (
        "OFFLINE_MODEL_PROVIDERS changed. This is expected when a new "
        "socket-free inference implementation lands, and it is the moment to "
        "give it its own 'rejected outside local and ci' case in this file -- "
        "then add its name to the set above. Do not widen this to a subset "
        "check: that is what let the new one through without a case."
    )

    # 两条消息必须是两条，不是一条通用的。合并它们会让
    # 「部署漏配了 provider」与「演示那份 env 被带进了部署」指向同一个地方。
    assert len(set(OFFLINE_MODEL_PROVIDERS.values())) == len(OFFLINE_MODEL_PROVIDERS)


@pytest.mark.security
def test_process_role_settings_do_not_load_other_service_credentials() -> None:
    api = Settings.from_env("api")
    assert api.database_url
    assert api.cache_redis_url
    assert api.ingress_database_url == ""
    assert api.worker_database_url == ""
    assert api.scheduler_database_url == ""
    assert api.queue_redis_url == ""
    assert api.channel_send_url == ""
    assert api.model_provider == ""

    ingress = Settings.from_env("ingress")
    assert ingress.ingress_database_url
    assert ingress.queue_redis_url
    assert ingress.cache_redis_url
    assert ingress.database_url == ""
    assert ingress.worker_database_url == ""
    assert ingress.scheduler_database_url == ""
    assert ingress.access_signing_key == ""

    worker = Settings.from_env("worker")
    assert worker.worker_database_url
    assert worker.scheduler_database_url == ""

    scheduler = Settings.from_env("scheduler")
    assert scheduler.scheduler_database_url
    assert scheduler.worker_database_url == ""
    assert scheduler.channel_send_url == ""


@pytest.mark.integration
@pytest.mark.security
def test_rls_hides_tenant_b_and_password_schema_has_no_plaintext(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    database = Database(settings.database_url)
    with database.transaction(tenant_id=tenant_a) as connection:
        own = connection.execute("SELECT id FROM employees ORDER BY id").fetchall()
        cross = connection.execute(
            "SELECT id FROM employees WHERE tenant_id = %s", (tenant_b,)
        ).fetchall()
        own_tenant = connection.execute("SELECT id FROM tenants").fetchall()
    assert len(own) == 1
    assert cross == []
    assert own_tenant == [{"id": tenant_a}]

    with database.transaction(tenant_id=None) as connection:
        assert connection.execute("SELECT id FROM employees").fetchall() == []
        assert connection.execute("SELECT id FROM tenants").fetchall() == []
        resolved_tenant = connection.execute(
            "SELECT id FROM resolve_login_tenant('tenant-a')"
        ).fetchone()
    assert resolved_tenant == {"id": tenant_a}

    worker_database = Database(settings.worker_database_url)
    with worker_database.transaction(tenant_id=tenant_a) as connection:
        own_profiles = connection.execute("SELECT id FROM profile_releases").fetchall()
        cross_profiles = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s", (tenant_b,)
        ).fetchall()
    assert len(own_profiles) == 1
    assert cross_profiles == []
    with worker_database.transaction(tenant_id=None) as connection:
        active_tenants = connection.execute("SELECT id FROM list_active_tenant_ids()").fetchall()
    assert active_tenants == [{"id": tenant_a}, {"id": tenant_b}]

    scheduler_database = Database(settings.scheduler_database_url)
    with scheduler_database.transaction(tenant_id=None) as connection:
        scheduler_tenants = connection.execute(
            "SELECT id FROM list_active_tenant_ids()"
        ).fetchall()
    assert scheduler_tenants == [{"id": tenant_a}, {"id": tenant_b}]
    with scheduler_database.transaction(tenant_id=tenant_a) as connection:
        assert connection.execute("SELECT id FROM scheduled_events").fetchall() == []
    with pytest.raises(InsufficientPrivilege):
        with scheduler_database.transaction(tenant_id=tenant_a) as connection:
            connection.execute("SELECT id FROM profile_releases").fetchall()

    migration_url = __import__("os").environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(migration_url) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'employees'"
            ).fetchall()
        }
        policy = connection.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = 'employees'::regclass"
        ).fetchone()
        runtime_roles = connection.execute(
            """
            SELECT rolname, rolbypassrls, rolsuper
              FROM pg_roles
             WHERE rolname IN (
                 'fastagent_app', 'fastagent_ingress',
                 'fastagent_worker', 'fastagent_scheduler'
             )
             ORDER BY rolname
            """
        ).fetchall()
        audit_update = connection.execute(
            "SELECT has_table_privilege('fastagent_app', 'audit_events', 'UPDATE')"
        ).fetchone()[0]
        profile_update = connection.execute(
            "SELECT has_table_privilege('fastagent_worker', 'profile_releases', 'UPDATE')"
        ).fetchone()[0]
        vector_extension = connection.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert "password_hash" in columns
    assert not {"password", "plaintext_password", "password_hint"} & columns
    assert policy is not None
    assert all(not bypass and not superuser for _, bypass, superuser in runtime_roles)
    assert audit_update is False
    assert profile_update is False
    assert vector_extension == ("vector",)


@pytest.mark.integration
@pytest.mark.security
def test_cost_tables_enforce_real_role_tenant_ab_and_app_worker_privilege_split(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    profile_release = UUID(str(seeded_database["profile_release"]))
    conversation_a, conversation_b = uuid4(), uuid4()
    seeded_cost_b = uuid4()
    migration_url = __import__("os").environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(migration_url) as connection:
        connection.execute(
            """INSERT INTO conversation_aggregates(
                   tenant_id, id, profile_release_id, target_stack, state
                 ) VALUES (%s, %s, %s, '[]', '{}'), (%s, %s, %s, '[]', '{}')""",
            (
                tenant_a, conversation_a, profile_release,
                tenant_b, conversation_b, profile_release,
            ),
        )
        connection.execute(
            "INSERT INTO tenant_cost_ceilings(tenant_id, hard_daily_cny) VALUES (%s, 1)",
            (tenant_b,),
        )
        connection.execute(
            """INSERT INTO model_invocation_costs(
                   tenant_id, id, conversation_id, correlation_id, role, provider_id,
                   model_id, release_version, release_content_hash, prompt_release,
                   status, estimated_input_tokens, estimated_output_tokens
                 ) VALUES (%s, %s, %s, %s, 'GENERATE_VALUE', 'test', 'test-model',
                           'test-release', %s, 'test-prompt', 'estimated', 1, 1)""",
            (tenant_b, seeded_cost_b, conversation_b, uuid4(), "a" * 64),
        )
        connection.commit()

    app = Database(settings.database_url)
    with app.transaction(tenant_id=tenant_a) as connection:
        assert connection.execute(
            "SELECT tenant_id FROM tenant_cost_ceilings WHERE tenant_id = %s", (tenant_b,)
        ).fetchall() == []
        assert connection.execute(
            "UPDATE tenant_cost_ceilings SET hard_daily_cny = 2 WHERE tenant_id = %s",
            (tenant_b,),
        ).rowcount == 0
    with pytest.raises(InsufficientPrivilege):
        with app.transaction(tenant_id=tenant_a) as connection:
            connection.execute(
                "INSERT INTO tenant_cost_ceilings(tenant_id, hard_daily_cny) VALUES (%s, 2)",
                (tenant_b,),
            )
    with app.transaction(tenant_id=tenant_a) as connection:
        connection.execute(
            "INSERT INTO tenant_cost_ceilings(tenant_id, hard_daily_cny) VALUES (%s, 2)",
            (tenant_a,),
        )
        assert connection.execute(
            "UPDATE tenant_cost_ceilings SET hard_daily_cny = 3 WHERE tenant_id = %s",
            (tenant_a,),
        ).rowcount == 1
    with pytest.raises(InsufficientPrivilege):
        with app.transaction(tenant_id=tenant_a) as connection:
            connection.execute(
                """INSERT INTO model_invocation_costs(
                       tenant_id, id, conversation_id, correlation_id, role, provider_id,
                       model_id, release_version, release_content_hash, prompt_release,
                       status, estimated_input_tokens, estimated_output_tokens
                     ) VALUES (%s, %s, %s, %s, 'GENERATE_VALUE', 'test', 'test-model',
                               'test-release', %s, 'test-prompt', 'estimated', 1, 1)""",
                (tenant_a, uuid4(), conversation_a, uuid4(), "a" * 64),
            )

    worker = Database(settings.worker_database_url)
    with worker.transaction(tenant_id=tenant_a) as connection:
        assert connection.execute(
            "SELECT tenant_id FROM model_invocation_costs WHERE tenant_id = %s", (tenant_b,)
        ).fetchall() == []
        assert connection.execute(
            "UPDATE model_invocation_costs SET status = 'failed' WHERE tenant_id = %s",
            (tenant_b,),
        ).rowcount == 0
    cost_insert = """INSERT INTO model_invocation_costs(
           tenant_id, id, conversation_id, correlation_id, role, provider_id,
           model_id, release_version, release_content_hash, prompt_release,
           status, estimated_input_tokens, estimated_output_tokens
         ) VALUES (%s, %s, %s, %s, 'GENERATE_VALUE', 'test', 'test-model',
                   'test-release', %s, 'test-prompt', 'estimated', 1, 1)"""
    with pytest.raises(InsufficientPrivilege):
        with worker.transaction(tenant_id=tenant_a) as connection:
            connection.execute(
                cost_insert,
                (tenant_b, uuid4(), conversation_b, uuid4(), "a" * 64),
            )
    with worker.transaction(tenant_id=tenant_a) as connection:
        own_cost = uuid4()
        connection.execute(
            cost_insert, (tenant_a, own_cost, conversation_a, uuid4(), "a" * 64)
        )
        assert connection.execute(
            "UPDATE model_invocation_costs SET status = 'failed' WHERE tenant_id = %s AND id = %s",
            (tenant_a, own_cost),
        ).rowcount == 1
    with pytest.raises(InsufficientPrivilege):
        with worker.transaction(tenant_id=tenant_a) as connection:
            connection.execute(
                "INSERT INTO tenant_cost_ceilings(tenant_id, hard_daily_cny) VALUES (%s, 4)",
                (tenant_a,),
            )


@pytest.mark.integration
@pytest.mark.security
def test_bad_channel_token_creates_no_business_fact(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    with pytest.raises(IngressAuthenticationError):
        ingress.accept(
            config_id="config-a",
            path_token="wrong-token",
            payload=callback(
                external_event_id="bad-1", sender_id="customer-1", text="must not persist"
            ),
        )
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_a) as connection:
        assert (
            connection.execute("SELECT count(*) AS count FROM inbox_events").fetchone()["count"]
            == 0
        )


@pytest.mark.integration
@pytest.mark.security
def test_ingress_can_only_resolve_one_binding_through_the_auth_function(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    with Connection.connect(settings.ingress_database_url) as connection:
        with pytest.raises(InsufficientPrivilege):
            connection.execute("SELECT * FROM channel_bindings").fetchall()
        connection.rollback()
        resolved = connection.execute(
            "SELECT config_id, tenant_id FROM resolve_channel_binding(%s)", ("config-a",)
        ).fetchone()
        missing = connection.execute(
            "SELECT config_id FROM resolve_channel_binding(%s)", ("unknown",)
        ).fetchone()
    assert resolved is not None
    assert resolved[0] == "config-a"
    assert UUID(str(resolved[1])) == UUID(str(seeded_database["tenant_a"]))
    assert missing is None


@pytest.mark.integration
@pytest.mark.security
def test_ingress_replay_key_is_bound_to_config_and_credential(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    # **What this case asserts narrowed on 2026-09-10** (independent review
    # P1-2), and the narrowing is a finding rather than a convenience.
    #
    # It used to read: *one delivery identifier may not be reused under a second
    # message id* -- the identifier being the payload's `nonce`. Real callbacks
    # carry no `nonce`, so the batch that translated them made `requestId` the
    # key, and the vendor's own document shows three pairs of **different
    # messages sharing one `requestId`**. Under that key ordinary traffic raised
    # `IngressAuthenticationError` and took the rest of the request with it:
    # messages lost, and a `200` the vendor never redelivers.
    #
    # The key is the **message id** now, so the sentence this case defends is:
    # *a replay key is bound to the tenant, the config and the credential that
    # carried it, and a redelivery under that key is a duplicate rather than a
    # second event* -- which is what the case is named for. What the change
    # costs is asserted below rather than left unsaid.
    first_payload = callback(
        external_event_id="replay-original",
        sender_id="customer-1",
        text="first delivery",
        request_id="provider-nonce-1",
    )
    first = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=first_payload,
    )
    duplicate = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=first_payload,
    )
    assert first.duplicate is False
    assert duplicate.duplicate is True

    # **The same delivery identifier under a second message id is now a second
    # message**, and that is the cost of the fix, asserted rather than implied.
    # It is not a hole: the two messages have different ids, so they are two
    # rows in `inbox_events` exactly as two genuine messages would be, and the
    # `ON CONFLICT` on `(tenant_id, source, external_event_id)` is what stops a
    # *replay* of either one. What is no longer defended is a claim about
    # `requestId` that the vendor does not honour.
    forged = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id="replay-forged",
            sender_id="customer-1",
            text="first delivery",
            request_id="provider-nonce-1",
        ),
    )
    assert forged.accepted is True
    assert forged.duplicate is False
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        inbox_count = connection.execute("SELECT count(*) AS count FROM inbox_events").fetchone()[
            "count"
        ]
    # Two ids, two rows -- and the redelivery of the first one in between added
    # none, which is the property that actually protects the customer from
    # hearing the same message answered twice.
    assert inbox_count == 2

    # **And the binding this case is named for**, read off the row rather than
    # inferred: the key is stored under the tenant, the config and the
    # fingerprint of the credential that carried it, so the same message id
    # arriving on another binding cannot collide with this one.
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        keys = connection.execute(
            """
            SELECT config_id, credential_fingerprint, external_event_id
              FROM ingress_replay_keys
             WHERE tenant_id = %s ORDER BY external_event_id
            """,
            (tenant_id,),
        ).fetchall()
    assert [row["external_event_id"] for row in keys] == ["replay-forged", "replay-original"]
    assert {row["config_id"] for row in keys} == {"config-a"}
    assert all(row["credential_fingerprint"] for row in keys)


@pytest.mark.security
def test_a_dsn_embedded_in_a_sentence_is_redacted() -> None:
    """T21. The 2026-09-01 leak was a DSN *in a log line*.

    `carries_credentials` only sees whole strings: `urlsplit` puts an embedded
    DSN in `path` with an empty `netloc`, so the value-shape fallback added for
    P3-4 walked straight past the exact shape that actually leaked.
    """

    message = f"connecting to postgresql://app:{DB_PASSWORD}@db.internal:5432/fastagent now"
    redacted = redact(message)
    assert DB_PASSWORD not in redacted
    assert REDACTED in redacted
    # Everything that is not the credential survives -- the line still says which
    # role, host, port and database, which is why it was being logged.
    assert "postgresql://app:" in redacted
    assert "@db.internal:5432/fastagent" in redacted
    assert redacted.startswith("connecting to ") and redacted.endswith(" now")


@pytest.mark.security
def test_an_empty_username_dsn_is_redacted_too() -> None:
    """`redis://:password@host` is how every Redis URL in this repository is written.

    The first draft of the embedded pattern required a non-empty username and
    therefore missed all of them -- caught while checking the fix, not by the
    registration.
    """

    line = f"queue=redis://:{REDIS_PASSWORD}@cache.internal:6379/0 ready"
    redacted = redact(line)
    assert REDIS_PASSWORD not in redacted
    assert "redis://:[REDACTED]@cache.internal:6379/0" in redacted


@pytest.mark.security
def test_two_credentials_in_one_line_are_both_redacted() -> None:
    """`sub` replaces every match; a first hit must not shadow a second."""

    line = (
        f"primary=postgresql://app:{DB_PASSWORD}@one/db "
        f"replica=postgresql://app:{DB_PASSWORD}@two/db"
    )
    redacted = redact(line)
    assert DB_PASSWORD not in redacted
    assert redacted.count(REDACTED) == 2


@pytest.mark.security
def test_ordinary_text_and_credential_free_urls_are_left_alone() -> None:
    """Redaction that mangles safe text stops being trusted and gets removed."""

    for untouched in (
        "see https://example.test/docs/page for details",
        "plain sentence, no url at all",
        "host:8080/path is not a url",
        "mailto:someone@example.test",
    ):
        assert redact(untouched) == untouched


@pytest.mark.security
def test_token_counts_survive_redaction() -> None:
    """T22. A credential is never an integer.

    `is_sensitive_key` matches names, and `token` as a substring blanked
    `input_tokens` and `output_tokens` out of every redacted cost record -- the
    numbers the RMB accounting is computed from. The key-name rule stays (it is
    what catches `access_signing_key`); it now yields to the value's type.
    """

    result = redact(
        {
            "input_tokens": 1234,
            "output_tokens": 56,
            "access_token_ttl_seconds": 900,
            "api_key": "sk-not-a-real-key",  # noqa: S106 - fixture sentinel
            "session_token": "still-a-secret",  # noqa: S106 - fixture sentinel
        }
    )
    assert result["input_tokens"] == 1234
    assert result["output_tokens"] == 56
    assert result["access_token_ttl_seconds"] == 900
    # A credential-shaped *string* under a credential-shaped name is still gone.
    assert result["api_key"] == REDACTED
    assert result["session_token"] == REDACTED


@pytest.mark.security
def test_a_boolean_under_a_credential_name_is_still_redacted() -> None:
    """`True` is not a measurement, and a flag is cheap to lose."""

    assert redact({"token_enabled": True})["token_enabled"] == REDACTED


@pytest.mark.security
def test_amounts_and_identifiers_survive_redaction() -> None:
    """T23. `Decimal` used to come back `[REDACTION_FAILED_DROPPED]`.

    Every amount vanished from a redacted cost record, in a system whose whole
    purpose is to account for money. Failing closed on an unrecognised type is
    right; treating a recognised, harmless one as unrecognised is not.
    """

    identifier = uuid4()
    moment = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)
    result = redact(
        {
            "cost_cny": Decimal("0.0731"),
            "estimated_cost_cny": Decimal("0"),
            "conversation_id": identifier,
            "occurred_at": moment,
        }
    )
    assert result["cost_cny"] == "0.0731"
    assert result["estimated_cost_cny"] == "0"
    assert result["conversation_id"] == str(identifier)
    # ISO-8601, not `str(datetime)`: the first version of this test asserted the
    # space-separated form and so pinned the shape P3-1 had to correct.
    assert result["occurred_at"] == moment.isoformat()
    assert REDACTION_FAILED not in str(result)


@pytest.mark.security
def test_an_unrecognised_type_still_fails_closed() -> None:
    """The widening must not turn into "render whatever you were given"."""


    class Custom:
        def __repr__(self) -> str:  # pragma: no cover - must never be reached
            return "secret-bearing repr"

    result = redact({"thing": Custom()})
    assert result["thing"] == {"type": "Custom", "value": REDACTION_FAILED}
    assert "secret-bearing repr" not in str(result)


@pytest.mark.security
def test_a_url_shaped_key_does_not_weaken_the_shape_fallback() -> None:
    """P1-1. Naming a field `*_url` used to make it *less* safe.

    `_redact_entry` sent url-keyed values straight to `redact_url`, which only
    understands a whole URL -- so a sentence under `database_url_note` kept its
    password while the identical sentence under any other key was caught by the
    shape fallback. That inverts this module's own rule that shape beats name.
    """

    sentence = f"conn postgresql://app:{DB_PASSWORD}@db.internal/x"
    for key in ("database_url_note", "some_uri_detail", "message"):
        result = redact({key: sentence})
        assert DB_PASSWORD not in result[key], key
        assert REDACTED in result[key], key


@pytest.mark.security
def test_redacting_twice_does_not_destroy_what_the_first_pass_kept() -> None:
    """P1-2, and a regression this batch introduced.

    `[REDACTED]` contains brackets, so an already-redacted URL fed back to
    `urlsplit` raises and `redact_url` fails closed -- taking the role, host,
    port and database with it. Redacting twice is a real path: T20 redacts at
    the client boundary and a sink may redact again.
    """

    once = redact(f"connecting to postgresql://app:{DB_PASSWORD}@db.internal:5432/fastagent now")
    twice = redact(once)
    assert twice == once
    assert REDACTION_FAILED not in twice
    assert "@db.internal:5432/fastagent" in twice


@pytest.mark.security
def test_a_password_disguised_as_a_redaction_marker_still_fails_closed() -> None:
    """The idempotence above must not become a way through.

    The check is on the password group, not "does the text contain `[REDACTED]`",
    so a value that merely starts with the marker is not waved past.
    """

    disguised = redact(f"postgresql://app:{REDACTED}{DB_PASSWORD}@db.internal/x")
    assert DB_PASSWORD not in disguised
    assert REDACTION_FAILED in disguised


@pytest.mark.security
def test_the_scheme_is_matched_case_insensitively() -> None:
    """P2-1. URL schemes are case-insensitive (RFC 3986).

    Without this the same credential got two answers depending on whether it had
    words around it: as a whole string `urlsplit` normalises the scheme and it
    was caught; embedded in a sentence it was not.
    """

    for scheme in ("POSTGRESQL", "Postgres", "postgresql"):
        line = f"x {scheme}://app:{DB_PASSWORD}@db.internal/x y"
        assert DB_PASSWORD not in redact(line), scheme


@pytest.mark.security
def test_credentials_in_the_query_string_of_an_embedded_url_are_redacted() -> None:
    """P2-2. A partially redacted line is worse than an unredacted one.

    The match used to stop at the first `/`, so `redact_url` only ever saw the
    authority and query-string credentials survived -- in a line that now looked
    as though it had been processed.
    """

    line = f"conn postgresql://app:{DB_PASSWORD}@db.internal/x?password={REDIS_PASSWORD} done"
    result = redact(line)
    assert DB_PASSWORD not in result
    assert REDIS_PASSWORD not in result
    assert result.startswith("conn ") and result.endswith(" done")


@pytest.mark.security
def test_a_numeric_credential_is_still_redacted() -> None:
    """P2-3, and the leak this batch introduced.

    "A credential is never an integer" was written as an unconditional and it is
    not: PINs, OTPs and all-digit webhook keys are exactly that shape. T13's
    P2-1 accepted the cost of redacting too much; redacting too little is a
    different decision and was never made.
    """

    result = redact({"password": 123456, "api_key": 8675309, "totp_secret": 999999.0})
    assert result["password"] == REDACTED
    assert result["api_key"] == REDACTED
    assert result["totp_secret"] == REDACTED


@pytest.mark.security
def test_measurements_under_credential_shaped_names_still_survive() -> None:
    """The narrowing must not undo T22 itself."""

    result = redact(
        {
            "input_tokens": 1234,
            "output_tokens": 56,
            "access_token_ttl_seconds": 900,
            "token_refresh_attempts": 2,
        }
    )
    assert result["input_tokens"] == 1234
    assert result["output_tokens"] == 56
    assert result["access_token_ttl_seconds"] == 900
    assert result["token_refresh_attempts"] == 2


@pytest.mark.security
def test_amounts_and_timestamps_render_in_a_shape_a_reader_can_parse() -> None:
    """P3-1. T23 exists so amounts stop disappearing.

    Rendering them as `str()` loses them a second way: `str(datetime)` puts a
    space where ISO-8601 wants a `T`, and `str(Decimal("1E+2"))` keeps scientific
    notation. Asserting only "not dropped" would have missed both.
    """

    result = redact(
        {
            "occurred_at": datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
            "cost_cny": Decimal("1E+2"),
            "tiny_cny": Decimal("0.0731"),
        }
    )
    assert result["occurred_at"] == "2026-09-03T12:30:00+00:00"
    assert result["cost_cny"] == "100"
    assert result["tiny_cny"] == "0.0731"


@pytest.mark.security
def test_a_password_containing_a_colon_is_redacted_wherever_it_appears() -> None:
    """P3-2. Whole-string and embedded must not be two definitions.

    `urlsplit` accepts an unescaped colon in the password, so the whole string
    was redacted while the same URL inside a sentence was not -- the same
    credential answered two ways depending on the words around it. The fix for
    P1-1/P1-2/P2-1/P2-2 routes both through `_redact_text`; this pins the last
    shape where the two used to diverge.
    """

    embedded = f"x postgresql://app:{DB_PASSWORD}:extra@db.internal/x y"
    whole = f"postgresql://app:{DB_PASSWORD}:extra@db.internal/x"
    assert DB_PASSWORD not in redact(embedded)
    assert DB_PASSWORD not in redact(whole)
    assert redact(whole) in redact(embedded)
