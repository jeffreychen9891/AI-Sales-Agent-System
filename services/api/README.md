# API process

Run `uvicorn fastagent_v2.api.app:app --host 0.0.0.0 --port 8080 --no-access-log`.
The disabled access log is deliberate: short-lived WebSocket tickets must not be written to URL logs.
