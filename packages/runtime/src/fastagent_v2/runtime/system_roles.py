"""K3 (ADR-0047 §二.6): the three default system roles, and how they stay true.

The user's ruling names three and only three: `owner` (every grantable
permission), `operator` (the conversation / takeover / notification surface,
read and write, **without tenant or role administration**), and `viewer`
(read-only). Reasoning quoted from the ruling: it is the smallest usable set,
covers "the merchant using it themselves", and the three boundaries are clear
enough not to need a document to explain them.

**Why two of the three are computed and one is written out.**

`owner` and `viewer` are *rules*: "everything grantable" and "everything that
only reads". A rule can be evaluated against the catalog, so it cannot drift
when the catalog grows -- and the catalog is going to grow forty routes' worth.
Writing them out as literal lists would mean every new action silently leaves
`owner` behind, which is the same "the record says one thing, the system does
another" family as T92 and T98.

`operator` is a *judgement*: which surface belongs to somebody who works
conversations. No rule computes that, so it is enumerated, and each new action
has to be considered against it deliberately. That is the honest split -- and it
is why `operator` is the one a reviewer should argue with.

**A note on what this batch could not have delivered a day ago.** Until K1
landed, `operator` would have been byte-identical to `viewer`: every write on
the conversation surface was either service-only (`conversation_aggregate.update`,
T98) or had no catalogued action at all (takeover, resume). Shipping two roles
with the same permissions would have been a boundary that reads like one and is
not. K1's command resources are what gave `operator` something to hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from fastagent_v2.runtime.authz import assignable_permissions

#: Verbs that only read. `list` and `read` are the two the catalog uses; the
#: rest of §3.2's primitives all change something.
#:
#: **`viewer` is this rule applied to the catalog, so it grows on its own** --
#: registered as **T114**, whose expiry was "the first batch after this that adds
#: a read action to the catalog must answer whether that read belongs in
#: `viewer`, rather than letting it in by default".
#:
#: **B2 #1 is that batch, and the answer is yes** for `channel_config.list`. What
#: `GET /channel-bindings` returns is channel *wiring* -- which channels exist,
#: whether they are active, which profile release they are pinned to, and when
#: credentials rotate. No secret, no hash, no customer content. Somebody trusted
#: to read a merchant's console can see that their WeCom channel is live.
#:
#: **The criterion, so the next batch does not have to re-derive it**: a read
#: action joins `viewer` unless it exposes either
#:
#:   * conversation or message content -- what customers said; or
#:   * any credential-derived value -- a hash, a fingerprint, a `secret_ref`, a
#:     version, or anything else `unified-authorization-layer.md:46` calls masked
#:     metadata.
#:
#: The second half is the case T114 was actually worried about, and **it has now
#: arrived** (B2 #4, 2026-09-06). Migration 029 puts K9's five metadata columns
#: on `channel_bindings`, and the freeze requires `GET /channel-bindings` to
#: return them -- `secret_ref`, `version`, `fingerprint` among them, which is
#: the second bullet above, word for word. **So `channel_config.list` leaves
#: `viewer`**, and T114's fear was exactly right: the role would have taken this
#: read in by definition, with nobody deciding anything.
#:
#: **The alternative, stated because it is the better design and was not
#: chosen here.** Split the action -- `channel_config.list` keeps the wiring and
#: stays in `viewer`, a second action carries the credential metadata -- which
#: would keep a useful read in the weakest role instead of taking it away. It
#: needs one route to project different fields for different permissions, and
#: this codebase has no field-level authorization anywhere; introducing it as a
#: side effect of a role question is the shape ADR-0053 §五 refuses. Registered
#: as **T164** with the reasoning, rather than done quietly or forgotten.
READ_ONLY_VERBS = frozenset({"read", "list"})

#: Read actions that are **not** in `viewer`, each with the criterion applied.
#:
#: The mechanism exists because `viewer` is otherwise *derived* -- every
#: read-only action in the catalog, by definition -- and T114's whole point is
#: that a definition cannot make this decision. Every entry here is a decision
#: somebody made and can be read back.
VIEWER_EXCLUSIONS = frozenset(
    {
        # `GET /channel-bindings` returns K9's masked metadata (029): a
        # `secret_ref`, a version and a fingerprint. That is the criterion's
        # second bullet, so the read does not belong to the role most likely to
        # be handed out without much thought.
        "channel_config.list",
        # **The first bullet, firing for the first time** (B3 #3).
        # `GET /conversations/{id}/messages` returns what customers said --
        # the envelope content itself, redacted but not summarised. That is
        # "conversation or message content" word for word, so no decision was
        # needed here beyond applying the sentence already written above.
        #
        # `operator` reaches it by enumeration (it works conversations) and
        # `owner` by being every grantable action; only `viewer` is refused.
        "message.list",
        # **The first bullet again, and the reason is `evidence`** (B3 #15).
        # A task row is mostly administrative -- status, owner, SLA -- but
        # `evidence` is free-form by design, and B3 #16 makes a person fill
        # it in while looking at a conversation. What lands there is what
        # the customer said, in the words of whoever filed the task.
        #
        # The alternative was to drop `evidence` from the list projection
        # and let `viewer` keep the read. That would leave the column with
        # no reader anywhere -- the shape ADR-0061 registered as C14 -- so
        # the content is returned and the role that is handed out without
        # much thought does not get it.
        "manual_task.list",
        # **The customer's identity, on the same criterion** (B3 #21-#23, user
        # ruling 2026-09-06). `external_user_id` is the id of a real person in
        # the merchant's channel, and `profile_data` is reserved for their name
        # and avatar. The freeze note for #21 asks for a *field-level* gate; the
        # ruling is that M1a puts the whole read behind the action instead --
        # with `profile_data` empty until something can fill it, a base tier
        # without the identity shows an internal uuid and two counters. The
        # split is registered as T179 with its due date written down.
        "contact.list",
        "contact.read",
        # **What we remember about the customer, on the first bullet and then
        # some** (E4-3, B4 #8). `contact.read` is excluded because
        # `external_user_id` and `profile_data` identify a real person;
        # `GET /contacts/{id}/memories` returns **the facts that person told us**
        # -- ring size, budget, a child's grade, whatever this merchant declared
        # in `memory_fields`. That is conversation content that outlived its
        # conversation, so the first bullet applies at least as hard here as it
        # does to `message.list`.
        #
        # **There is no field-level half to argue about**, unlike
        # `channel_config.list` and T164: the value *is* the resource. Dropping
        # `field_value` from the projection would leave a list of column names
        # nobody asked for.
        "contact_memory.list",
        # **The list carries a redacted last-message summary** (B3 #1), which
        # is the same customer text `message.list` was excluded for -- a
        # snippet is still what somebody said. The other eight fields would
        # be fine for a viewer; the ninth is not, and a projection cannot be
        # half-granted (T174 is the registration of exactly that gap).
        "conversation_aggregate.list",
    }
)

#: `operator`'s surface, enumerated because no rule produces it.
#:
#: Deliberately tight, and the reason is asymmetric cost: a role that grants too
#: little produces a complaint from somebody who cannot do their job, and is
#: fixed in one line. A role that grants too much produces nothing at all --
#: nobody reports a permission they did not need -- which is exactly the failure
#: T98 was written for.
#:
#: So the ruling's words are read narrowly. "会话 / 接管 / 通知相关的读写" is
#: the conversation surface itself plus the two takeover commands, and the two
#: session-shaped permissions a console needs to hold a connection open:
#:
#:   * `conversation_aggregate.read` -- see the conversation;
#:   * `conversation_takeover.create` / `conversation_resume.create` -- K1's
#:     command resources, the reason this role differs from `viewer` at all;
#:   * `websocket_ticket.create` -- open the realtime channel; without it the
#:     console cannot function.
#:
#: **`session.read` was here and is gone** (B1 #16, T125). It was here so that a
#: console could read its own session -- and `GET /auth/me`, the only route that
#: ever asked for it, stopped asking: reading who you are is now reachable by
#: being the subject. Leaving it would have left this role granting a permission
#: no route consults, which is precisely the failure T98 was written for, seeded
#: by the platform into a role it seeds itself.
#:
#: Explicitly **not** here, each for a reason:
#:
#:   * `role.*`, `tenant.read` -- the ruling says "不含租户与角色管理", and
#:     reading the tenant is not part of the surface it names;
#:   * `employee.list` / `employee.read` -- an operator console will eventually
#:     want to show *who* took a conversation over (that is K7's argument for
#:     `display_name`). It is not in the surface the ruling names, and adding it
#:     when B3 needs it is one line;
#:   * `scheduled_event.create` -- proactive follow-up is the second batch;
#:   * `profile_*`, `permission.list`, `service_health.read` -- not this surface.
#:
#: **`channel_config.list` is deliberately not here, and B2 #1 owed this
#: sentence** (independent review 2026-09-05, P3-1). That batch put the action
#: into `viewer` because channel wiring is not secret -- and the same argument
#: applies at least as well to the role that actually works the console. It is
#: withheld anyway: the ruling says `operator` is "不含租户与角色管理", and which
#: channels a tenant has wired up is tenant *configuration*, not the conversation
#: surface this role names. `viewer` seeing more than `operator` in one narrow
#: place is K3's existing shape (`test_operator_and_viewer_are_not_the_same_role`
#: asserts both directions), not an accident of this batch.
#:
#: Notifications are named in the ruling and absent here because **no
#: notification action exists in the catalog yet** (freeze B3 #13/#14 were moved
#: out of M1a by ADR-0048 K17). When they arrive they belong in this set.
OPERATOR_ACTIONS = frozenset(
    {
        "conversation_aggregate.read",
        # **Added by B3 #3, and the narrow reading requires it rather than
        # merely allowing it.** The ruling names 「会话 ... 相关的读写」 and the
        # comment above reads that as "see the conversation" -- which mapped
        # to the aggregate read only because, when this list was written, no
        # message route existed. An operator who takes a conversation over
        # and cannot read what the customer said cannot work it; the
        # aggregate gives state, not words.
        #
        # It is **not** in `viewer` (see `VIEWER_EXCLUSIONS`): the difference
        # between the two roles is exactly that an operator is trusted with
        # conversation content and a viewer is not.
        "message.list",
        # **Added by B3 #25.** Writing a slot by hand is what an agent does
        # *while* holding the conversation -- ADR-0054 §三.2 exists so the
        # model cannot overwrite those values afterwards. An operator who
        # can take a conversation over but cannot record what the customer
        # told them has nowhere to put it.
        "conversation_slot.update",
        # **Added by B3 #5.** Marking a conversation read is an act on
        # your own state, and an operator's day is a list of
        # conversations with unread counts on it. It is a write, so
        # `viewer` does not get it -- a viewer may read conversations and
        # cannot clear their own count, which is a real consequence of
        # "reads only" rather than an oversight; if it is the wrong trade
        # the fix is a decision about the role.
        "conversation_read_cursor.update",
        # **Added by B3 #15/#16.** An operator works conversations, and
        # the manual task is the record of work a conversation produced
        # that the machine cannot finish. Filing one and reading the
        # backlog are the two halves of that, so both are here; the four
        # verbs that *move* a task (B3 #17-#20) are not built yet and are
        # deliberately not pre-granted.
        "manual_task.create",
        "manual_task.list",
        # **Two of B3 #17-#20, and the other two are refused on the same
        # criterion.** Taking a task and finishing it is working a
        # conversation. Reassigning one is deciding what somebody else
        # will spend their afternoon on, and cancelling one is deciding
        # that work the system asked for will not happen -- both are
        # decisions about another person queue, and an operator who
        # needs them can be granted them by name.
        # An operator works conversations, and a conversation is with somebody.
        "contact.list",
        "contact.read",
        "conversation_aggregate.list",
        "manual_task.claim",
        "manual_task_resolution.create",
        "conversation_takeover.create",
        "conversation_resume.create",
        "websocket_ticket.create",
        # **All four memory actions** (E4-3, B4 #8-#11). The operator is the
        # only role that is *with* the customer, and 「她妈妈戴 18 号圈口」 is learned
        # in a conversation. `contact_memory.list` / `.create` follow the same
        # enumeration `conversation_slot.update` did: an agent who is told
        # something and has nowhere to put it is the shape ADR-0054 §三.2 exists
        # to prevent.
        #
        # **`.update` and `.invalidate` are here too, and that is the part worth
        # arguing with.** The stingy reading is `manual_task`'s -- file one but
        # do not let an operator cancel somebody else's -- and it was tried
        # first. It produces a role that can **record a fact and never correct
        # its own typo**, which is worse than either extreme: ADR-0131 M3 gives
        # people the right to correct precisely because the model does not have
        # it, and leaving that right only with `owner` means the wrong fact keeps
        # being used until an admin is found.
        #
        # The failure this opens is bounded and visible: a tombstone does not
        # remove the row, `GET …/memories` still returns it with its
        # `tombstoned_at`, and every move writes an `audit_events` row naming the
        # employee. **The author made this call; it is one line to overturn.**
        "contact_memory.list",
        "contact_memory.create",
        "contact_memory.update",
        "contact_memory.invalidate",
    }
)

SYSTEM_ROLE_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "owner": "every grantable permission",
        "operator": "works conversations: read, take over, resume",
        "viewer": "reads only",
    }
)


#: `viewer`'s exact contents, pinned so that the rule cannot grow in silence.
#:
#: **T114 asked for "not automatically", and a criterion in a comment is not
#: that** (independent review 2026-09-05, P2-4). `viewer` is still *computed* --
#: the rule does not drift and does not need per-action maintenance -- but the
#: computed answer is compared against this snapshot, so every action that joins
#: it costs somebody a line here, in a diff, next to the criterion above.
#:
#: That is the third option the B2 #1 self-review missed when it framed this as
#: "either a rule or a rule-minus-exceptions": the rule stays, and the *growth*
#: becomes reviewable.
#:
#: The half this exists for has not arrived yet: B2 #2/#4 add reads over
#: credential-derived values (`fingerprint`, `version`), and the criterion says
#: those do **not** belong in `viewer`. Without this snapshot they would join it
#: in the same commit that added them, silently, which is exactly what T114 was
#: registered to prevent.
VIEWER_SNAPSHOT = frozenset(
    {
        # `channel_config.list` was here until B2 #4; see `VIEWER_EXCLUSIONS`.
        # **`control_policy.read` joined by derivation (B3 #10), and the
        # tripwire is why that is a sentence somebody wrote rather than a
        # thing that happened.** It is a read, so `viewer` gets it unless the
        # exclusion criterion applies -- and it does not: two operational
        # switches are neither conversation content nor credential metadata.
        # Knowing that new conversations start with the AI answering is the
        # kind of thing a read-only role is for.
        "control_policy.read",
        "conversation_aggregate.read",
        "employee.list",
        "employee.read",
        # **资料目录与单份资料（E3-6）。** 绊线在本批打响，报的是
        # `added ['material.list', 'material.read']`，下面两行是把判据套上去的答案。
        #
        # **三条排除判据一条都不命中**：这两个读返回的是**商家自己的销售资料的
        # 元数据**——文件名、媒体类型、字节数、内容哈希、在架还是下架。
        # 不是对话或消息内容（客户一个字都没出现在里面），
        # 不是凭据元数据，也不是客户的身份。
        #
        # **顺带一条判断，写在这里免得下一个人重新推一遍**：
        # `material.invalidate`（下架）不在 `viewer` 里，因为它不是读；
        # 它也**不在 `OPERATOR_ACTIONS` 里**——操作员做的是会话里的活，
        # 而「哪几份资料还能发给客户」是**商家的内容决定**，与改 profile 同族。
        # 需要它的人按名字单独授予。
        "material.list",
        "material.read",
        "permission.list",
        "profile_release.read",
        "profile_schema.read",
        "role.list",
        "service_health.read",
        "tenant.read",
    }
)


def system_role_permissions() -> Mapping[str, frozenset[str]]:
    """The three roles' permission sets, resolved against today's catalog.

    Recomputed rather than cached: the catalog is read from disk by
    `assignable_permissions()`, and a process that resolved these once at import
    would answer with the catalog as it was when it started. That matters for
    the migration path, which is the one caller that must see the catalog it was
    deployed with.
    """

    # **Platform actions need no subtraction here** (ADR-0070 §四): they never
    # enter `assignable_permissions()` at all, so neither derived role can pick
    # one up. B1 #9 subtracted them from both sets while they were still
    # assignable; narrowing the source made that net dead code, and dead code in
    # a security derivation reads as a guard that is doing something.
    #
    # `VIEWER_EXCLUSIONS` below is a different thing and stays: those are
    # *tenant* reads that were decided out of `viewer` one at a time (B2 #4).
    # **Platform actions come out of every tenant-scoped role**, not only be
    # refused by `authorize()`. A role that carries a permission its holder can
    # never use is T98's shape exactly: stored, shown to an administrator, and
    # denied on use. The authorizer refusing it is the safety net; not granting
    # it in the first place is the honest answer.
    #
    # **Both roles, and the `VIEWER_SNAPSHOT` tripwire is how that was found.**
    # The first version subtracted from `grantable` only, so `owner` was clean
    # while `tenant_registry.list` -- a read -- walked straight into `viewer` by
    # the derivation rule. The deploy-time guard caught it on the first test
    # run, which is exactly the failure T114 registered: a role that grows by
    # definition rather than by decision.
    grantable = {item["action"] for item in assignable_permissions()}
    read_only = {
        item["action"]
        for item in assignable_permissions()
        if item["verb"] in READ_ONLY_VERBS
    } - VIEWER_EXCLUSIONS
    if read_only != set(VIEWER_SNAPSHOT):
        # **Deliberately a failure, not a warning.** A `viewer` that quietly
        # gained a read is the shape T114 registered; a deploy that stops rather
        # than grows the role is the cheap end of that trade, and the fix is one
        # reviewable line in `VIEWER_SNAPSHOT` -- taken only after the criterion
        # above has been applied to the new action.
        raise ValueError(
            "viewer no longer matches VIEWER_SNAPSHOT: "
            f"added {sorted(read_only - set(VIEWER_SNAPSHOT))}, "
            f"removed {sorted(set(VIEWER_SNAPSHOT) - read_only)}. "
            "Apply the criterion above the snapshot, then update it in the same commit"
        )
    missing = OPERATOR_ACTIONS - grantable
    if missing:
        # An operator permission that is not grantable would be stored, shown to
        # an administrator, and denied on use -- T98's failure with the roles
        # the platform seeds itself. Refusing here means a catalog change that
        # removes one of these fails at deploy rather than in production.
        raise ValueError(
            f"operator names actions that are not grantable: {sorted(missing)}"
        )
    return MappingProxyType(
        {
            "owner": frozenset(grantable),
            "operator": frozenset(OPERATOR_ACTIONS),
            "viewer": frozenset(read_only),
        }
    )


def ensure_system_roles(connection: object, *, tenant_id: object) -> dict[str, int]:
    """Create or re-sync one tenant's system roles. Idempotent by construction.

    **Why this is a function run at migration time rather than rows in a
    migration file.** A migration is SQL, and SQL cannot read the resource
    catalog -- so seeding literal permission arrays would freeze `owner` and
    `viewer` at the moment the migration was written, and every catalogued
    action added afterwards would quietly not be in them. Running the rule on
    every deploy means the two computed roles are correct for the code that is
    actually deployed, with no snapshot to go stale and no follow-up migration
    per action.

    `is_system` is set here and nowhere else: it is not writable from the wire
    (CP-3 refuses it precisely so a client cannot mint a role the delete route
    will not remove), and these are the only rows entitled to it.

    **It only ever rewrites rows it owns.** `roles`'s unique key is
    `(tenant_id, name)` and `POST /roles` reserves no names, so a tenant may
    already have a role called `viewer`. The conflict clause is therefore
    restricted to `WHERE roles.is_system`; a clash is skipped and reported
    (`-1` in the returned counts) instead of taken over. The returned mapping is
    the only signal, which is why `seed_system_roles()` prints it.

    **`viewer` grows on its own, and that is worth knowing.** It is defined as
    "every grantable read", so each catalogued `read`/`list` action added by a
    later batch joins it without anybody deciding to. Harmless today -- the ten
    it holds are all innocuous -- but B2 and B3 will add reads over channel
    credentials' metadata and conversation content, and those will land in
    `viewer` by definition rather than by choice. Registered as **T114**.
    """

    counts: dict[str, int] = {}
    for name, permissions in system_role_permissions().items():
        stored = sorted(permissions)
        cursor = connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO roles(tenant_id, id, name, description, permissions, is_system)
            VALUES (%s, gen_random_uuid(), %s, %s, %s::jsonb, true)
            ON CONFLICT (tenant_id, name) DO UPDATE
              SET permissions = EXCLUDED.permissions,
                  description = EXCLUDED.description,
                  updated_at = now()
              WHERE roles.is_system
            RETURNING id
            """,
            (tenant_id, name, SYSTEM_ROLE_DESCRIPTIONS[name], json_dumps(stored)),
        )
        if cursor.fetchone() is None:
            # **A row with this name exists and is not ours.** `roles`'s unique
            # key is `(tenant_id, name)` and `POST /roles` reserves nothing, so
            # a tenant can create a role called `viewer`. Without the
            # `WHERE roles.is_system` above, the next deploy would rewrite its
            # permissions (**wider**, not narrower), set `is_system` and thereby
            # make it undeletable by its own creator -- silently, since the
            # platform path writes no `audit_events` (independent review
            # 2026-09-05, P2-1).
            #
            # Skipped and reported rather than raised: one tenant's name clash
            # is not a reason to fail everybody else's deploy, and taking the
            # row over is the thing this guard exists to prevent.
            counts[name] = -1
            continue
        counts[name] = len(stored)
    return counts


def json_dumps(value: object) -> str:
    """Local so this module has no import-time dependency beyond the catalog."""

    import json

    return json.dumps(value, ensure_ascii=False)
