# Secret setup

The ZIP intentionally contains no production tokens, admin keys, refresh tokens, or API keys.
The pasted credentials must be treated as exposed and rotated before deployment.

## Railway Variables

Set these in Railway Variables, not in Git:

- `SHELL_API_KEY`
- `KIMI_API_KEY` or the provider key used by the runtime
- `KIMI_BASE_URL`
- `KIMI_MODEL`
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` when using an OpenAI-compatible provider

## PHP bridge

Copy `config/secrets.php.example` to a server-only secrets file outside the public web root, fill it there, and ensure the filename is excluded from Git and ZIP exports.

Never put real values into `.env.example`, JavaScript, browser requests, README files, screenshots, or public repositories.
