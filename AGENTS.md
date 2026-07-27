# PrivAiTe, guide for AI agents (read this before changing code)

PrivAiTe is a self-hosted, OpenAI-compatible proxy that **detects PII, replaces it
with reversible placeholders before forwarding to the LLM provider, and restores
the real values in the response.** It is a privacy tool: a single leak defeats its
purpose. The rules below each exist because breaking them caused a real bug. Treat
them as invariants, not suggestions.

## Non-negotiable invariants

1. **Fail closed.** If detection/anonymization raises, the request is **blocked**,
   never forwarded. `pii.on_error` defaults to `"block"`; detector exceptions
   re-raise (`engine._detect_all`). Do not add a fallback that forwards raw text on
   error. Startup refuses unsafe configs (see #6).
2. **One choke point.** Every piece of user text reaches the provider only through
   `PIIEngine._anonymize_text`. The `block_entities` gate lives there. If you add a
   new field/path that carries user text, route it through the engine, never
   forward it directly.
3. **Restore parity.** Whatever is anonymized must be restored on the way back, in
   **both streaming and non-streaming**: `content`, `tool_calls[].function.arguments`,
   legacy `function_call.arguments`, `reasoning_content`/`reasoning`, `refusal`,
   and the audio `transcript`.
4. **`mask` and `redact` are irreversible.** They must NOT enter the reversible
   mapping (`mapping.note()`, not `mapping.add()`). Two values that mask to the same
   string must never cross-restore. Only `placeholder` and `fake_replacement` are
   reversible.
5. **Never log or echo PII.** Errors name entity **types**, never values
   (`PIIBlockedError`, `UnsupportedContentError`). No logger call may emit message
   content or entity values. The reversible map is per-request, in-memory only.
   The opt-in detection cache (`privaite/pii/cache.py`) may hold only salted
   hashes and span metadata (offsets, types, scores, sources): never text,
   values, anonymized output, or mapping state. Its privacy delta is documented
   in the README threat model; keep that section in sync if you touch it.
6. **Startup fails fast on unsafe config**, it never degrades silently: `pii.enabled`
   with 0 detectors, `merge_strategy: intersection` with <2 detectors, a Presidio
   language with no spaCy model, duplicate provider `model_name` aliases, and a
   `block_entities` type no enabled detector can emit (conservative check: skipped
   when a detector's producible set is unknown) all raise at boot.
7. **Config defaults are the safety posture, do not flip them casually.**
   `preset: onnx` (~84.5% recall), `on_error: block`, `deanonymization.fuzzy_matching: false`
   (fuzzy can mis-substitute), detector `trust_remote_code: false`, `block_entities: []`.

## What is and isn't scanned (know the surface)

Scanned: `messages[].content` (string, multimodal text parts, or bare strings in a
list); `tool_calls`/`function_call` arguments parsed as JSON and scrubbed value by
value **including numeric leaves with >=7 digits** (a card number sent as a bare
number is caught); `/v1/completions` `prompt` and `suffix`; `/v1/embeddings`
`input`; chat `prediction.content` and `web_search_options.user_location`
(auxiliary request fields, scrubbed input-side via `process_request_value`, no
restore path needed).

NOT scanned (documented limitation, keep it documented if you change it):
`messages[].name`, top-level `user`/`metadata`, `tools`/`functions` definitions,
JSON object keys, and tokenized (integer-array) inputs.

`POST /v1/pii/inspect` (off by default, `pii.inspect.enabled`) is a dry run: it
returns the caller's own detections + anonymized preview and forwards NOTHING.
It is the one deliberate exception to "never echo values" (the caller sent the
text; invariant #5 still fully applies to logs, errors and /stats, and the
endpoint module deliberately has no logger). Keep it excluded from /stats.

## Gateway: the SECOND scrub surface (`privaite/gateway/`)

Opt-in agent CLI routes (`gateway.enabled`, off by default): `/v1/messages`,
`/v1/messages/count_tokens` (Anthropic Messages, Claude Code) and `/v1/responses`
(OpenAI Responses, Codex, beta). They are a **parallel request path** to
`privaite/api/`: a core-side fix (a new scanned field, a new restored field, a
gate) is NOT automatically true here. Check both.

- **Scrub (`gateway/scrub.py`)** still goes through the one choke point: every
  scanned string reaches `PIIEngine.scrub_document` (hence `_anonymize_text`),
  so `block_entities` and fail-closed apply unchanged. Rules that exist because
  breaking them leaked: scrub coverage must match restore coverage (restore
  rewrites every string leaf, so the client holds REAL values inside
  `server_tool_use`/`mcp_tool_use` blocks and echoes them back next turn: scrub
  them); tool OUTPUT carriers are where a file the agent read arrives
  (`custom_tool_call_output` and friends, a list of `{type,text}` parts, not
  just a string); an Anthropic block type this build does not know is scanned
  through an allowlist of plaintext field names, never relayed raw. Relayed
  byte-for-byte: `thinking`/`redacted_thinking`, binary and opaque payloads,
  and the Responses opaque item types. The agent's own prompt (`system`,
  `instructions`) is the one field read but not rewritten: it goes through
  `PIIEngine.gate_document`, so `block_entities` still rejects the request while
  the prompt reaches the provider verbatim. The exact frozensets are documented
  in `docs/gateway.md` and pinned by `tests/test_gateway/test_gateway_docs.py`:
  change one, change the other.
- **Restore (`gateway/restore.py`)** is NOT the core restore. `restore_tree`
  rewrites **every string leaf** of the response except blocks whose type is in
  the protocol's `skip_restore_types` (thinking, encrypted reasoning/compaction),
  and an `arguments` string is restored on its PARSED tree and re-encoded, never
  by plain substitution (splicing a raw quote/backslash/newline back into a JSON
  string literal breaks the client's `json.loads`).
- **A separate SSE restorer** lives here too (`_SSERestorer`), driven by the
  declarative event plans in `gateway/protocols.py`: one `StreamingDeAnonymizer`
  per text channel so a placeholder split across two events still restores, a
  flush at channel end and at `[DONE]` so held-back text is never dropped, and
  `json_escaped_mapping` on JSON-fragment channels (streamed tool arguments) so
  the spliced original stays a valid piece of the JSON string literal it lands
  in. Upstream framing (event names, comments, `[DONE]`) is preserved verbatim.
- **Relay (`gateway/relay.py`)**: `accept-encoding` is never forwarded (the
  proxy must be able to decode what comes back), duplicate header names are
  relayed verbatim as a list of pairs, an upstream timeout maps to 504 and a
  transport failure to 502.
- **Auth**: gateway paths are deliberately skipped by `AuthMiddleware` when
  gateway mode is on (the only credential in the request is the client's own
  upstream token). With the default `server.host: 0.0.0.0` and no rate limit,
  an exposed port plus gateway mode is an open endpoint. Documented in
  `docs/gateway.md`; do not "fix" it silently in either direction.

## Streaming handler (`privaite/streaming/handler.py`)

Forward the provider's own chunks (preserve `id`, `usage`, `logprobs`, and the real
finish chunk: do not synthesize a duplicate finish). One restore buffer **per
choice index** (n>1 must not share). Flush held-back text onto the finish chunk and
again after the stream ends without a `finish_reason`. Never suppress a chunk that
carries any payload other than fully-held-back content.

## Before you push or release

- Run in the repo venv (`.venv`): `pytest`, `ruff check`, `ruff format --check`,
  and **`mypy privaite/ integrations/openwebui/privaite_filter.py integrations/litellm/privaite_guardrail.py`**.
  Both the push CI and the publish workflow type-check the integrations:
  skipping them locally once broke a release.
- Version: bump **both** `pyproject.toml` and `privaite/__init__.py`; date the
  `CHANGELOG.md` section; bump the integration pins (`privaite>=X`).
- Docs: `python scripts/gen_llms_full.py` after ANY documentation change
  (`--check` fails when stale). `llms-full.txt` is a concatenation and went
  stale for three releases once; `llms.txt` and `docs/llms.txt` are the same
  file and must stay identical.
- Benchmark: if you touched detection, re-run `privaite-bench`
  (`python -m solutions.compare` from that repo root) and update `COMPARISON.md` +
  the README numbers. The published numbers must match the shipped code.
- Release: `gh release create vX.Y.Z` triggers `publish.yml` → PyPI (trusted
  publisher; its Environment field must stay empty). A PyPI version can never be
  reused, so verify green CI first.

## Integrations must stay in sync with the core

`integrations/litellm/privaite_guardrail.py` and
`integrations/openwebui/privaite_filter.py` run the **same engine in-process**. Any
core behavior change (fuzzy, block gate, a new restored/scanned field) must be
mirrored in both.

- OpenWebUI filter: `outlet` must **pop** the reversible map (Open WebUI may persist
  message metadata); do not stash it when `deanonymize` is off; clear any
  client-supplied incoming map in `inlet`.
- LiteLLM guardrail: scan **every** Responses `input` item (role message, tool
  output, tool-call arguments, `input_text` part, bare string), not just a
  homogeneous list.
- The guardrail also lives upstream in the fork `crp4222/litellm`, branch
  `feat/privaite-guardrail` (PR #31530 to BerriAI/litellm). Sync fixes there too.
  Fork PRs target `litellm_oss_staging`, **not** `main`. The fork must pass
  litellm's gates: ruff-strict (`UP006` lowercase `list`, `C901` complexity <=10,
  no net-new `Any`), type-discipline (`LIT007`: no `TypeGuard`/`TypeIs`), line
  length 120. Its tests fake the `privaite` package because it is absent in litellm
  CI.

## Verify, don't assume

Reproduce a suspected bug before fixing it, and re-run the exact command the CI/gate
runs (not a subset). For provider/streaming behavior, test against a real model
through the local proxy (`python -m privaite --config <yaml>` then curl with
`stream: true`); running with `deanonymization.enabled: false` proves the provider
only ever received placeholders.
