-- 066: 上传回来的那对密钥有了落点 —— **而落点一有，读路由就必须拿不到它**。
--
-- ---------------------------------------------------------------------------
-- 用户 2026-09-16 裁定：存在 `materials` 上，按列级授权
-- ---------------------------------------------------------------------------
--
-- `/msg/sendFile` 的必填里有 `fileAesKey` 与 `fileMd5`（v1 生产实现逐字如此），
-- 所以发一次文件就得在发送那一刻拿得到它们。而本仓对这一族有明文立场 ——
-- `ingress/wecom_proxy/inbound.py` 写着：
--
--   > `fileAuthkey`, `fileAeskey`, **`fileAesKey`** -- credentials for fetching
--   > or decrypting the media, not references to it.
--
-- 也就是说这一笔是**把凭据落库**。用户裁定按本仓既有的凭据处置来做：
-- 存在目录行上，**而授权按列发**（`007` / `023` / `029` / `046` 那一族的做法）。
--
-- ---------------------------------------------------------------------------
-- ⚠️ 这一条真正在修的，是加列会**自动**打开的那个口子
-- ---------------------------------------------------------------------------
--
-- `058` 给 `fastagent_app` 的是**表级** `GRANT SELECT ON materials`。
-- 表级授权**覆盖以后新增的每一列** —— 所以只要 `ADD COLUMN` 一落地，
-- 那两列凭据**立刻对读路由可见**，而没有任何东西会红：
-- `GET /materials` 与 `GET /materials/{id}` 今天各自只 `SELECT` 十列，
-- 它们不会因为多了两列就开始返回它们 —— **直到有人加一个字段**。
--
-- `017` 的表头把这条规则的另一半写过了（`channel_bindings` 是列级）：
--
--   > `007` defined the authorisation on this table as **column-level** ...
--   > so a new column is granted to nobody by construction.
--
-- **本表不是那样**，所以这里要把它改成那样：先收回表级，再按列发。
-- 之后再加列，默认谁都读不到 —— 这正是 `017` 说的 by construction。
--
-- ---------------------------------------------------------------------------
-- 这一批**只发有调用方的授权**
-- ---------------------------------------------------------------------------
--
-- 回填那两列的是入站侧（上传结果的异步回调），发送时读它们的是 worker ——
-- **两者今天都还没有代码**。这张表自己立过这条规矩 —— `053` 建表时一条
-- `GRANT` 都没发，理由逐字写在那儿：
--
--   > **No GRANT.** ... nothing reads or writes this table yet. The routes
--   > arrive in E3-6, the send path in E3-4, and each grants what it needs.
--   > A grant issued now would be a permission with no caller.
--
-- 所以本批**不给 worker、也不给 ingress 发任何授权**：那两条随它们各自的实现
-- 一起发，与 `065` 把第一条 `INSERT` 发给导入脚本同源。
--
-- ---------------------------------------------------------------------------
-- 可逆
-- ---------------------------------------------------------------------------
--
-- 三列可空无默认；授权是从表级换成列级，**范围只缩不放**。
-- `materials` 今天零行（写入方 `065` 才有，而它还没被跑过）。

ALTER TABLE materials
    ADD COLUMN provider_file_aes_key text
        CHECK (provider_file_aes_key IS NULL
               OR length(provider_file_aes_key) BETWEEN 1 AND 512),
    ADD COLUMN provider_file_md5 text
        CHECK (provider_file_md5 IS NULL OR length(provider_file_md5) BETWEEN 1 AND 128),
    ADD COLUMN provider_file_size bigint
        CHECK (provider_file_size IS NULL OR provider_file_size > 0);

COMMENT ON COLUMN materials.provider_file_aes_key IS
    '**凭据**：上传回调带回的 fileAesKey，sendFile 的必填之一。'
    '按列授权，读路由拿不到 —— 与 inbound.py 对这一族的判断一致'
    '（credentials for decrypting the media, not references to it）。';

COMMENT ON COLUMN materials.provider_file_md5 IS
    '上传回调带回的 fileMd5，sendFile 的必填之一。与 aes key 同批授权。';

COMMENT ON COLUMN materials.provider_file_size IS
    '上传回调带回的 fileSize。**与 byte_size 分开存**：byte_size 是我们从 source_url '
    '取一次算出来的，这一列是供应商那边的读数 —— 两个数描述同一份文件的两侧，'
    '不一致本身就是要看见的信息，合成一列就看不见了。';

-- ---------------------------------------------------------------------------
-- 授权：表级换列级，范围只缩不放
-- ---------------------------------------------------------------------------
--
-- 列出来的十列**就是两条读路由 `SELECT` 的那十列**（`api/app.py` 的
-- `GET /materials` 与 `GET /materials/{id}`，逐字对过），外加 `065` 的
-- `source_url` —— 那一列不是凭据，而运维要答得出「这份资料从哪儿拉」。
--
-- **`upload_request_id` 与 `provider_file_id` 也给**：它们是句柄不是密钥，
-- 而排障时「这行有没有上传过、供应商那边叫什么」是第一个要问的。
--
-- **不给的只有两列**：`provider_file_aes_key` 与 `provider_file_md5`。
REVOKE SELECT ON materials FROM fastagent_app;

GRANT SELECT (
    tenant_id, id, slug, version, filename, media_type, byte_size,
    content_hash, status, archived_at, created_at,
    source_url, upload_request_id, provider_file_id, provider_file_size
) ON materials TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('066_material_provider_credentials')
ON CONFLICT (version) DO NOTHING;
