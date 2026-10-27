#!/usr/bin/env python
"""让那通 4 轮回放走一遍**真实链路** —— ingress → inbox → 四步推理 → outbox → 渠道模拟器。

与 `scripts/demo_trace.py` 是两条路，而这件事必须说清楚（那边的 docstring 同样写了）：
那条路渲染的是场景文件里的**模型侧产出**，零依赖、十秒钟；
这一条让同一份场景走**生产 seam**（照 `scripts/probe_real_provider_pipeline.py` 的成例）：
事件从 `WeComProxyIngress.accept` 进，由真实 `InboxProcessor` 处理
（推理由 `inference_for_provider("scripted")` 装配，即回放替身），
从 `OutboxDispatcher` 出，落到 `channel_sink` 渠道模拟器（本脚本自己起，端口向
OS 要，与 `tests/conftest.py` 的 T82 同一个理由：写下的地址必须是真在听的那个）。
跑完后 inbox / conversation / intent / outbox / delivery / scheduled_events
里躺着的是**真实管线写下的行**，不是渲染出来的样子。

## 第 3 轮怎么复现：走 scheduler，不是再发一条消息

场景的第 3 轮是**定时器轮次**（90 秒静默）。它在生产里的来历：
第 2 轮的出站投递写下 `last_outbound_at`，`rental` 帧的 TimeRule
（`rental-silence_check`，`profiles/synthetic-carrier`）据此排一行
`scheduled_events`；到点后 `Scheduler.enqueue_due` 把它变成一个 `timer`
事件进 inbox。演示不真等 90 秒 —— `enqueue_due(now=...)` 的 `due_at`
比较键**就是调用方时钟**（`scheduler.py` 里写明的接缝："the caller owns it"），
所以这里读出那行的 `due_at`，把 `now` 注到它之后一秒。
**静默判读本身不受这个注入影响**：`read_silence` 的回答录在场景文件里，
回放替身不读 `silent_for_seconds`。

## 它拒绝跑的三种情况

1. **供应商不是 `scripted`** —— 这条脚本只演回放；真供应商在这里意味着
   给一场演示花真钱、调真端点，而且回放的轮次序号机制也对不上真模型；
2. **数据库没有 scratch 标记，或有不是演示自己建的 binding**
   （复用 probe 的 `_refuse_unless_scratch`，含 TRUNCATE 前的两条独立判据）——
   本脚本会 TRUNCATE 十五张表；
3. **当前时刻在配包的 quiet hours 里**（`synthetic-carrier` 是 22:30–08:00
   Asia/Shanghai）—— 那样第 3 轮的主动发送会被主动性闸拒掉。**那是对的行为**，
   但不是这份走查要演的东西：一份「第 3 轮没说话」的输出读起来像系统坏了，
   而真因是一条按设计工作的闸。所以拒绝并说明何时再跑。

用法（先起栈、跑迁移；完整命令序列见 `docs/portfolio/README.md` 的演示一节）：

    python scripts/demo_drive.py

环境：ingress / worker / scheduler 三个角色各自的那套（`Settings.from_env`
按角色收窄必填集，所以**不需要** api 角色的签名密钥）。
`FASTAGENT_CHANNEL_SEND_URL` 不用配 —— 本脚本从真在听的 socket 写它。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from types import MappingProxyType
from typing import Any
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# 与 `pyproject.toml` 的 pytest `pythonpath` 同一张清单（外加 `scripts/` 自己，
# 下面按**顶层名**导入 probe 靠它 —— 不是 `scripts.` 包式导入，因为门禁的 mypy
# 把 `scripts/*.py` 当顶层模块，包式导入会让同一个文件有两个模块名而被拒）。
# pytest 替用例做了这件事，而这条脚本的读者是在终端里的人 ——
# 要求他先 export 六段 PYTHONPATH 再跑演示，演示最可能的死法就是死在那一步。
for _src in (
    "scripts",
    "packages/runtime/src",
    "packages/channels/src",
    "services/api/src",
    "services/worker/src",
    "services/ingress/src",
):
    _path = str(REPOSITORY_ROOT / _src)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastagent_v2.ingress.adapter import WeComProxyIngress  # noqa: E402
from fastagent_v2.ingress.wecom_proxy.callback import conversation_id_for  # noqa: E402
from fastagent_v2.profiles.publisher import publish_release  # noqa: E402
from fastagent_v2.profiles.registry import ProfileRegistry  # noqa: E402
from fastagent_v2.runtime.db import Database  # noqa: E402
from fastagent_v2.runtime.queue import PointerQueue  # noqa: E402
from fastagent_v2.runtime.redaction import redact  # noqa: E402
from fastagent_v2.runtime.settings import Settings  # noqa: E402
from fastagent_v2.worker import proactivity  # noqa: E402
from fastagent_v2.worker.channel import HttpChannelAdapter  # noqa: E402
from fastagent_v2.worker.channel_sink import app as channel_sink_app  # noqa: E402
from fastagent_v2.worker.inference import inference_for_provider  # noqa: E402
from fastagent_v2.worker.inference_scripted import ScriptedInference  # noqa: E402
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher  # noqa: E402
from fastagent_v2.worker.scheduler import Scheduler  # noqa: E402

# 凭据与 scratch 闸**复用 probe 的，不另写一份**：token 的字面量全仓只许有一个
# （「别写像凭据的字面量」那条纪律），而 `_refuse_unless_scratch` 的「有不是
# 自己建的 binding 就拒」判的是 token 哈希 —— 两个脚本用两个 token 的话，
# 先跑 probe 再跑这条会把对方的 binding 当成真租户的而互相拒绝。
from probe_real_provider_pipeline import (  # noqa: E402
    CHANNEL_TOKEN,
    _refuse_unless_scratch,
)
from psycopg import Connection  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

TENANT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CONFIG_ID = "demo-config"
DEMO_SENDER_ID = "demo-customer"
DEMO_ACCOUNT_ID = "demo-hosted-account"

#: 客户那一侧每轮说什么。**这是驱动侧的输入，不是模型侧产出**，所以它住在
#: 驱动脚本里而不是场景文件里 —— 场景文件的 `provenance` 声明的是「模型侧
#: 文档的出处」，往里面塞驱动参数会让那句声明盖到它没盖的东西上。
#: 回放替身不读入站内容（按轮次序号取），这些文本只决定 inbox 里躺着什么。
#:
#: 哪几轮该有文本**不在这里决定**：`plan_turns` 按场景文件实际录的步来判
#: （录了 `read_silence` 的是定时器轮次，没有入站），两边对不上当场拒 ——
#: 场景加了一轮而这里没跟上时，死法是一条点名的报错，不是静默少一轮。
INBOUND_TEXTS: MappingProxyType[int, str] = MappingProxyType(
    {
        1: "你们那个便携投影仪能租吗？",
        2: "租三天吧，下周一还。",
        4: "行，就这么定。",
    }
)

SINK_READY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class TurnAction:
    """驱动一轮要做的事：发一条客户消息，或触发到期的定时器。"""

    ordinal: int
    label: str
    #: `None` ⇔ 定时器轮次（场景里录了 `read_silence` 的那一轮）。
    inbound_text: str | None


def plan_turns(
    scripted: ScriptedInference, inbound_texts: MappingProxyType[int, str] | dict[int, str]
) -> list[TurnAction]:
    """把场景的每一轮配上驱动动作，**两边对不上就拒**。

    判据来自 `ScriptedInference.recorded_steps`（它存在的理由正是让调用方
    不必再读一遍文件猜），不是这里自己解析场景 —— 那是「两处各算一遍」的入口。
    """

    labels = scripted.turn_labels()
    known = {ordinal for ordinal, _ in labels}
    stray = sorted(set(inbound_texts) - known)
    if stray:
        raise ValueError(
            f"INBOUND_TEXTS 给了第 {stray} 轮文本，而场景 {scripted.scenario_id!r} "
            f"只有 {sorted(known)} 这些轮次"
        )
    actions: list[TurnAction] = []
    for ordinal, label in labels:
        timer = "read_silence" in scripted.recorded_steps(ordinal)
        text = inbound_texts.get(ordinal)
        if timer and text is not None:
            raise ValueError(
                f"第 {ordinal} 轮录了 read_silence（定时器轮次，没有入站），"
                f"INBOUND_TEXTS 却给了它一条文本 —— 两边有一边改了而另一边没跟上"
            )
        if not timer and text is None:
            raise ValueError(
                f"第 {ordinal} 轮（{label!r}）是客户轮次，INBOUND_TEXTS 里没有它的文本"
            )
        actions.append(TurnAction(ordinal=ordinal, label=label, inbound_text=text))
    return actions


def refuse_if_quiet_hours(profile_document: dict[str, Any], now: datetime) -> None:
    """quiet hours 预检。见模块 docstring 的第 3 条拒绝。"""

    quiet = proactivity.knobs(profile_document).quiet_hours
    if quiet.covers(now):
        raise SystemExit(
            "refusing: 现在在配包的 quiet hours 里，第 3 轮的主动发送会被主动性闸"
            f"拒掉（那是对的行为，但不是这份走查要演的）。{quiet.lifts_at(now).isoformat()} "
            "之后再跑。"
        )


def _reserve_loopback_socket() -> socket.socket:
    # 端口 0 → OS 挑，然后把**这个 socket 本身**交给 uvicorn —— 先问再按号重绑
    # 有竞态窗口（tests/conftest.py 同一条理由，T82）。
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen()
    return reserved


def start_channel_sink() -> tuple[uvicorn.Server, Thread, str]:
    reserved = _reserve_loopback_socket()
    base_url = "http://{}:{}".format(*reserved.getsockname())
    server = uvicorn.Server(
        uvicorn.Config(channel_sink_app, log_level="warning", access_log=False)
    )
    worker = Thread(target=server.run, kwargs={"sockets": [reserved]}, daemon=True)
    worker.start()
    deadline = time.monotonic() + SINK_READY_TIMEOUT_SECONDS
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            if not worker.is_alive():
                raise SystemExit(f"channel sink exited before serving {base_url}")
            try:
                if client.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.02)
        else:
            raise SystemExit(f"channel sink did not become ready at {base_url}")
    # 先听到了才写地址，与 conftest 同序：什么都不许指向一个还没在听的端口。
    os.environ["FASTAGENT_CHANNEL_SEND_URL"] = f"{base_url}/send"
    return server, worker, base_url


def seed(migration_url: str, package_root: Path, scenario_profile: dict[str, Any]) -> UUID:
    """清空 scratch 库并种上演示要的最小集合：租户、配包 release、渠道绑定。

    形状照 `probe_real_provider_pipeline.seed`，差两样：配包按**场景文件声明的
    身份**取（不是写死一个），release id 从发布结果里读回来（不是断言一个常量）。
    """

    package = ProfileRegistry(root=package_root, relative_to=REPOSITORY_ROOT).package(
        profile_id=str(scenario_profile["profile_id"]),
        version=str(scenario_profile["version"]),
    )
    from fastagent_v2.runtime.auth import sha256_token

    with Connection.connect(migration_url, row_factory=dict_row) as connection:
        _refuse_unless_scratch(connection)
        connection.execute(
            """
            TRUNCATE audit_events, platform_audit_events, delivery_receipts,
                     outbox_messages, policy_decisions,
                     intent_records, scheduled_events, inbox_events, conversation_aggregates,
                     ws_tickets, employee_sessions, channel_bindings, employees,
                     profile_releases, tenants CASCADE
            """
        )
        connection.execute("INSERT INTO tenants(id, code) VALUES (%s, 'demo-tenant')", (TENANT,))
        connection.commit()
        published = publish_release(
            database=Database(migration_url), tenant_id=TENANT, package=package
        )
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES (%s, %s, %s, %s)
            """,
            (CONFIG_ID, TENANT, published.release.release_id, sha256_token(CHANNEL_TOKEN)),
        )
        connection.commit()
    return UUID(str(published.release.release_id))


