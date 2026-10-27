# Ingress process

Run `uvicorn fastagent_v2.ingress.app:app --host 0.0.0.0 --port 8081 --no-access-log`.
Proxy access logs must redact the per-config path token before Stage is allowed.
