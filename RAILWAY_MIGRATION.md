# YACINEDEV Railway Agent Bundle

Based on repository commit `939a1889d643b37cb70a36ff7e146608a3231bb5`.

## Included

- `app.py`: Flask API and `/health` endpoint.
- `agent_runtime.py`: durable agent jobs with thinking, tool events, cancellation checks, and persisted state.
- `agent_cmds.py`: `/cmd/*` tools and workspace operations.
- `Dockerfile` and `railway.json`: Railway build and health-check configuration.
- `.env.example`: variable names only; no credentials are included.
- `smoke_test.py`: read-only health/auth smoke test.

## Railway setup

1. Create a Railway service from this repository or upload this bundle.
2. Use the Dockerfile builder; `railway.json` already points to it.
3. Add Railway Variables:
   - `SHELL_API_KEY`: a long random secret.
   - One LLM credential: `KIMI_API_KEY`, `OPENAI_API_KEY`, or another compatible key.
   - Optional `KIMI_BASE_URL` and `KIMI_MODEL`.
4. Deploy and wait for `/health` to return HTTP 200.
5. Keep the API key server-side. PHP should proxy requests to Railway; browser JavaScript must not contain the key.

## Endpoints

- `GET /health`
- `POST /agent/jobs`
- `GET /agent/jobs/<job_id>`
- `POST /agent/jobs/<job_id>/cancel`
- `POST /shell`
- `POST /code/run`
- `POST /workspace/list`
- `POST /workspace/read`
- `POST /workspace/write`
- `POST /cmd/<command>`

Send `X-Api-Key: $SHELL_API_KEY` on every endpoint except `/health`.

## Summary event model

The job record stores `events`: `thinking`, `tool`, `token`, `answer`, and `error`. The PHP frontend should render each `tool` event and immediately create a fresh compact `thinking` placeholder, even if a `tool_start` event was missed. This prevents the intermittent missing-thinking state from the PHP path.

## Safety

Keep the shell allowlist, workspace path checks, per-command timeouts, and output limits. Rotate `SHELL_API_KEY` if it was ever exposed. This bundle contains no production tokens, cookies, or passwords.