def accept_customer_message(ingress: WeComProxyIngress, *, ordinal: int, text: str) -> None:
    from uuid import uuid4

    accepted = ingress.accept(
        config_id=CONFIG_ID,
        path_token=CHANNEL_TOKEN,
        # 供应商回调的信封形状，与 probe 同源（v2 自己的信封会被当场拒）。
        payload={
            "code": 0,
            "msg": "ok",
            "data": [
                {
                    "cmd": 15000,
                    "msgType": 2,
                    "msgUniqueIdentifier": f"demo-turn-{ordinal}-{uuid4()}",
                    "requestId": uuid4().hex,
                    "senderId": DEMO_SENDER_ID,
                    "receiverId": DEMO_ACCOUNT_ID,
                    "userId": DEMO_ACCOUNT_ID,
                    "fromRoomId": 0,
                    "isRoomNotice": 0,
                    "timestamp": int(datetime.now(UTC).timestamp()),
                    "msgData": {"content": text},
                }
            ],
        },
    )
    if not accepted.accepted:
        raise SystemExit(f"ingress 拒收了第 {ordinal} 轮：{accepted!r}")


def fire_due_timer(scheduler: Scheduler, migration_url: str) -> None:
    """把**已经排着**的那行 silence_check 按注入时钟触发。

    silence_check 恰好一行是判据不是巧合：第 1 轮出站排的那行被第 2 轮的
    客户消息按 `cancel_on: customer_message` 取消，第 2 轮出站重排 ——
    多于一行说明取消那条路坏了，演示该停下指着它。

    **但它不是唯一一行 PENDING**（第一版这么断言，实跑当场被咬）：
    第 2 轮的回复「这台先给你留着」被承诺台账记成 AI 承诺，
    排了一行 `ai_commitment_due` —— 那是管线在正确工作，不是脏数据。
    所以这里只收窄到 silence_check，并在触发后核对 scheduler 取的确实是它
    （`enqueue_due` 按 `due_at` 取最早的，静默检查的 90 秒远早于承诺期限 ——
    万一不是，下面那条核对会点名报出来，而不是把错的轮次演下去）。
    """

    with Connection.connect(migration_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT id, kind, due_at, status FROM scheduled_events"
            " WHERE status = 'PENDING' AND kind = 'silence_check' ORDER BY due_at"
        ).fetchall()
    if len(rows) != 1:
        raise SystemExit(
            "expected exactly one PENDING silence_check scheduled event, found "
            f"{len(rows)}"
        )
    fired = scheduler.enqueue_due(now=rows[0]["due_at"] + timedelta(seconds=1))
    if len(fired) != 1:
        raise SystemExit(f"scheduler 没把到期的那行放进 inbox：{fired!r}")
    if fired[0].scheduled_event_id != UUID(str(rows[0]["id"])):
        raise SystemExit(
            f"scheduler 取走的是 {fired[0].scheduled_event_id}，不是那行 silence_check "
            f"({rows[0]['id']}) —— 有一行更早到期的事件抢了先，这一轮演的不是静默"
        )


