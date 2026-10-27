from __future__ import annotations

import argparse
import logging
import socket
import time
from datetime import UTC, datetime

from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import (
    OFFLINE_MODEL_PROVIDERS,
    Settings,
    SettingsError,
)
from fastagent_v2.worker.channel import ChannelAdapter, ChannelFactory, HttpChannelAdapter
from fastagent_v2.worker.digest import ManagerDigest
from fastagent_v2.worker.inference import inference_for_provider
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.scheduler import Scheduler
from fastagent_v2.worker.tenant_purge import TenantPurgeRunner
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter

LOGGER = logging.getLogger(__name__)


def channel_factory_for(settings: Settings) -> ChannelFactory | None:
    """How this worker sends, or `None` to keep using the single sink adapter.

    **This is the one line ADR-0094 §四 reserved for `cli.py`**, and it is here
    rather than in `pipeline.py` on purpose: the dispatcher resolves an address
    and asks for an adapter, and never learns which provider it got. Which
    client to build, from which credentials, with which allowlist, is a wiring
    decision -- so it is made where wiring is made.

    **Two refusals are built into the shape rather than checked for:**

    * **no confirmer is passed.** `RestrictedWeComProxyClient` falls back to
      `_deny_unconfirmed`, so every `CONFIRMED_OPERATION` -- `create_client`,
      `set_notify_url` -- raises if anything ever calls it from in here. A
      background loop must not be able to create an instance or repoint a
      callback address: there is nobody at a terminal to say yes. `send_text` is
      deliberately not in that set, which is why this works at all;
    * **only `send_text` is handed to the adapter.** The adapter receives a
      bound method, not the client, so the rest of the surface is not reachable
      from the dispatch path even by mistake.
    """

    if settings.channel_provider != "wecom_proxy":
        return None

    # **This import buys nothing at import time, and the first version of this
    # comment claimed otherwise** (independent review 2026-09-09, P2-1). It said
    # a sink-channel worker "should not need the provider package present to
    # start" -- but `cli.py` already imports `wecom_proxy_channel` at module
    # scope, and that module imports the provider error taxonomy at module scope
    # too (B2-5's whole point). By the time this function runs, the entire
    # provider package including the client is in `sys.modules`.
    #
    # **What is actually true, and is the reason to keep it here**: in the sink
    # configuration nothing *constructs* a `RestrictedWeComProxyClient`, nothing
    # reads the credentials file, and nothing reads the allowlist. Those are
    # side effects worth not having; the import is not.
    from fastagent_v2.channels.wecom_proxy import (
        DEFAULT_CREDENTIALS_PATH,
        GuidAllowlist,
        RestrictedWeComProxyClient,
        load_credentials,
    )

    # **Refused at startup, not at the first send.**
    #
    # The first draft passed `GuidAllowlist("")` and called it default-deny.
    # It is not: an empty path is `Path(".")`, which *exists* and is a
    # directory, so the allowlist raises `WeComProxyConfigError: guid allowlist
    # is unreadable` -- **on the first message, inside the dispatch loop**,
    # rather than on the operator's screen. A test written for the claim caught
    # the claim being false.
    #
    # `GuidAllowlist`'s own default-deny still does its job when the file is
    # *configured and absent*, which is the case it was designed for. What it
    # cannot cover is nobody having said where the file is, and that is a
    # deployment mistake worth refusing to start over.
    if not settings.channel_guid_allowlist_file:
        raise SettingsError(
            "FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE is required when "
            "FASTAGENT_CHANNEL_PROVIDER is wecom_proxy"
        )

    client = RestrictedWeComProxyClient(
        credentials=load_credentials(settings.channel_credentials_file or DEFAULT_CREDENTIALS_PATH),
        allowlist=GuidAllowlist(settings.channel_guid_allowlist_file),
    )

    def build(*, guid: str, recipient_id: str) -> ChannelAdapter:
        # **文件那条路只在这里接上。** 适配器的 `send_file` 默认 `None`，
        # 于是用例里的双件不会因为多了一个必填参数而全部改签名，
        # 而**生产这一个接上了**——没接上时一行文件消息得到的是
        # `file_sending_not_wired` 那条失败回执，不是一次崩溃。
        return WeComProxyChannelAdapter(
            send_text=client.send_text,
            send_file=client.send_file,
            guid=guid,
            recipient_id=recipient_id,
        )

    return build


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FastAgent v2 M0 process roles")
    subparsers = parser.add_subparsers(dest="role", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    scheduler = subparsers.add_parser("scheduler")
    scheduler.add_argument("--once", action="store_true")
    scheduler.add_argument("--interval", type=float, default=1.0)
    return parser


def run_worker(settings: Settings, *, once: bool) -> None:
    queue = PointerQueue(settings.queue_redis_url)
    database = Database(settings.worker_database_url)
    governor = None
    if settings.model_provider not in OFFLINE_MODEL_PROVIDERS:
        # The ledger and the ceilings only exist for real calls. An offline
        # provider costs nothing, and recording a fabricated cost would pollute
        # the totals the RMB re-estimate is supposed to be computed from.
        #
        # ⚠️ **查表，不要写死某一支的名字。** 这里原本是 `!= "mock"`，
        # 而 `scripted` 进表之后这一行**没跟着改** —— 于是离线回放也会装配
        # 成本账。表在 `runtime/settings.py` 顶上，是同一张。
        from fastagent_v2.worker.cost import CostGovernor
        from fastagent_v2.worker.model_policy import active_release

        governor = CostGovernor(database=database, release=active_release())
    retriever = None
    if settings.model_provider not in OFFLINE_MODEL_PROVIDERS:
        # **与 governor 同一个条件，理由也同源**：离线供应商下没有嵌入端点可调，
        # 也没有账要记。检索器因此只在真实供应商下装配 ——
        # `InboxProcessor` 收到 `None` 就整步跳过（裁定三：没请求就不检，
        # 而「没有检索器」在数据上与「没请求」同形：两者都不留证据行）。
        #
        # **这一段是生产里唯一把检索接上的地方**，而 `InboxProcessor` 的那个参数
        # 是可选的 —— 所以「有人把这段删了」不会有任何东西报错。
        # 拦这一手的是 `tests/contracts/test_retrieval_wiring.py`。
        import httpx
        from fastagent_v2.worker.embedding import EmbeddingBinding, EmbeddingClient
        from fastagent_v2.worker.model_policy import active_release as _active_release
        from fastagent_v2.worker.providers import HttpxChatTransport
        from fastagent_v2.worker.retrieval import KnowledgeRetriever

        release = _active_release()
        retriever = KnowledgeRetriever(
            database=database,
            embedder=EmbeddingClient(
                transport=HttpxChatTransport(httpx.Client()),
                binding=EmbeddingBinding.from_release(release.document),
            ),
            release=release,
            governor=governor,
        )
    processor = InboxProcessor(
        database=database,
        pointer_queue=queue,
        inference=inference_for_provider(
            settings.model_provider,
            environment=settings.environment,
            governor=governor,
        ),
        worker_id=f"{socket.gethostname()}-worker",
        retriever=retriever,
    )
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(settings.channel_send_url),
        dispatcher_id=f"{socket.gethostname()}-sender",
        # `None` unless the deployment says otherwise, so every environment that
        # has not opted in keeps sending exactly where it sent yesterday.
        channel_factory=channel_factory_for(settings),
    )
    # **B1 #12's executor.** `C13` (ADR-0083) asked for exactly this: an
    # asynchronous executor that is really running, rather than a route
    # promising a worker that does not exist. It lives in this loop for the
    # reason the digest below gives -- a second deployment artifact is a second
    # thing that can be *not installed*.
    #
    # **`None` when the key is absent, and that is a refusal not a default.**
    # The tombstone is an HMAC over the tenant code (T154); computing it with a
    # made-up key would write fingerprints that can never be compared against
    # anything, and the code is destroyed in the same statement, so there is no
    # second chance to get it right.
    purge_runner = (
        TenantPurgeRunner(
            database=Database(settings.worker_database_url),
            fingerprint_key=settings.purge_fingerprint_key,
        )
        if settings.purge_fingerprint_key
        else None
    )
    if purge_runner is None:
        # **Say it once, rather than being silently absent** (independent review
        # P1-2). Without the key this loop simply never purges anything, and a
        # queued job sits at `PENDING` while the route has already answered
        # `202 accepted`. Preflight now requires the key on Stage, so this line
        # is for every other deployment.
        LOGGER.warning(
            "tenant purge runner is disabled: FASTAGENT_PURGE_FINGERPRINT_KEY is not set"
        )
    # The hourly digest runs here rather than in its own cron for one reason:
    # this loop already enumerates active tenants, and a second deployment
    # artifact is a second thing that can be *not installed* -- the failure this
    # repository has paid for more than once. The interval is enforced against
    # the last digest actually written (see `digest.py`), so calling it on every
    # pass cannot produce a second message.
    #
    # **It is not free, and the previous version of this comment said it was.**
    # It claimed "one indexed query"; the interval probe is a `max(created_at)`
    # over `outbox_messages` filtered on two JSON keys, which no index covered
    # (independent review 2026-09-04, P2-2). Migration `016` adds a partial
    # index matching that predicate exactly, so the claim is now true -- but the
    # cost is still one query and one round trip per tenant per pass, and the
    # honest description is "cheap", not "free".
    digest = ManagerDigest(database=Database(settings.worker_database_url))
    while True:
        try:
            processor.process_one(block_ms=250)
        except Exception as error:  # noqa: BLE001 - isolate one poisoned fact
            # Do not include exception text or traceback: either can contain
            # provider/customer material. PostgreSQL retains the fact and lease.
            LOGGER.error(
                "worker isolated one failed inbox event; error_type=%s",
                type(error).__name__,
            )
        if purge_runner is not None:
            try:
                purge_runner.run_one()
            except Exception as error:  # noqa: BLE001 - isolate one failed purge
                # Its own guard, for the reason the two digest guards below
                # give: a purge that fails deterministically must not stop the
                # inbox and the outbox from being served on every later pass.
                LOGGER.error(
                    "worker isolated one failed tenant purge; error_type=%s",
                    type(error).__name__,
                )
        try:
            dispatcher.dispatch_one()
        except Exception as error:  # noqa: BLE001 - isolate one poisoned delivery
            # The adapter answers channel failures with a receipt, so reaching
            # here means the failure is ours (database, contract, adapter bug).
            # The outbox lease expires and the row is reclaimed with its attempt
            # counter advanced; it cannot retry without bound.
            LOGGER.error(
                "worker isolated one failed outbox dispatch; error_type=%s",
                type(error).__name__,
            )
        # **Two guards, because there are two failures.** Enumerating the
        # tenants is one database call and can fail on its own; digesting one
        # tenant is another. The first version had a single `try` around both,
        # which meant a tenant whose digest failed deterministically aborted the
        # pass at the same point every time and **every tenant after it in the
        # ordering was never digested at all** (independent review 2026-09-04,
        # P2-3). Moving the `try` inside the loop fixes that and would leave the
        # enumeration itself unguarded -- so it keeps its own.
        try:
            tenant_ids = list(dispatcher.active_tenant_ids())
        except Exception as error:  # noqa: BLE001 - isolate the enumeration
            LOGGER.error(
                "worker could not list active tenants for the digest; error_type=%s",
                type(error).__name__,
            )
            tenant_ids = []
        for tenant_id in tenant_ids:
            # **The `try` is inside the loop.** It used to wrap the whole
            # enumeration, so a tenant whose digest failed deterministically --
            # a malformed contact row, a task whose proposal cannot be read --
            # would abort the pass at the same point every time and **every
            # tenant after it in the ordering would never be digested at all**
            # (independent review 2026-09-04, P2-3). One poisoned tenant is
            # supposed to cost one tenant, which is the same reasoning the two
            # loops above already follow.
            try:
                digest.run_for(tenant_id=tenant_id, now=datetime.now(UTC))
            except Exception as error:  # noqa: BLE001 - isolate one tenant
                # The tenant id is not customer material and is what makes this
                # line actionable: without it the operator knows a digest failed
                # and not whose.
                LOGGER.error(
                    "worker isolated one failed manager digest; tenant_id=%s error_type=%s",
                    tenant_id,
                    type(error).__name__,
                )
        if once:
            return


def run_scheduler(settings: Settings, *, once: bool, interval: float) -> None:
    queue = PointerQueue(settings.queue_redis_url)
    scheduler = Scheduler(
        database=Database(settings.scheduler_database_url),
        pointer_queue=queue,
        scheduler_id=f"{socket.gethostname()}-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    scheduler.republish_pending()
    last_repair = time.monotonic()
    while True:
        scheduler.enqueue_due()
        if once:
            return
        if time.monotonic() - last_repair >= 10:
            scheduler.republish_pending()
            last_repair = time.monotonic()
        time.sleep(max(0.1, interval))


def main() -> None:
    arguments = build_parser().parse_args()
    settings = Settings.from_env(arguments.role)
    if arguments.role == "worker":
        run_worker(settings, once=arguments.once)
    elif arguments.role == "scheduler":
        run_scheduler(settings, once=arguments.once, interval=arguments.interval)


if __name__ == "__main__":
    main()
