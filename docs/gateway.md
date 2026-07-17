# Agent CLI gateway (Claude Code, Codex)

Gateway mode lets an agent CLI point its base URL at PrivAiTe. On the way out,
the request is scrubbed by the same engine and presets as the OpenAI-compatible
endpoints, tool-call arguments included, and the CLI's own auth token is relayed
verbatim upstream. On the way back, the real values are restored, streaming
included. It is opt-in and off by default: with `gateway.enabled: false` the
routes do not exist and nothing about the proxy changes.

Verified live with Claude Code (Anthropic Messages API: `/v1/messages` and
`/v1/messages/count_tokens`) and Codex (OpenAI Responses API: `/v1/responses`),
both on regular consumer subscriptions.

## Enable it

```yaml
gateway:
  enabled: true
  anthropic:
    base_url: "https://api.anthropic.com/v1"
  openai_responses:
    base_url: "https://api.openai.com/v1"                 # API-key mode
    # base_url: "https://chatgpt.com/backend-api/codex"   # Codex on a ChatGPT subscription
```

Gateway routes carry the client's own provider credentials: PrivAiTe neither
injects nor validates any key there (`PRIVAITE_API_KEYS` applies to the
OpenAI-compatible endpoints only).

## Claude Code

```bash
ANTHROPIC_BASE_URL=http://localhost:8400 claude
```

Claude Code sends its own login (subscription OAuth or API key) with each
request; the gateway relays it as-is to `gateway.anthropic.base_url`.

## Codex

Codex only speaks the Responses API. Add a custom provider to
`~/.codex/config.toml`:

```toml
model_provider = "privaite"

[model_providers.privaite]
name = "PrivAiTe"
base_url = "http://localhost:8400/v1"
wire_api = "responses"
requires_openai_auth = true    # ChatGPT subscription login, relayed as-is
# env_key = "OPENAI_API_KEY"   # API-key mode instead: use this line, drop requires_openai_auth
```

For subscription use, also set the gateway upstream to the Codex backend
(`https://chatgpt.com/backend-api/codex`, commented in the YAML above). For
API-key mode, keep the default `https://api.openai.com/v1`.

## What is scanned (and what is not)

**Anthropic Messages:** `messages[]` string content, `text` blocks, `tool_use`
input (the tool-call-argument leak), and `tool_result` content. Not scanned:
the `system` field, `tools`/`tool_choice` definitions, `thinking` and
`redacted_thinking` blocks (Anthropic rejects modified thinking blocks echoed
back on a later turn, so they pass through untouched in both directions), and
media blocks.

**OpenAI Responses:** `input`, as a string or item by item: role message
content, `function_call` arguments (parsed as JSON, scrubbed value by value),
`function_call_output`, other text-bearing item fields, bare strings. Not
scanned: the top-level `instructions` field and `tools` definitions.

The unscanned `system` and `instructions` fields matter in practice: they are
the agent's own prompt, and Claude Code injects your `CLAUDE.md` and project
context there, so PII inside those reaches the provider. Keep secrets and
personal data out of them.

Restore covers both streaming and non-streaming responses. The same fail-closed
policy applies: if scrubbing fails, the request is rejected and nothing is
forwarded.

## Honest limits

- **The gateway protects the egress, not the agent.** Claude Code and Codex
  still hold the real values in their own context and local transcripts; only
  what reaches the provider is scrubbed. Keeping values out of the agent's own
  context would take a source-side interceptor, which a gateway is not.
- **Subscription relay.** This forwards your own subscription traffic for your
  own use. It is not a provider-supported or provider-endorsed integration, and
  the ChatGPT Codex backend is undocumented and could change without notice.
  API-key mode is the durable path.
- Everything in the README
  [threat model](https://github.com/crp4222/PrivAiTe#threat-model) still
  applies: this is pseudonymization, not anonymization, and detection is
  best-effort.
