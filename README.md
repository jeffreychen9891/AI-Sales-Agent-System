# AI Sales Agent System

一个**多租户 AI 销售助理平台**的工程快照。

企业 IM 里客户的消息进来 → 系统 **理解 / 规划 / 生成** 三步出一条回复 →
经出站队列投递回去。沿途还要做对的事：按租户隔离数据、记每一次模型调用的成本、
把"我明天给您发报价"这种承诺落进台账并在到期时回来、在它不该自己开口的时候
闭嘴、以及**把每一步的依据留痕到可以回放**。

这个仓展示的是**架构设计、可测试性设计与大模型工程化**，不是一套完整的商业业务逻辑。

> ⚠️ **本仓是一份经过筛选与改名的快照，不是原始工作仓。**
> 见下面的 [来历](#来历) 与 [这个快照里没有什么](#这个快照里没有什么)。

---

## 来历

<!-- ⚠️ 这一节留给仓主本人填写：离职与交接的合规声明。
     自动化工具不代写这一段。 -->

_（待仓主补充）_

---

## 30 秒看到它在动 —— 不需要 API Key、数据库，也不需要联网

```sh
python -m venv .venv
.venv/Scripts/activate          # Linux / macOS: source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
pytest tests/contracts -q
```

实测产出（Python 3.12 / CPython，耗时约 27 秒）：

```
2104 passed, 53 skipped
```

> 这个数和下面每一个数都是**跑出来的，不是算出来的**，
> 基线是**含本文件的那一笔提交**（公开仓 `EXPORT-MANIFEST.json` 里记着它的源 commit）。
> 本仓纪律：写"已验证"就要给出一条能复核它的命令 —— 上面那条就是。

**为什么不用 key 就能跑**，这恰好是这个仓里我最愿意讲的一件事：

| | |
|---|---|
| 模型侧 | `FASTAGENT_MODEL_PROVIDER` 默认是一个**确定性替身**，并且有一道闸把它挡在 `local` / `ci` 之外 —— 替身进不了任何会真的对外说话的环境 |
| 网络侧 | 测试期间 `pytest-socket` 拦住**一切非回环连接**，而钉住这道闸的用例**真的去连一次** `203.0.113.1`（TEST-NET-3）确认它拦得住 |
| 数据库 | `tests/contracts` 这一层按设计不碰数据库 —— 它验的是契约、不变量与代码/文档一致性 |

> 那条装依赖的命令**没有在干净环境里验过**：`requirements-dev.lock` 里
> `pytest-socket` 那一条是手写补进去的，只带 wheel 的哈希（本机取 sdist 时上游超时）。
> wheel 是 `py3-none-any`，CPython 3.12 上 `--require-hashes` 满足；
> 别的解释器或要 sdist 的场景下请按文件头那条 `pip-compile` 重新生成。
> **这种"我没验过"的地方本仓一律写出来**，理由见
> [ENGINEERING-NOTES](docs/ENGINEERING-NOTES.md) 最后一节。

---

## 再 10 秒：看它**每一轮在想什么**

测试绿只说明它对，看不到它怎么想。这条命令把一通四轮对话的**决策轨迹**打出来，
**同样不需要 key、数据库或联网**：

```sh
python scripts/demo_trace.py demo/scenarios/carrier-rental-walkthrough.json
# 想要一份能在浏览器里看的：加 --html trace.html
```

每一轮显示四件事：**读到什么**（意图、填上的槽位、还不知道的）·
**决定做什么**（选的策略、理由码、被允许的生成约束）·
**说了什么** · **花了多少**（逐步 token 与延迟）。

最值得看的是**第 3 轮**：客户 90 秒没回音，于是这一轮**多出一步** ——
系统先「读这次静默」（*「他上一条回得很快……大概是在和同事确认取机时间，
而不是不想租了。此刻重复报一遍条款没有用。」*），**再**决定做什么。
这个系统把**「这一轮该不该开口」当成一个可以被判对错的决定**，
而不是一个总是回答的函数。

> ⚠️ 这条路渲染的是**模型侧产出**（`demo/scenarios/*.json` 里逐字就是那些文档，
> 并且按 `packages/contracts/inference/*.schema.json` 校验过），
> **不是一次真实链路的产物** —— 它不经过 ingress、不写 inbox / outbox、不记账。
> 输出的表头会写明自己是哪一条。

---

## 让同一通对话走一遍**真实链路**

上面那条是渲染；这一条是**驱动**：同一份场景文件，事件从 `WeComProxyIngress.accept`
进，真实 `InboxProcessor` 处理，`OutboxDispatcher` 投到渠道模拟器 ——
跑完后 inbox / intent / outbox / `scheduled_events` 里躺着的是**管线写下的行**。
第 3 轮不是「再发一条消息」：第 2 轮的出站按配包的 TimeRule 排了一行
`scheduled_events`，`Scheduler.enqueue_due` 把它变成 `timer` 事件回灌同一个
inbox（演示不真等 90 秒 —— `due_at` 的比较键是调用方时钟，这是 scheduler
声明的接缝，不是演示挖的后门）。

需要 Docker（PostgreSQL + 两个 Redis）。密码**每次现生成**，compose 刻意不给默认值；
数据落 tmpfs，`down` 之后什么都不剩：

```sh
# 1. 一套现生成的密码（三个给容器，四个给 migrate.py 建的运行时角色）
for v in MIGRATOR_DB APP_DB INGRESS_DB WORKER_DB SCHEDULER_DB QUEUE_REDIS CACHE_REDIS; do
  export "FASTAGENT_${v}_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
done

# 2. 各进程角色自己的连接串（端口是 demo.env 里那三个默认值）
export FASTAGENT_MIGRATION_DATABASE_URL="postgresql://fastagent_migrator:${FASTAGENT_MIGRATOR_DB_PASSWORD}@localhost:25432/fastagent"
export FASTAGENT_INGRESS_DATABASE_URL="postgresql://fastagent_ingress:${FASTAGENT_INGRESS_DB_PASSWORD}@localhost:25432/fastagent"
export FASTAGENT_WORKER_DATABASE_URL="postgresql://fastagent_worker:${FASTAGENT_WORKER_DB_PASSWORD}@localhost:25432/fastagent"
export FASTAGENT_SCHEDULER_DATABASE_URL="postgresql://fastagent_scheduler:${FASTAGENT_SCHEDULER_DB_PASSWORD}@localhost:25432/fastagent"
export FASTAGENT_QUEUE_REDIS_URL="redis://:${FASTAGENT_QUEUE_REDIS_PASSWORD}@localhost:26379/0"
export FASTAGENT_CACHE_REDIS_URL="redis://:${FASTAGENT_CACHE_REDIS_PASSWORD}@localhost:26380/0"

# 3. 源码根。`pytest` 从 pyproject.toml 读这几段，而**手跑脚本的人没有人替他读** ——
#    少了它 migrate.py 会 ModuleNotFoundError: No module named 'fastagent_v2'
#    （实测踩过，所以这行写在这里而不是留给你去猜）
#    分隔符问 python 要，不写死：POSIX 是 `:`，Windows 是 `;` ——
#    而这一节的 `.venv/Scripts/` 就是 Windows 的写法。**这一行也是实测踩过的**：
#    写死 `:` 的那一版在 Windows 上照抄跑，第一步 migrate.py 就是上面那个
#    ModuleNotFoundError —— 一条「防止 ModuleNotFoundError」的注释下面
#    躺着一行会产生它的命令。
export PYTHONPATH=$(python -c "import os;print(os.pathsep.join(['packages/runtime/src','packages/channels/src','services/api/src','services/worker/src','services/ingress/src']))")

# 4. 起栈、跑迁移
docker compose -p fastagent-demo -f infrastructure/demo/compose.yml \
    --env-file infrastructure/demo/demo.env up -d --wait
python scripts/migrate.py

# 5. 给演示库打 scratch 标记。驱动脚本会 TRUNCATE，而它的闸要求
#    「库自己声明可毁」—— 标记在库上，不在命令行旗子上（理由见脚本 docstring）
python -c "import os; from psycopg import Connection; \
c = Connection.connect(os.environ['FASTAGENT_MIGRATION_DATABASE_URL']); \
c.execute(\"COMMENT ON DATABASE fastagent IS 'fastagent-v2-scratch'\"); c.commit()"

# 6. 演示开关（demo.env 里一个凭据都没有，只有开关）→ 跑
set -a; . infrastructure/demo/demo.env; set +a
python scripts/demo_drive.py

# 7. 收干净。`-f` 要带上：不带它 compose 只能靠容器标签去猜项目，
#    而这一行的 `${...}` 仍然要从**你这个 shell**里取值
docker compose -p fastagent-demo -f infrastructure/demo/compose.yml \
    down --volumes --remove-orphans
```

每一轮打印客户说了什么、出站投了什么（带投递状态），收尾打一份脱敏后的
耐久状态：4 个 inbox 事件全 `consumed`（3 条客户消息 + 1 个 `timer`）、
4 条意图 `APPLIED`（第 3 轮的意图是 `followup`，不是普通应答 ——
主动发送在数据上就分得出来）、4 条出站 `DELIVERED`，以及那行 silence_check
定时器的一生（被客户消息按 `cancel_on` 作废 → 重排 → `CONSUMED`）。

> ⚠️ 两条边界，写在这里免得被误读：配包声明了 quiet hours
> （22:30–08:00 Asia/Shanghai），在窗口里跑，第 3 轮的主动发送会被
> 主动性闸**按设计拒掉** —— 脚本因此在那个时段拒绝开跑并说明何时再来，
> 而不是产出一份「第 3 轮没说话」让人以为系统坏了。
> **这条拒绝在真实窗口里被撞过一次**（2026-10-28 凌晨照抄上面那串命令）：
> 前六步全部走通（起栈、76 条迁移、scratch 标记都过了），
> 第七步是脚本自己停下来，打出
> `refusing: 现在在配包的 quiet hours 里 …… 2026-10-28T00:00:00+00:00 之后再跑`。
> **一道只在注释里存在的闸和一道会停下来的闸，区别只有撞上去才看得见。**
> 模型侧仍是回放替身（`scripted`，被环境闸挡在 `local`/`ci` 之外）——
> **真实的是链路，不是模型**；这句话反过来说正是上一节那条路的边界。

---

## 它是什么构成的

```
客户消息 ──▶ ingress ──▶ inbox ──▶ worker ──▶ outbox ──▶ 投递
                 │                   │
            路径令牌校验      理解 → 规划 → 生成
            先落库再 ack      成本记账 / 承诺台账 / 人工接管
                                     ▲
                              scheduler（到期事件回灌同一个 inbox）
```

| | |
|---|---|
| **服务** | `services/ingress`（入站 webhook，校验令牌、**先持久化再 ack**）· `services/worker`（三步主链路 + 独立租约的出站投递 + scheduler 角色）· `services/api`（登录、不可变 `AuthContext`、一次性 WebSocket ticket） |
| **包** | `packages/runtime`（配置与闸）· `packages/channels`（渠道适配）· `packages/contracts`（**22 份 JSON Schema**：业务契约 8 · 控制面 7 · 推理 6 · 模型策略 1） |
| **数据库** | PostgreSQL，**76 条迁移**，多租户靠 RLS 隔离；每个进程角色拿**各自的**库凭据，迁移凭据不下发给运行时容器 |
| **队列 / 缓存** | Redis（队列与缓存两个实例分开） |
| **测试** | **3342 条**：四层 `contracts` 2157 · `integration` 948 · `e2e` 133 · `security` 100，另有 4 条在 `tests/` 顶层（`test_cli.py` 与钉 channel sink 夹具的那条）—— **写成加得起来的**，否则「四层」这个框会把差额吃掉 |

依赖锁定到哈希（`requirements*.lock`），门禁是 `ruff` + `mypy --strict`
（strict 覆盖 170 个源文件，0 error —— 这个数是在**公开树里**跑出来的，
不是源仓的；源仓更大）。

> **把门禁串起来的那两个脚本（`scripts/ci.sh` / `ci.ps1`）不在这棵树里。**
> 它们按顺序调五个检查脚本，其中四个（ADR 引用、登记号、静态分类、密钥扫描）
> 各自要读一份**没出去的内部文档**，于是被连带扣下。
> 一个会停在第一个缺失脚本上的门禁脚本，比没有门禁脚本更糟 ——
> 读者会照着它去跑，然后得到一个看起来像自己环境问题的 `No such file`。
> 所以它跟着一起不出去，而不是留一个跑不起来的壳。
> 上面那两条命令（`ruff` / `mypy`）在这棵树里是可以直接跑的，而且**是跑过的**：
> `ruff check` 零发现，`mypy --strict` 170 files / 0 error。
> 这不是顺带一句 —— 在导出工具加上那道闸之前，**这棵树的 `ruff check` 是 17 条红**，
> 16 条是改名的直接后果（`WeComProxy*` → `WeComProxy*` 改掉的是标识符，
> 而 isort 按字母排序，于是本来排好的导入块变成了没排好的）。
> 源仓 ruff 绿而公开树不绿，两棵树之间隔着一张改名表 ——
> 「证据取自另一个环境」这条在这个仓里被抓到过很多次，这是其中一次。

---

## 工程上值得看的

主文是 **[docs/ENGINEERING-NOTES.md](docs/ENGINEERING-NOTES.md)**。三个开胃菜：

1. **同一个判据的两个半边，会永远一致 —— 包括一致地错。**
   改写文件和扫描文件**各自按后缀判断"这是文本吗"**，于是
   `.gitignore` / `Dockerfile` 这类没有后缀的文件**既不被改写、也不被扫描**：
   两边用同一个错判据，所以谁都没报警。这一族在这个仓里被抓到过**十二次**，
   ENGINEERING-NOTES 逐条列出来了 —— 包括**在第一笔公开提交前一刻**才发现的那条：
   三道闸全绿、用例全过，而 `git add -A` 比磁盘少了一个文件，
   少的那个正好是 README 叫读者去跑的那个；
2. **错的方向往往是安静的。** 一个闭包用正则找 `from X import`，而那条正则
   要求模块名后紧跟空白 —— 于是它对**所有** `from a.b import` 结构性失明。
   它不报错，它报"已跑到不动点"，只是那个不动点是错的；
3. **两个测试替身不是一份事实的两份拷贝。** 一个替身的判据是"不改变别人的断言"，
   另一个的判据是"回放一段录好的决策文档"。**它们因为不同的原因而改变**，
   所以是两个实现 —— 把它们并成一个，第一次改动就会让一百多条用例变红。

---

## 这个快照里没有什么

**筛选是由一个会拒绝不干净结果的工具做的**，不是手工删文件 ——
**四道 fail-closed 闸**（禁用词 · 密钥规则 · `git add -A` 会悄悄漏掉的 ·
这棵树自己过不过 `ruff`）**＋ 连带排除 ＋ 一张只计数不拒绝的悬空坐标清单**。
最后那一项刻意只计数：一句 docstring 顺口提到一个没出去的文件是无害的，
而一行**会去执行**那个文件的代码是致命的，**把两类数进同一个数字就没人能据此行动** ——
所以致命那一类改成了连带（代码、shell 脚本、以及这两份文档里的命令块各有一条判据），
剩下的才留在计数里。

为什么必须是"导出一棵干净的树 + 全新历史"而不是就地删：
**git 历史会留着，而一次公开无法撤回。**

扣下的东西，按类：

* **真实客户数据** —— 一份去标识过的真实生产对话语料（它仍然暴露客户所在行业），
  以及读它的那些用例；
* **前雇主的运维信息** —— Stage 部署配置与脚本（主机名、对象存储桶与凭据路径）；
* **内部过程文档** —— `docs/`（含交接件与供应商接口纪要）与 `evidence/`；
* **供应商可识别性** —— 企业 IM 供应商名在全树被改名为 `wecom_proxy`，
  第一个真实租户的行业词被改掉。

代价是**可数的，而不是沉默的**：**1890 个受跟踪文件里导出 666 个**
（加上这两份作品集文档与一份导出清单，公开树共 669 个文件）。
扣下的 1224 个里，**每一个都在私有一侧的审计报告里带着触发它的那个词** ——
一个只给总数的报告答不了"为什么少了这一条用例"。
"一个悄悄少了四百条用例的导出，读起来和『这个项目只有这么多用例』一模一样" ——
所以工具不许静默截断。

**真实链路那条路做到哪一步了**（命令序列在上面「让同一通对话走一遍真实链路」一节）：

| 这一块 | 状态 |
|---|---|
| 回放替身 + 场景文件 | **在**，13 条用例钉着 |
| 决策轨迹渲染 | **在**，12 条用例钉着，零依赖 |
| `infrastructure/demo/compose.yml` · `demo.env` | **在**，且这套 compose **真的起过**：`up -d --wait` 三个健康检查全过 |
| 76 条迁移在这套 compose 上跑通 | **跑过**，`schema_migrations` 数回来是 76 |
| 把四轮真的驱动过一遍 | **跑过**：`scripts/demo_drive.py`，第 3 轮由 `Scheduler.enqueue_due` 按注入时钟触发（不是再发一条消息），4 条出站全 `DELIVERED` |

上一版这张表的后两行是「没做 / 没有」，原因是写它的机器拉不到那两个镜像
（上游连接不稳、没配镜像源）。镜像源配好之后补上了 ——
**但「一条命令跑通」今天仍然不是**：密码要自己生成、scratch 标记要自己打，
上面那一节把每一步都写成了可以照抄的命令，哪一步在替你做什么也写在旁边。

---

## 另外三层怎么跑

`integration` / `e2e` / `security` 需要 PostgreSQL + Redis：

```sh
# 1. 一套现生成的密码。三个给容器（compose 里是 `:?required`，不给就拒绝起），
#    四个给 migrate.py 建的运行时角色
for v in MIGRATOR_DB APP_DB INGRESS_DB WORKER_DB SCHEDULER_DB QUEUE_REDIS CACHE_REDIS; do
  export "FASTAGENT_${v}_PASSWORD=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
done
export FASTAGENT_ACCESS_SIGNING_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(36))')
export FASTAGENT_PURGE_FINGERPRINT_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(36))')

# 2. 各进程角色自己的连接串（端口是 local/compose.yml 里那三个默认值）
export FASTAGENT_ENVIRONMENT=ci
export FASTAGENT_MODEL_PROVIDER=mock
export FASTAGENT_ALLOWED_WS_ORIGINS=http://localhost:3000
export FASTAGENT_MIGRATION_DATABASE_URL="postgresql://fastagent_migrator:${FASTAGENT_MIGRATOR_DB_PASSWORD}@localhost:55432/fastagent"
export FASTAGENT_DATABASE_URL="postgresql://fastagent_app:${FASTAGENT_APP_DB_PASSWORD}@localhost:55432/fastagent"
export FASTAGENT_INGRESS_DATABASE_URL="postgresql://fastagent_ingress:${FASTAGENT_INGRESS_DB_PASSWORD}@localhost:55432/fastagent"
export FASTAGENT_WORKER_DATABASE_URL="postgresql://fastagent_worker:${FASTAGENT_WORKER_DB_PASSWORD}@localhost:55432/fastagent"
export FASTAGENT_SCHEDULER_DATABASE_URL="postgresql://fastagent_scheduler:${FASTAGENT_SCHEDULER_DB_PASSWORD}@localhost:55432/fastagent"
export FASTAGENT_QUEUE_REDIS_URL="redis://:${FASTAGENT_QUEUE_REDIS_PASSWORD}@localhost:56379/0"
export FASTAGENT_CACHE_REDIS_URL="redis://:${FASTAGENT_CACHE_REDIS_PASSWORD}@localhost:56380/0"

# 3. 源码根。与上一节同一个理由：pytest 自己从 pyproject.toml 读它，
#    而 migrate.py 是手跑的，没人替它读。分隔符同样问 python 要
export PYTHONPATH=$(python -c "import os;print(os.pathsep.join(['packages/runtime/src','packages/channels/src','services/api/src','services/worker/src','services/ingress/src']))")

# 4. 起栈、跑迁移、跑这三层
docker compose -f infrastructure/local/compose.yml up -d --wait
python scripts/migrate.py
pytest tests/integration tests/e2e tests/security -q

# 5. 收干净（数据落 tmpfs，down 之后什么都不剩）
docker compose -f infrastructure/local/compose.yml down --volumes --remove-orphans
```

> ⚠️ **上一版这里的第一行是 `cp .env.example .env`，而 `.env.example`
> 不在这棵树里**（它的键名面向真实部署，导出时按名扣下）。
> 一条指着不存在文件的命令，比没有这一节更糟 ——
> 读者照着跑，第一行就 `cp: cannot stat`，而那读起来像他自己的环境问题。
> 所以这一节改成**把每一个变量写出来**，不依赖一个没出去的文件。
> 同族的第三次了：前两次是导出树里的用例去读没出去的文件，
> 这一次是**文档**里的命令，而文档不在任何连带判据的范围里。

> **这三层是真的跑过的，而且是把上面那个代码块逐字抄下来跑的** ——
> 不是跑一个"等价的"脚本。实测产出（7 分 06 秒）：
>
> ```
> 1180 passed, 1 skipped
> ```
>
> 唯一那条 skip 是 `test_wecom_proxy_guard.py` 里一条只在 Linux 上成立的
> POSIX 权限断言。`tests/contracts` 那 2157 条不需要栈，上面第一节那条命令就是它。
>
> ⚠️ **"逐字抄下来跑"这个口径不是炫耀，是上一版栽在它上面。**
> 上一版这一节与上面那节的 `PYTHONPATH` 都写死了 `:` 作分隔符 ——
> 而 POSIX 是 `:`，**Windows 是 `;`**，这一节的 `.venv/Scripts/` 就是 Windows 的写法。
> 当时的注释写着「少了它 `migrate.py` 会 `ModuleNotFoundError`，实测踩过」，
> **而那行命令自己在 Windows 上就产生那个错**。写它的人跑的是一个
> 同一批 env 的装配脚本，不是这个代码块。

---

## 目录

| 路径 | 是什么 |
|---|---|
| `packages/contracts/` | 22 份 JSON Schema —— 契约先行的那个"定义点" |
| `packages/runtime/` | 配置、环境闸、不变量 |
| `services/` | `api` / `ingress` / `worker` 三个进程角色 |
| `infrastructure/migrations/` | 76 条迁移（含 RLS 策略） |
| `tests/contracts/` | 最大的一层，也是不用任何外部依赖就能跑的一层 |
| `demo/scenarios/` | 回放替身的输入：一通 4 轮对话（含"这一轮应该不说话"那一步） |
| `scripts/demo_trace.py` | 决策轨迹渲染 —— 零依赖，上面那条 10 秒的路 |
| `scripts/demo_drive.py` | 让同一通对话走真实链路 —— ingress → inbox → outbox，第 3 轮走 scheduler |
| `infrastructure/demo/` | 演示那套依赖的 compose 与开关 |
| `docs/ENGINEERING-NOTES.md` | **这个仓真正想让人看的东西** |
