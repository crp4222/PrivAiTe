# API reference

OpenAI-compatible:

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat (streaming + non-streaming) |
| `POST /v1/completions` | Text completions |
| `POST /v1/embeddings` | Embeddings (anonymized, no de-anonymization) |
| `POST /v1/pii/inspect` | Dry-run detection preview (off by default, see [verify.md](verify.md)) |
| `GET /v1/models` | List configured models |
| `GET /health` | Health check |
| `GET /ready` | Readiness check |
| `GET /stats` | PII detection stats per session |

## What gets anonymized

Scanned before anything is forwarded to the provider:

- `messages[].content`, whether a plain string or a multimodal list of parts (text parts are scrubbed, images and audio are left alone).
- `tool_calls[].function.arguments` and the legacy `function_call.arguments`: parsed as JSON and scrubbed value by value, including numeric values (a card number sent as a bare JSON number is detected too; on a hit the leaf becomes the masked string). Object keys and the function name stay intact. Arguments that are not valid JSON are scrubbed as free text.
- `/v1/completions` `prompt` and `/v1/embeddings` `input`, as a string or a list of strings.
- Auxiliary request fields that carry user text: chat `prediction.content` (predicted outputs, string or text-part list) and `web_search_options.user_location`, plus the `/v1/completions` `suffix`. These are request inputs, scrubbed on the way in only.

NOT scanned (know your surface): `messages[].name`, top-level fields like `user` and `metadata`, and `tools`/`functions` definitions are forwarded as-is; JSON object keys inside tool arguments are never rewritten (masking parameter names would break the tool schema). Keep PII out of those fields, or strip them upstream.

On the way back, the original values are restored in `message.content` and in returned `tool_calls` (including the legacy `function_call`), in both non-streaming and streaming responses. Set `pii.passthrough.tool_calls: true` to forward tool-call arguments unchanged.

## Strict mode

For a stricter posture, set `pii.strict: true`: any request whose content can't be inspected (a shape that is neither text nor a known media part, e.g. tokenized `input` arrays) is rejected with `400` instead of being forwarded.

## Passthrough caveat

`passthrough.system_messages` and `passthrough.tool_calls` skip the engine entirely for those parts, which also skips `block_entities`. Both are `false` by default; do not enable them together with a block policy you rely on.
