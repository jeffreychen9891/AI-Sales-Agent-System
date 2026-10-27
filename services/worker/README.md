# Worker processes

- Inbox worker: `python -m fastagent_v2.worker.cli worker`
- Scheduler: `python -m fastagent_v2.worker.cli scheduler`

The worker commits aggregate/intent/outbox facts atomically, then dispatches the outbox under a
separate recoverable lease. Both roles reload facts from PostgreSQL; Redis carries pointers only.
