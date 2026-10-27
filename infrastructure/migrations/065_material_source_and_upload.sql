-- 065: 资料目录补三列 —— 一个供应商拉得到的地址，以及那次上传的去向。
--
-- ---------------------------------------------------------------------------
-- 用户 2026-09-16 推翻了 09-12 的「E3 第一版不做文件发送」
-- ---------------------------------------------------------------------------
--
-- 那次裁定的依据是 `docs/wecom-proxy-cdn-upload-api.md` §2.2：上传请求体里
-- **只有 `fileUrl`、没有任何字节字段**，所以「供应商自己来拉」会把对象存储与
-- 公网出口拉回范围内。用户 2026-09-16 裁定推翻它，**而推翻的只有那一条**。
--
-- **ADR-0130 的 E1 与 E2 不必推翻，这一点要说清楚**：
--
--   * **E1**（素材由人工放置，没有接收字节的 route）—— 照旧。本批的写入方是
--     一个脚本（与 `import_knowledge.py` 同形），**没有任何 route 接收字节**；
--   * **E2**（不做对象存储与病毒扫描）—— 照旧。我们**不存字节**，
--     只存一个地址；字节在哪、谁托管，是运维选择，不是这张表的事。
--
-- 也就是说：**这张表仍然是目录**，`053` 表头那句
-- 「It does not carry the bytes. That is not an oversight」逐字仍然成立。
-- 变的是它现在还记着「**去哪儿拉**」与「**供应商那边叫什么**」。
--
-- ---------------------------------------------------------------------------
-- 三列各自答一个问题
-- ---------------------------------------------------------------------------
--
-- `source_url`      —— 供应商去哪儿拉这份文件（`cdnUploadByUrlAsync` 的 `fileUrl`）。
-- `upload_request_id` —— 那次上传的 `requestId`。**它是分流的唯一判据**：
--                       上传结果的异步回调走 `cmd=20000`，而它的 `msgData`
--                       （`fileAesKey`/`fileId`/`fileKey`/`fileMd5`/`fileThumbSize`）
--                       **与「媒体密钥通知」那一种形状相同** ——
--                       `callback.py` 今天正是按这几个字段认那一类的。
--                       两者分得开的只有一样：**这个 requestId 是不是我们发起的**。
-- `provider_file_id`  —— 回调带回来的 `fileId`，`sendFile` 的必填之一。
--
-- **为什么 `upload_request_id` 唯一**：一次 `requestId` 只对应一次上传。
-- 不唯一的话，回调回来时「这是哪一行的结果」答不出来，
-- 而那正是这三列存在的全部理由。
--
-- ---------------------------------------------------------------------------
-- 这一条可逆
-- ---------------------------------------------------------------------------
--
-- 三列全部可空、无默认、不改任何既有语义；`materials` 今天**零写入方**
-- （`INSERT INTO materials` 全仓 0 命中，2026-09-16 现核），所以落地时一行都没有。
-- 与 `064` 那条外键不同，**这一条下面没有数据**。

ALTER TABLE materials
    ADD COLUMN source_url text
        CHECK (source_url IS NULL OR length(source_url) BETWEEN 1 AND 2048),
    ADD COLUMN upload_request_id text
        CHECK (upload_request_id IS NULL OR length(upload_request_id) BETWEEN 1 AND 128),
    ADD COLUMN provider_file_id text
        CHECK (provider_file_id IS NULL OR length(provider_file_id) BETWEEN 1 AND 512);

-- **唯一，而且只在有值时唯一。** 部分索引而不是 `UNIQUE` 约束：
-- 绝大多数行在上传之前 `upload_request_id` 是 NULL，
-- 而 NULL 在唯一约束里互不相等 —— 那样写也能过，但索引会白白覆盖全表。
CREATE UNIQUE INDEX IF NOT EXISTS materials_upload_request_idx
    ON materials (tenant_id, upload_request_id)
    WHERE upload_request_id IS NOT NULL;

COMMENT ON COLUMN materials.source_url IS
    '供应商去哪儿拉这份文件（cdnUploadByUrlAsync 的 fileUrl）。'
    '我们不存字节 —— ADR-0130 E2 照旧成立，这里存的只是一个地址。';

COMMENT ON COLUMN materials.upload_request_id IS
    '那次上传的 requestId。**cmd=20000 分流的唯一判据**：上传结果与媒体密钥通知的 '
    'msgData 形状相同（都带 fileAesKey/fileId/fileKey/fileMd5），'
    '分得开的只有「这个 requestId 是不是我们发起的」。';

COMMENT ON COLUMN materials.provider_file_id IS
    '上传回调带回来的 fileId，sendFile 的必填字段之一。NULL = 这份还没有上传成功。';

-- ---------------------------------------------------------------------------
-- 目录行的写入方要 INSERT —— **这是一次权限改动，独立列出**
-- ---------------------------------------------------------------------------
--
-- `058` 给的是 `SELECT, UPDATE`，当时的理由是读路由与那条 archive 都够用，
-- **而没有任何东西插入**（`INSERT INTO materials` 全仓 0 命中，2026-09-16 现核仍是 0）。
-- 本批第一次有了写入方：`scripts/import_materials.py`，与 `import_knowledge.py`
-- 同形 —— 人工整理一份 JSON，有人把它导进来。
--
-- **连的是 `fastagent_app`，不是迁移角色**，理由与 `051` 逐字相同：
-- 迁移角色是超级用户，**整个绕过 RLS**，用它导入等于那条隔离策略一次都不会被走到。
--
-- **只加 INSERT，不动 DELETE。** 目录是版本化的：一份资料改了就是新版本，
-- 旧版本 `status` 置 `archived` 而不是删 —— 那条路 `058` 已经给了 `UPDATE`。

GRANT INSERT ON materials TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('065_material_source_and_upload')
ON CONFLICT (version) DO NOTHING;
