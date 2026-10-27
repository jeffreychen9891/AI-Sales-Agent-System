-- T205 / B3-4d: keep the provider's `base64RawData`, and say out loud that
-- nothing reads it.
--
-- **ADR-0103 §三**: 「存，并在库内注释与合同里明写它今天零消费方」. The value of
-- keeping it is retrospective -- when the parser turns out to have read a field
-- wrong, this is the only way back to what actually arrived. The value of
-- saying so is different and just as deliberate:
--
-- > 本仓已经有过 `redact()`（零调用方两个月）、`reconcile()`（零生产调用方）、
-- > `frame_generation`（只写不读）、`legal_hold`（恒 false）四个先例，
-- > 而它们共同的代价是**下一个读到那个字段的人以为它在起作用**。
--
-- So this column is written by ingress and read by nobody, and that is the
-- design rather than an omission. `tests/contracts/test_raw_payload_has_no_
-- consumer.py` holds it to that: it fails the moment a query anywhere selects
-- this column.
--
-- ## Why a column here and not a field in the envelope
--
-- **User ruling, 2026-09-09.** `message-envelope` is a frozen contract, and
-- ADR-0103 §三 does not name a revision to it (ADR-0022 / 「图纸优先级」第 2 条:
-- 未点名即不生效). Putting the blob there would also have carried it into two
-- places it has no business being: `GET /conversations/{id}/messages` selects
-- `envelope` (033), and the envelope is what the model sees. A field nobody is
-- meant to read should not ride in the structure everybody reads.
--
-- The column is on `inbox_events` rather than in a table of its own so that it
-- inherits the three things that already work there: RLS
-- (`inbox_events_tenant_isolation`), the purge (`purge_tenant_data` deletes
-- `inbox_events` rows by tenant, `043`), and ingress's existing INSERT
-- privilege. **A new table would have needed all three re-derived**, and the
-- most likely outcome of that is a table the purge forgets -- customer bytes
-- surviving an erasure request is the one failure here that cannot be undone.
--
-- ## Nothing is granted, and that is the point
--
-- `001` grants `SELECT, INSERT ON inbox_events TO fastagent_ingress`, and `033`
-- grants `SELECT` to `fastagent_app`. Those are table-level, so this column
-- rides along. **Privileges therefore do not express "no readers" here** -- the
-- comment below and the contract test do. That is a real difference from
-- `046`'s column, where the boundary is a grant, and it is written down rather
-- than left for a reader to discover.
--
-- ## The escape hatch is pre-authorised, not pre-built
--
-- ADR-0103 §三.1 sized this three ways, and the answer depends on one binary
-- fact -- whether `base64RawData` carries the callback JSON (~1 GB/year at
-- 1000 msg/day) or media bytes (~1 TB/year). The vendor document narrowed it:
-- the field rides on the *envelope* beside `msgData` and is 12 base64
-- characters on a read receipt, so the two expensive tiers are all but excluded
-- (ADR-0103 §五 item 4). **The first real message with media settles it**, and
-- the ruling for the expensive outcome is already made: stop storing the body,
-- keep a hash and an object-storage reference (`cos_storage.py`). No further
-- decision is required -- only a migration.
--
-- **Not pre-built on purpose.** Switching tiers automatically would mean two
-- storage shapes in the record and nobody able to say which one a given row
-- used. Measuring is one query: `SELECT max(length(provider_raw_data)) FROM
-- inbox_events`.
--
-- ## This column is not `envelope.raw_payload_ref`, and that one is always null
--
-- **The frozen contract already has a field with a similar name** (independent
-- review 2026-09-09, P3-1): `message-envelope.schema.json`'s `raw_payload_ref`,
-- which RFC `conversation-engine.md:131` defines as "加密对象存储 pointer +
-- retention class". **Both of its writers set it to `None` and always have**
-- (`adapter.py`, `scheduler.py`). Somebody tracing a mis-parsed message will
-- look at the envelope first, see `raw_payload_ref: null`, and conclude nothing
-- was kept -- the same misreading this batch is written against, running the
-- other way. **The copy is here, in this column.**
--
-- **And the two have to be reconciled before the escape hatch is used, not on
-- the day it is used**: if the measurement lands on media bytes and the ruling
-- switches to "hash + object-storage reference", that reference is exactly what
-- `raw_payload_ref` was defined to carry. Which name holds it -- and whether
-- this column then holds only the hash -- is a decision that belongs to that
-- switch. Recorded in T205's expiry so it cannot arrive as a surprise.
--
-- ## No length cap here, and the real ceiling is in another service
--
-- Every other string `accept()` writes has an explicit bound (`MAX_TEXT`
-- 20000, `MAX_EXTERNAL_EVENT_ID` 255, `MAX_EXTERNAL_USER_ID` 128,
-- `MAX_PAYLOAD_REF` 2048). **This one deliberately has none**, and the reason
-- is stronger than "it is probably small" (independent review 2026-09-09,
-- P3-2): a cap that degraded an over-long value to NULL would make the
-- measurement measure **the cap**, and the measurement is the expiry condition
-- of the open observation above. Capping first would delete the evidence the
-- ruling is waiting for.
--
-- **What does bound a row today is not in this service.** The only ceiling in
-- the repository is `client_max_body_size 2m` in the two Stage nginx example
-- configs (`infrastructure/stage/`); **ingress itself has no limit at all**, and
-- nothing tests that the proxy limit is in place. Before trusting a
-- measurement taken here, check that cap is still deployed -- otherwise the
-- number describes the proxy, not the provider.

ALTER TABLE inbox_events ADD COLUMN IF NOT EXISTS provider_raw_data text;

COMMENT ON COLUMN inbox_events.provider_raw_data IS
'The provider''s base64RawData, kept verbatim. ZERO CONSUMERS TODAY, BY DESIGN
(T205, ADR-0103 三): it is written by ingress and read by no code, no route and
no report -- kept only so that a parsing mistake can be diagnosed against what
actually arrived. Anyone about to read it should first ask what they think it
contains, because that is exactly the question this batch could not answer:
whether it holds the callback JSON or media bytes is still open (observation
list item 4). NULL has THREE meanings, and the third is not about the field at all:
(1) the callback carried no such field; (2) it carried one this database cannot
store (a NUL byte degrades to NULL, like auth_evidence.nonce -- evidence we can
do without, never a refused message); (3) THE CALLBACK NEVER REACHED THIS TABLE
-- a withdrawal (msgType=2063) writes an audit row and returns before the insert
(ADR-0107), so this column is not "every callback we ever received".
NOT TO BE CONFUSED WITH envelope.raw_payload_ref, which is a different field in
the frozen message-envelope contract (an object-storage pointer per RFC 6.2) and
is always null today; the copy lives here, in this column.';

INSERT INTO schema_migrations(version) VALUES ('047_inbound_raw_payload')
ON CONFLICT (version) DO NOTHING;
