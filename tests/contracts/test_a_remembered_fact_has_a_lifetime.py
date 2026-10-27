"""T299：一条记忆可以有寿命 —— **而在本批之前 `ttl_days` 零消费方。**

## 它关掉的是什么

`ttl_days` **2026-09-12 就写进 schema 了**，而在本批之前**全仓两处出现，
两处都在 schema 文件自己身上**（一处它的 description、一处它的类型声明）。
它自己的 description 逐字写着这件事：

> omitting it means no expiry is declared — **nothing expires a memory today**

**这是本仓判据 2「数据生产了，全仓无人消费」的一个标本** ——
只是这一个更干净：连数据都没生产，是一个**声明了、校验了形状、而无人执行**的键
（判据 1）。T299 登记时写死的到期条件是「实现记忆过期的那一批」，本批是它。

## 两个数是一起定的，而那不是凑巧

**用户 2026-09-20 裁定：默认「永不过期」，上限 365。**
T299 登记时就写着「上界与默认值只能一起定」，理由在
`runtime/memory_lifetime.py` 开头 —— 它们答的是同一个问题的两半。

## 本文件守的五件

1. **两处的 365 是同一个数**（常量 vs schema 的 `maximum`）；
2. **省略 = 永不过期**，而这一条**不是靠「今天没人写 ttl_days」为真的** ——
   它有一条真的声明了别的字段的对照；
3. **引擎两处都过滤**：召回那一处（模型看见什么）与占位那一处
   （新的 `remember` 写不写得进去）。⚠️ **只接前者会造出一个更糟的形状**，
   见 `test_an_expired_memory_does_not_keep_its_slot_forever`；
4. **读 `contact_memories` 的每一处都点名了 `created_at`** —— 按 AST 数，
   因为忘了取那一列的后果是 `is_expired` 一律答「没过期」，**静默失效**；
5. **控制面那条 POST 不给过期的行让位** —— 消费方清单上唯一没接的那个，
   而它是一条裁定不是欠账，理由逐字写在那条用例里。
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime, timedelta

from fastagent_v2.runtime.memory_lifetime import (
    MEMORY_TTL_DAYS_MAX,
    is_expired,
    live_memories,
    ttl_days_by_field,
)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = (
    REPOSITORY_ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"
)
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)
APP = (
    REPOSITORY_ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"
)

#: 读 `contact_memories` 的生产文件。两个 service 各一个。
READERS = (PIPELINE, APP)


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_the_cap_is_one_number_in_two_places() -> None:
    """**365 写在两处，而它是一个数。**

    常量在 `runtime/memory_lifetime.py`（运行期读它的人看这里），
    `maximum` 在 schema（封版时拦下 366 的是它）。
    **两处都必须有**：只有常量，一份声明 400 天的配包能过闸；
    只有 schema，运行期没有任何地方说得出这个上限是多少。

    **而两处一旦不相等，就有一个是谎话** —— 所以这一条判等值。
    """

    ttl = _schema()["properties"]["memory_fields"]["items"]["properties"]["ttl_days"]
    assert ttl.get("maximum") == MEMORY_TTL_DAYS_MAX, (
        # 1) 看到了什么
        f"schema 的 `ttl_days.maximum` 现读 {ttl.get('maximum')!r}，"
        f"而 `MEMORY_TTL_DAYS_MAX` 是 {MEMORY_TTL_DAYS_MAX}。"
        # 2) 为什么这条要红
        "\n这两个数是同一件事的两处写法：schema 那个在封版时拦，"
        "常量那个是运行期唯一说得出上限的地方。**不等就有一个是谎话。**"
        # 3) 到期了该做什么
        "\n改上限要同时改两处；改不动其中一处时，先说清楚为什么它们该不一样 —— "
        "用户 2026-09-20 裁的是一个数（365），不是两个。"
    )
    assert ttl.get("minimum") == 1, "下界不该在本批变动"


def test_omitting_ttl_days_means_never_expires() -> None:
    """**省略 = 永不过期**，用户 2026-09-20 裁定的那一半。

    ⚠️ **这一条带了一个真的声明过 `ttl_days` 的对照**，而那不是凑数：
    没有对照的话，一份**什么都没声明**的 profile 也能让它绿 ——
    那时它证明的是「`ttl_days_by_field` 返回空表」，
    不是「省略的那个字段不过期」。**本仓记着「守卫本身可能是空的」。**
    """

    profile = {
        "memory_fields": [
            {"key": "ring_size"},  # 没声明寿命
            {"key": "anniversary", "ttl_days": 30},  # 对照：声明了
        ]
    }
    lifetimes = ttl_days_by_field(profile)

    assert lifetimes == {"anniversary": 30}, (
        f"只有声明过 `ttl_days` 的字段该进这张表，现读 {dict(lifetimes)}。"
        "\n把没声明的填一个默认天数，会把「商家没说」与「商家说了很久」"
        "变成同一件事 —— 而那两件在产品上不是一回事。"
    )

    long_ago = datetime.now(UTC) - timedelta(days=10_000)
    assert not is_expired(
        field_key="ring_size", created_at=long_ago, now=datetime.now(UTC), lifetimes=lifetimes
    ), "没声明 `ttl_days` 的字段过期了 —— 省略的含义被改掉了"
    # 对照：声明了的那个，同样的年龄，必须过期
    assert is_expired(
        field_key="anniversary",
        created_at=long_ago,
        now=datetime.now(UTC),
        lifetimes=lifetimes,
    ), "声明了 30 天的字段在一万天之后仍然没过期 —— 这条闸是空的"


def test_the_boundary_is_strictly_greater_than() -> None:
    """**边界用例要站在边界上。**

    `ttl_days: 30` 的含义是「30 天之内算数」。
    所以**整整 30 天**的那一刻**不算过期**，30 天零一秒才算。
    本仓记着「夹具不能恰好等于错误答案」—— 一个写 `days=31` 的用例
    对 `>=` 和 `>` 两种实现都绿，它分不出这两者。
    """

    now = datetime.now(UTC)
    lifetimes = {"k": 30}

    exactly = now - timedelta(days=30)
    assert not is_expired(field_key="k", created_at=exactly, now=now, lifetimes=lifetimes), (
        "整整 30 天被判成过期了 —— `ttl_days: 30` 说的是「30 天之内算数」，"
        "判据该是 `>` 不是 `>=`"
    )
    a_second_later = now - timedelta(days=30, seconds=1)
    assert is_expired(
        field_key="k", created_at=a_second_later, now=now, lifetimes=lifetimes
    ), "超过 30 天之后仍然不过期 —— 这条闸没有上边界"


def test_a_row_without_created_at_is_not_silently_dropped() -> None:
    """**取不到 `created_at` 时答「没过期」，而不是「过期了」。**

    那一列是 `NOT NULL DEFAULT now()`，所以 `None` 只可能来自
    **一个没取这一列的查询**。两种错的代价不对称：
    答「过期了」会静悄悄吞掉一位客户的全部记忆；
    答「没过期」至多是漏过滤一次，而下面那条 AST 断言专门防它。
    """

    assert not is_expired(
        field_key="k",
        created_at=None,
        now=datetime.now(UTC),
        lifetimes={"k": 1},
    )


def test_live_memories_keeps_order_and_drops_only_the_expired() -> None:
    """滤掉过期的，**顺序不动** —— 顺序是商家在 `memory_fields` 里写的那个。"""

    now = datetime.now(UTC)
    rows = [
        {"field_key": "a", "created_at": now - timedelta(days=2)},
        {"field_key": "b", "created_at": now - timedelta(days=400)},
        {"field_key": "c", "created_at": now - timedelta(days=2)},
    ]
    live = live_memories(rows, now=now, lifetimes={"b": 30})
    assert [row["field_key"] for row in live] == ["a", "c"], (
        f"现读 {[r['field_key'] for r in live]} —— 该滤掉的只有 `b`，且顺序不动"
    )


def _reader_functions() -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for path in READERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                found[f"{path.name}::{node.name}"] = node
    return found


def _statements_only(node: ast.FunctionDef) -> str:
    """那个函数的**语句**，docstring 剥掉。

    ⚠️ 本仓踩过八次「守卫读到自己的散文」。本文件差一点是第九次：
    下面判「读 `contact_memories` 的地方都点名了 `created_at`」，
    而本批给那几个函数写的 docstring 里**逐字**写着
    「所以这条 SELECT 多取一列 `created_at`」。
    抄自 `test_a_promise_only_a_person_can_keep.py::_statements_only`。
    """

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def test_the_shared_column_list_carries_created_at() -> None:
    """**守卫的守卫。**

    下面那条允许一处用 `_MEMORY_COLUMNS` 来证明它取了 `created_at`。
    **那个许可只有在这个常量真的含着那一列时才成立** ——
    否则下面那条就是一条看起来在守、而实际什么都没要求的断言
    （本仓「守卫本身可能是空的」）。
    """

    source = APP.read_text(encoding="utf-8")
    start = source.index("_MEMORY_COLUMNS = (")
    end = source.index(")", start)
    columns = source[start:end]
    assert "created_at" in columns, (
        "`_MEMORY_COLUMNS` 不再含 `created_at` —— "
        "四条 route 的 `expired` 会因此一律算成 `false`，"
        "而下面那条「每一处都点名了 created_at」会继续绿。"
    )


def test_every_reader_asks_for_created_at() -> None:
    """**每一处从 `contact_memories` 读行的地方，都点名了 `created_at`。**

    忘了取那一列的后果是 `is_expired` 对那条路**一律答「没过期」** ——
    过期这件事在那条路上静默失效，**而没有任何东西会变红**：
    查询照跑、行照读、模型照样拿到一条本该过期的事实。
    **这正是本仓「多传一个不报错」那一族**，只是方向反过来：少取一列不报错。

    ⚠️ **判的是语句，不是文件文本**（见 `_statements_only`）：
    本批给那几处写的 docstring 里就有「多取一列 `created_at`」这句话，
    按文件文本判，这一条会为了错的理由绿。

    ## 两类被放过的，各有理由，而理由都是可查的

    * **走 `_MEMORY_COLUMNS` 的**：控制面四条 route 共用那一串列名，
      而它含着 `created_at`（`test_the_shared_column_list_carries_created_at`
      把这一点单独钉住 —— 否则这条许可是空的）；
    * **`SELECT 1` 那种存在性检查**：它一行数据都不取，
      拿不到 `created_at` 也无从判过期，**而它也不把任何值交给任何人**。
      ⚠️ 它确实因此看不见「这一行过期了」—— 那正是 `_memory_gate` 那一处
      要处理的事，不是这一处。
    """

    missing: list[str] = []
    for name, node in _reader_functions().items():
        body = _statements_only(node)
        if "FROM contact_memories" not in body:
            continue
        # 只数真的取行的那些 SELECT。
        reads_rows = [
            fragment
            for fragment in body.split("SELECT")[1:]
            if "contact_memories" in fragment.split("WHERE")[0]
            and not fragment.lstrip().startswith("1 ")
        ]
        if not reads_rows:
            continue
        if not any(
            "created_at" in fragment or "_MEMORY_COLUMNS" in fragment
            for fragment in reads_rows
        ):
            missing.append(name)

    assert not missing, (
        # 1) 看到了什么
        f"这些地方从 `contact_memories` 读行、而没点名 `created_at`：{missing}。"
        # 2) 为什么这条要红
        "\n`is_expired` 拿 `created_at` 当这条事实的年龄；取不到就一律答"
        "「没过期」—— **过期在那条路上静默失效，而查询照跑、行照读。**"
        # 3) 到期了该做什么
        "\n要么把那一列加进 SELECT，要么说清楚这一处为什么不需要判过期"
        "（例如它只数行数、不把值交给任何人），并在这里排除它。"
    )


def test_both_engine_readers_filter_by_lifetime() -> None:
    """**引擎两处都过滤，而不是只过滤模型看得见的那一处。**

    两处各答一个问题：
    `_remembered_memories` 答「模型这一轮看见哪几条」，
    `_memory_gate` 答「这个字段的槽位占了没」。
    """

    functions = _reader_functions()
    for name in ("pipeline.py::_remembered_memories", "pipeline.py::_memory_gate"):
        assert name in functions, f"{name} 不见了 —— 本文件的坐标要跟着改"
        body = _statements_only(functions[name])
        assert "live_memories" in body, (
            f"`{name}` 不再按寿命过滤了。"
            "\n只过滤其中一处会造出一个更糟的形状：模型看不见那条过期事实，"
            "而新的 `remember` 也写不进去（槽位还占着），"
            "**于是那个字段从此空着，没有任何东西会变红。**"
        )


def test_an_expired_memory_does_not_keep_its_slot_forever() -> None:
    """**过期那一行在写新值时让位，而让位要真的写进库。**

    `contact_memories_one_active_per_field_idx` 是
    `UNIQUE (tenant_id, contact_id, field_key) WHERE status = 'active'` ——
    **它不认得「过期」**。所以只把那一行从 `taken` 里去掉是不够的：
    紧接着那条 `INSERT` 会撞唯一索引，整轮回滚 ——
    **正是 `_memory_gate` 这道闸存在的理由那个形状**。

    ⚠️ **只腾判定已过期的那一行，活着的一行永远不碰**：
    裁定八说模型只能追加、不得改写现有记忆。
    """

    body = _statements_only(_reader_functions()["pipeline.py::_memory_gate"])

    assert "SET status = 'superseded'" in body, (
        "过期那一行不再让位了 —— 那条 `INSERT` 会撞 "
        "`contact_memories_one_active_per_field_idx`，整轮回滚。"
    )
    assert "if field_key in expired:" in body, (
        # 1) 看到了什么
        "让位的条件不再是「这一行过期了」。"
        # 2) 为什么这条要红
        "\n**判据必须是 `expired` 那个集合，不能是「这一轮想写这个字段」** ——"
        "后者会把一条活着的记忆也顺手 superseded 掉，"
        "而那正是裁定八禁止的「模型改写现有记忆」，且 `054` 的触发器"
        "让 superseded 不可逆。"
        # 3) 到期了该做什么
        "\n要改这一处，先回去读 ADR-0131 裁定八与 `054` 那条触发器。"
    )


def test_the_control_plane_says_which_ones_expired() -> None:
    """**控制面把「过期了」显示出来，而不是让它与「还在用」长得一样。**

    读时过滤的代价是：行照旧 `active`，而模型已经不用它了。
    **本仓为这个形状付过一次钱** —— `manual_tasks.expires_at` 全仓零写者
    而读模型照样显示它，于是「这个任务不过期」与「没人填过这一格」
    在屏幕上分不开（`test_manual_task_owner_sla_expiry.py` 把它钉成了事实）。
    """

    body = _statements_only(_reader_functions()["app.py::_memory_projection"])
    assert "'expired':" in body, (
        "控制面的读模型不再给出 `expired` —— 那条 `active` 而模型从不使用的记忆，"
        "在屏幕上又变回与「还在用」长得一样了。"
    )
    assert "is_expired" in body, "`expired` 不再是算出来的了 —— 它是个常量吗？"


def test_the_control_plane_post_still_refuses_an_expired_field() -> None:
    """**控制面那条 POST 不给过期的行让位，而这是有意的、不是漏的。**

    ## 消费方清单上的第四个，它**没有接**，理由写在这里

    「这条记忆还算不算数」今天有四个读取点：

    | 读取点 | 本批怎么处置 |
    |---|---|
    | `_remembered_memories`（模型看见什么）| 过滤 |
    | `_memory_gate`（模型写不写得进去）| 过滤 + 给过期那行让位 |
    | `_memory_projection`（控制面显示）| 多一个算出来的 `expired` |
    | **`record_contact_memory` 那条 `SELECT 1`（员工手工建）** | **不接** |

    ## 为什么引擎那一处必须接，而这一处不必

    **因为这一处有出路，那一处没有。**

    引擎只有一条写记忆的路（`remember`），过期的行占着槽位时
    模型**既看不到旧事实、也写不进新的**，那个字段从此空着 ——
    没有任何补救动作可做。

    控制面有两条：`POST` 建一条新的，`PUT`（`revise_contact_memory`）
    **本来就是「这个字段换一个值」那条路**，它自己就 `superseded` 旧行再写新行。
    所以员工不会卡住 —— 他看到 `expired: true`（本批加的），走 `PUT` 就改掉了。

    ## 而让 `POST` 也自动让位会更糟

    那会让 `POST` **有时是破坏性的**：同一个动作，对一条活着的记忆报 409，
    对一条过期的记忆**悄悄把旧值 superseded 掉**。
    `054` 的触发器让 superseded 不可逆，而一个「看情况决定要不要毁掉旧值」的
    `POST` 正是本仓「多传一个不报错」那一族 ——
    **调用方看不出这次调用属于哪一种。**

    **所以这不是欠账，是一条裁定。** 它由本条用例钉住：
    有人哪天让 `POST` 也让位，这一条会红并指着上面这段理由。
    """

    body = _statements_only(_reader_functions()["app.py::record_contact_memory"])

    assert "MEMORY_FIELD_ALREADY_ACTIVE" in body, (
        "控制面建记忆那条路不再按「这个字段已经有 active 行」拒了 —— "
        "本条用例的前提没了，去读上面那段理由再决定它该怎么改"
    )
    assert "SET status = 'superseded'" not in body, (
        # 1) 看到了什么
        "`record_contact_memory` 开始给旧行让位了。"
        # 2) 为什么这条要红
        "\n**那会让 `POST` 有时是破坏性的**：对活着的记忆报 409，"
        "对过期的记忆悄悄毁掉旧值，而 `054` 的触发器让 superseded 不可逆 —— "
        "调用方看不出自己这次属于哪一种。"
        "\n员工本来就有出路：`PUT`（`revise_contact_memory`）就是"
        "「这个字段换一个值」那条路，而本批已经让 `expired` 在屏幕上看得见。"
        # 3) 到期了该做什么
        "\n真要改，先说清楚 `POST` 的破坏性什么时候发生、调用方怎么预先知道。"
    )
