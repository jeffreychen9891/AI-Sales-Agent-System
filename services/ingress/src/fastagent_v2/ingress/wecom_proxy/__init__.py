"""WeComProxy's *inbound* half: turning a callback payload into envelope content.

**The client moved out of here (B2-5).** `RestrictedWeComProxyClient` and the
three modules it carries -- the guid allowlist, the credentials loader and the
error taxonomy -- now live in `fastagent_v2.channels.wecom_proxy`.

**Not because "both halves need it" -- that was never true.** ADR-0094 §一 Q6
gave that as the reason, and it does not survive contact with the code: nothing
in `services/ingress` has ever imported the client. The real reason is the
mirror image of it. **The one service holding the client was the one service not
using it**, while `services/worker` needed its error taxonomy and could not
reach it without crossing a service boundary -- so it paid by matching exception
*names as strings*, with a comment admitting that a rename over here would
silently downgrade its receipts.

It did not go to `packages/runtime` either: `AGENTS.md` (Security and
Architecture) says core/runtime code contains no channel SDK logic, and a client
that calls the vendor's own endpoints is exactly that. `packages/channels` is
shared without being runtime, which keeps that rule true word for word (user
ruling 2026-09-09; ADR-0110).

What stays here is the half that is genuinely ingress-only: `inbound.py` reads a
callback's `msgType`/`msgData` and produces envelope content, and nothing
outside this service has any use for it.

The package is kept (rather than flattening `inbound.py` up a level) because the
name still says something true: this is the WeComProxy-shaped part of ingress,
and a second provider would sit beside it rather than inside it.
"""

from __future__ import annotations