def process_and_dispatch(
    processor: InboxProcessor, dispatcher: OutboxDispatcher, migration_url: str
) -> str:
    """处理一个事件、投一条出站，返回客户看到的文本。"""

    if not processor.process_one(block_ms=5000):
        raise SystemExit("worker 没取到事件 —— 看 inbox_events.status 与 audit_events")
    if not dispatcher.dispatch_one():
        raise SystemExit("这一轮没有出站可投 —— 看 intent_records 与 audit_events 的拒绝理由")
    with Connection.connect(migration_url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT command, status FROM outbox_messages ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise SystemExit("投递说成功了而 outbox 里没有行 —— 不该发生")
    command = row["command"] or {}
    content = command.get("content") if isinstance(command, dict) else None
    text = content.get("text") if isinstance(content, dict) else None
    shown = text if isinstance(text, str) else json.dumps(command, ensure_ascii=False)
    return f"[{row['status']}] {shown}"


def durable_state(migration_url: str) -> dict[str, Any]:
    """跑完后库里躺着什么。查询集照 probe，加 scheduled_events（定时器的一生）。"""

    with Connection.connect(migration_url, row_factory=dict_row) as connection:

        def rows(sql: str) -> list[dict[str, object]]:
            return connection.execute(sql).fetchall()

        return {
            "inbox_events": rows(
                "SELECT event_type, status, attempt, error_code FROM inbox_events"
                " ORDER BY created_at"
            ),
            "conversation_aggregates": rows(
                "SELECT state_version, control_mode, lifecycle, generation,"
                " jsonb_array_length(target_stack) AS frames FROM conversation_aggregates"
            ),
            "intent_records": rows(
                "SELECT kind, status, strategy_id FROM intent_records ORDER BY id"
            ),
            "outbox_messages": rows(
                "SELECT status, command -> 'content' ->> 'text' AS text"
                " FROM outbox_messages ORDER BY created_at"
            ),
            "delivery_receipts": rows(
                "SELECT status FROM delivery_receipts ORDER BY received_at"
            ),
            "scheduled_events": rows(
                "SELECT kind, status, attempt FROM scheduled_events ORDER BY created_at"
            ),
            "audit_events": rows(
                "SELECT action, resource_type, reason_code FROM audit_events ORDER BY created_at"
            ),
        }


def main() -> int:
    # Windows 控制台默认 cp1252，中文 print 会在半截死掉并连累后面的收尾 ——
    # 所以不赌调用方记得 PYTHONIOENCODING。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    scripted = ScriptedInference.from_env(repository_root=REPOSITORY_ROOT)
    scenario = json.loads(Path(scripted.source).read_text(encoding="utf-8"))
    actions = plan_turns(scripted, INBOUND_TEXTS)

    migration_url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]

    server, sink_thread, sink_url = start_channel_sink()
    try:
        # 角色各自收窄的必填集；worker 的 Settings 在 sink 地址写好之后才建。
        ingress_settings = Settings.from_env("ingress")
        worker_settings = Settings.from_env("worker")
        scheduler_settings = Settings.from_env("scheduler")
        if worker_settings.model_provider != "scripted":
            raise SystemExit(
                "refusing: FASTAGENT_MODEL_PROVIDER 不是 scripted。这条脚本只演回放；"
                "真供应商在这里意味着给一场演示花真钱、调真端点。"
            )

        print(f"scenario   : {scripted.scenario_id} ({scripted.source})")
        print(f"provider   : {worker_settings.model_provider} (version {scripted.version})")
        print(f"channel    : sink at {sink_url}")

        release_id = seed(migration_url, REPOSITORY_ROOT / "profiles", scenario["profile"])
        profile_document = (
            ProfileRegistry(root=REPOSITORY_ROOT / "profiles", relative_to=REPOSITORY_ROOT)
            .package(
                profile_id=str(scenario["profile"]["profile_id"]),
                version=str(scenario["profile"]["version"]),
            )
            .document
        )
        refuse_if_quiet_hours(profile_document, datetime.now(UTC))
        conversation_id = conversation_id_for(
            tenant_id=TENANT, config_id=CONFIG_ID, peer_id=DEMO_SENDER_ID
        )
        print(f"seeded     : tenant {TENANT} release {release_id}")
        print(f"conversation {conversation_id}")

        queue = PointerQueue(worker_settings.queue_redis_url)
        ingress = WeComProxyIngress(
            database=Database(ingress_settings.ingress_database_url),
            pointer_queue=queue,
            runtime_deadline_seconds=ingress_settings.runtime_deadline_seconds,
        )
        processor = InboxProcessor(
            database=Database(worker_settings.worker_database_url),
            pointer_queue=queue,
            inference=inference_for_provider(
                worker_settings.model_provider,
                environment=worker_settings.environment,
                governor=None,  # 离线回放：没有账要记（cli.py 的查表条件，同一个理由）
            ),
            worker_id=f"{socket.gethostname()}-demo",
        )
        dispatcher = OutboxDispatcher(
            database=Database(worker_settings.worker_database_url),
            channel=HttpChannelAdapter(worker_settings.channel_send_url),
            dispatcher_id=f"{socket.gethostname()}-demo-sender",
        )
        scheduler = Scheduler(
            database=Database(scheduler_settings.scheduler_database_url),
            pointer_queue=queue,
            scheduler_id=f"{socket.gethostname()}-demo-scheduler",
            runtime_deadline_seconds=scheduler_settings.runtime_deadline_seconds,
        )

        for action in actions:
            print(f"\n―― 第 {action.ordinal} 轮：{action.label}")
            if action.inbound_text is None:
                fire_due_timer(scheduler, migration_url)
                print("   （定时器轮次：scheduler 把到期的 silence_check 放进了 inbox）")
            else:
                accept_customer_message(
                    ingress, ordinal=action.ordinal, text=action.inbound_text
                )
                print(f"   客户：{action.inbound_text}")
            reply = process_and_dispatch(processor, dispatcher, migration_url)
            print(f"   出站：{reply}")

        print("\n---- durable state after the full walkthrough (redacted) ----")
        report = redact(durable_state(migration_url))
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        server.should_exit = True
        sink_thread.join(timeout=SINK_READY_TIMEOUT_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
