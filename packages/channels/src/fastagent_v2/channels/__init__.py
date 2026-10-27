"""Provider clients: the code that actually speaks a channel's wire protocol.

**A package of its own, and the reason is a rule rather than taste.**
`AGENTS.md` (Security and Architecture) says:

    Core/runtime code contains no merchant-specific terms, tenant IDs, prompts,
    or channel SDK logic. Differences belong in versioned profiles, policies,
    and adapters.

`RestrictedWeComProxyClient` calls the vendor's own endpoints and its
`FORBIDDEN_OPERATIONS` holds the vendor's own operation names -- that is channel
SDK logic by any reading, so `packages/runtime` is not where it may live. But it
could not stay in `services/ingress` either: **ingress has never imported it**,
while `services/worker` needs its error taxonomy, so the one service holding it
was the one service not using it.

So it lives here: **shared like `runtime`, but explicitly not `runtime`.** That
keeps the rule above true word for word, with nothing amended and no reading of
it chosen on the implementation's behalf (user ruling, 2026-09-09; ADR-0110).

**What belongs here:** clients that talk to a provider, plus the guards that
travel with them -- allowlists, confirmation gates, credential loading, error
taxonomies.

**What does not:** anything that reads a *callback* into our own shapes. That is
inbound parsing, it is ingress-only, and `services/ingress` keeps it -- see
`fastagent_v2.ingress.wecom_proxy.inbound`.
"""

from __future__ import annotations
