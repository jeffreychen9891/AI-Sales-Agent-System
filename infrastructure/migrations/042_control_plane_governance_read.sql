-- 042: the control plane's first read of the governance tables (B3 #2 second
-- delivery, T172, user ruling 2026-09-06 「发窄 SELECT」).
--
-- ADR-0076 split `GET /conversations/{id}` in two because two of §6.2's
-- takeover-period items live in tables `fastagent_app` could not read:
-- refused slot updates in `audit_events` (written by `pipeline.py`, action
-- `conversation.slot_update_refused`) and the cost gate halting the
-- understanding step in `policy_decisions` (`reason_code =
-- UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL`). ADR-0076 §三 left the choice
-- between this and having the engine maintain the two counters on the
-- aggregate; the user ruled for the grant. ADR-0081 records both.
--
-- **Column-level, and the columns are the ones the route reads.** A table
-- `GRANT SELECT` would have been one line shorter and would also have handed
-- the API role every policy decision's `context` -- limits, projected spend,
-- model ids -- to answer one boolean. `032` refused the same shape with the
-- same words: what is not in the list is a decision too.
--
-- **What this does not narrow, stated rather than glossed** (ADR-0081 §三):
-- the grant is narrow in columns, not in rows. `fastagent_app` can now read
-- `audit_events.evidence` for **any** action, inside its own tenant -- RLS
-- (`001:299`, ENABLE + FORCE, `app.tenant_id`) is what keeps it there, and
-- the `action =` filter in the route is a query, not a privilege. Narrowing
-- by row needs a view or a projection table, which is a different decision
-- with different failure modes; T181 registers it rather than pretending
-- this migration made it.

-- (2) 被拒绝的槽位更新, and the moment the current takeover began -- the
-- same four columns answer both, which is the point independent review
-- P1-1 made: `conversation_takeover.create` rows are in this table and
-- their `created_at` is the lower bound the projection needs.
--
-- Withheld: `actor_id` (always NULL on refusal rows, and the field an
-- audit reader would use to profile *people*), `correlation_id` (the key
-- that stitches audit rows into a request chain across resources),
-- `resource_type` (the route already knows it) and `id`.
GRANT SELECT (tenant_id, action, resource_id, reason_code, evidence, created_at)
    ON audit_events TO fastagent_app;

-- (5) 理解步被成本闸门中断. Withheld, all nine of them: `id`, `intent_id`,
-- `hook`, `effect`, `modifications`, `obligations`, `policy_id`,
-- `policy_version` and `context` -- the payload of every policy judgement
-- the engine has ever made. The projection needs to count rows and name
-- the latest, and that is all these four columns allow.
--
-- (The first draft of this comment said "five columns" and listed seven
-- withheld, dropping `hook` and `effect` -- independent review P3-2. A
-- migration comment is read here as the grant list itself, the way `032`'s
-- is quoted, so a short list reads as a wider grant than was made.)
GRANT SELECT (tenant_id, conversation_id, reason_code, created_at)
    ON policy_decisions TO fastagent_app;

-- **Indexes, in the same migration that opens the read** (independent
-- review P2-2). `audit_events` had no index but its primary key, and this
-- route's query was a `Seq Scan` filtering 20 002 rows to answer with 0 --
-- a cost proportional to the tenant's whole audit history and unrelated to
-- the answer. `audit_events` takes a row per inbound message and has no
-- retention, and both `GET /conversations/{id}` and `resume-preview` run
-- these queries, the second one repeatedly while a person holds the
-- conversation.
--
-- `033` and `041` each built their indexes in the migration that opened
-- the read they serve -- `033` because an earlier independent review said
-- so. This is the third such read and was the first to skip it.
--
-- **Not a partial index on the refusal action**: the same three-column
-- prefix answers the takeover-period lookup, which filters a *different*
-- action on the same conversation. One index, both queries.
CREATE INDEX IF NOT EXISTS audit_events_resource_action_recent_idx
    ON audit_events (tenant_id, resource_id, action, created_at DESC);
CREATE INDEX IF NOT EXISTS policy_decisions_conversation_reason_idx
    ON policy_decisions (tenant_id, conversation_id, reason_code, created_at);

COMMENT ON COLUMN audit_events.evidence IS
    'Written by every audit producer; readable by fastagent_app since 042 '
    '(column-level, tenant-scoped by RLS). A producer that puts customer '
    'values in here is putting them within reach of a control-plane read, so '
    'the slot-refusal writer records slot names and confidences and no values.';

INSERT INTO schema_migrations(version) VALUES ('042_control_plane_governance_read')
ON CONFLICT (version) DO NOTHING;
