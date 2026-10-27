"""Failed-password-attempt throttling for the two surfaces that verify one.

**The bypass this closes.** ADR-0048 §二.5 (K8) says a person owing a password
change may do nothing except change it: every route answers 403
`PASSWORD_CHANGE_REQUIRED`. `POST /auth/change-password` is the single exemption,
and it takes the **current password** in the body -- so the one route a
locked-out token can still reach is a route that checks a secret, and it could
be called as many times as the attacker liked. The lockout looked like a
containment boundary and was, for guessing purposes, a doorway. B1 #17's
independent review named it as a residual risk; the user's ruling was to fix it
now, on the grounds that a security mechanism was being bypassed.

`POST /auth/login` is throttled by the same code and in the same batch. It is
the other surface that turns a plaintext password into a yes/no, it has never
had any limit, and leaving it out would mean an attacker with no token at all
faced fewer obstacles than one with a locked-out token.

**Which Redis, and why that is not a new decision.** ADR-0001 §2 gave v2 an
"独立 cache/rate-limit Redis" -- provisioned, health-checked and preflighted
since M0, and until now used by nothing. This is the use it was named for.

**The window is per identity under attack, not per caller.** Keys are
`(tenant, email)` for login and the employee id for a password change: what is
being protected is an *account*, and an attacker who can rotate source addresses
would walk around a caller-keyed limit. The cost is stated rather than hidden --
see `docs/backlog/ideas.md` T131: **an attacker spraying one guess across many
accounts is not slowed by this**, and neither is a distributed attempt on many
accounts at once. Closing that needs a second, caller-keyed dimension and a
trustworthy client address, which the ingress layer does not hand us today.

**Both halves of that trade are stated, because only one of them was** (throttle
independent review 2026-09-05, P2-1). T131 records that spraying one guess across
many accounts is not slowed by this. The other half is sharper and was missing:
**anybody who knows an employee's email address can keep that account locked
out** -- ten wrong passwords, and the real owner's correct password answers 429
for fifteen minutes, renewable forever. That is the standard cost of
identity-keyed limiting, the reviewer did not ask for a design change, and not
limiting is worse; it is registered as **T134** so that the first real merchant
does not discover it as a surprise.

**Failures count; successes clear.** A window that counted successes would lock
out the one person using their account correctly, and this repository has enough
ways to lock somebody out already.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError


class ThrottleUnavailable(RuntimeError):
    """The limiter could not be consulted, so the caller must refuse.

    **Fail closed, and loudly.** The alternative -- carry on when the limiter is
    unreachable -- makes the protection disappear exactly when nobody is
    watching, and leaves a system that reports itself as protected. This
    repository has written that lesson down repeatedly under a different name:
    a guard that silently stops guarding is worse than no guard, because the
    record then says one thing and the system does another.

    The cost is real and is not hidden: **while the cache Redis is unreachable,
    nobody can log in.** That is a new availability coupling for
    `POST /auth/login`, it is registered as **T132**, and the alternative
    (fail open on login, fail closed on password change) is written there for
    whoever revisits it.
    """


class ThrottleExceeded(RuntimeError):
    """Too many failed attempts on this identity inside the window."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    """How many failures, over how long.

    Two policies exist and they differ on purpose. Login is a thing people get
    wrong: a typo, an old password, the wrong tenant code. A password change is
    a thing people get wrong far less often -- they are looking at the password
    they just typed into the other field -- and it is the surface reachable by a
    token that is otherwise locked out. So the change window is tighter.
    """

    limit: int
    window_seconds: int


#: Ten wrong passwords in fifteen minutes on one account.
#:
#: Chosen to sit well above human error and far below anything useful to a
#: guesser: at this rate a four-digit PIN would take a day and a real password
#: longer than the company will exist. It is not derived from a drawing -- no
#: drawing gives a number -- so it is a judgement, and the reasoning is here so
#: that changing it is an argument rather than a preference.
LOGIN_POLICY = ThrottlePolicy(limit=10, window_seconds=900)

#: Five, on the surface K8 leaves open to a locked-out token.
PASSWORD_CHANGE_POLICY = ThrottlePolicy(limit=5, window_seconds=900)


class AttemptThrottle:
    """A fixed window per key, in the Redis ADR-0001 set aside for it.

    Fixed window rather than sliding: a sliding window costs a sorted set per
    identity and buys precision at the boundary that does not matter here --
    the worst case is twice the limit across two adjacent windows, which is
    still twenty guesses an hour rather than unbounded.
    """

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    @staticmethod
    def _key(scope: str, identity: str) -> str:
        return f"fastagent:v2:throttle:{scope}:{identity}"

    def guard(self, *, scope: str, identity: str, policy: ThrottlePolicy) -> None:
        """Refuse before the password is verified, not after.

        Checking first is what makes the limit worth having: argon2 is
        deliberately expensive, so a limiter consulted afterwards would let an
        attacker spend the server's CPU at will and only then be told no.
        """

        key = self._key(scope, identity)
        try:
            recorded = self._client.get(key)
            if recorded is None:
                return
            if int(recorded) < policy.limit:
                return
            remaining = self._client.ttl(key)
        except RedisError as error:
            raise ThrottleUnavailable(str(error)) from error
        except ValueError as error:
            # A key holding something that is not a number is somebody else's
            # key, or corruption. Either way this is not a limiter answer.
            raise ThrottleUnavailable(f"throttle key is not a counter: {key}") from error
        # **-1 and -2 are not the same thing, and treating them alike locked an
        # account forever** (independent review 2026-09-05, P3-2). `ttl` answers
        # **-2** for a missing key -- there is genuinely no window left -- and
        # **-1** for a key with *no expiry at all*. The second is reachable: an
        # `incr` that succeeds followed by an `expire` that does not (the process
        # is killed, Redis blips). Once the counter is at the limit, `guard`
        # raises before `record_failure` runs, so the `expire(..., nx=True)` that
        # would have set a TTL never gets another chance -- the key, and the
        # account, are stuck while the answer keeps promising fifteen minutes.
        #
        # So a key with no expiry gets one here. Repairing rather than refusing:
        # the counter is real and its failures did happen; what is missing is
        # only the window they should age out of.
        if remaining == -1:
            try:
                self._client.expire(key, policy.window_seconds)
            except RedisError as error:
                raise ThrottleUnavailable(str(error)) from error
            remaining = policy.window_seconds
        raise ThrottleExceeded(
            retry_after_seconds=remaining if remaining and remaining > 0 else policy.window_seconds
        )

    def record_failure(self, *, scope: str, identity: str, policy: ThrottlePolicy) -> None:
        """One more wrong answer, and start the window if this is the first.

        `expire(..., nx=True)` rather than a bare `expire`: refreshing the TTL on
        every failure would let a steady trickle of guesses hold the window open
        forever without ever tripping the limit -- the counter would be reset by
        nothing and the block would never begin.
        """

        key = self._key(scope, identity)
        try:
            self._client.incr(key)
            self._client.expire(key, policy.window_seconds, nx=True)
        except RedisError as error:
            raise ThrottleUnavailable(str(error)) from error

    def clear(self, *, scope: str, identity: str) -> None:
        """A correct password ends the window.

        Deliberately not raising `ThrottleUnavailable`: this runs *after* a
        successful authentication, and failing the request at that point would
        turn a limiter outage into "your password is right and you still cannot
        log in". The counter expires on its own, so the worst case of a swallowed
        error here is that a person who mistyped twice and then succeeded keeps
        two failures against them until the window ends.
        """

        try:
            self._client.delete(self._key(scope, identity))
        except RedisError:
            return
