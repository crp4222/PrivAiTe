# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `fake_replacement`: the first retry after a collision asked the generator for
  variant 0, the seed of the initial candidate, so it reproduced the collision
  and the retry budget was effectively 9 of 10. Retries now walk variants 1 to
  10.

## [0.4.2] - 2026-08-23

### Added
- A policy page (`docs/policy.md`) presenting `custom_patterns`,
  `entity_overrides` and `block_entities` as one declarative policy: a worked
  example combining the three, the four properties that make it enforceable
  (determinism, the inspect dry-run, the startup refusal of dead block rules,
  the preset-allowlist exemption), the boundary where a rule on an ML-detected
  type inherits the detector's recall, and the structured-only limit. Linked
  from the README preset section and the docs index.
- A measurement page (`docs/agent-leak-measurement.md`): what a coding agent
  actually sends to its provider, measured at the wire on real Claude Code and
  Codex sessions. Includes the controls that answer "your prompt did the work"
  (an ordinary question with no mention of `.env` leaks the same 23 of 24 on the
  realistic fixture), the reproduced miss mechanism, and the cost.

### Changed
- Open WebUI filter 0.1.9: the version had stayed at 0.1.8 while the file gained
  the tool-call argument escaping and moved its floor to `privaite>=0.4.1`, so
  an installed copy could not be told apart from the current one.
- The comparison page gains a guard-model section (Llama Guard,
  gpt-oss-safeguard, Shieldstral): document-level verdicts carry no spans, so
  they cannot replace or restore values, and a model-judged policy is
  probabilistic where PrivAiTe's policy layer is deterministic; guard models
  in turn cover semantic moderation PrivAiTe deliberately does not.
- The README leads with the measurement rather than with a per-type caveat: the
  irreversible handling of `CREDIT_CARD` and `SECRET` is unchanged and still
  stated, one paragraph lower.
- Gateway scrub cost is restated from the 0.4.1 benchmark run in the README and
  `docs/gateway.md`: the per-request maximum without the detection cache is 42 s
  (Claude Code) and 72 s (Codex), against a 1 to 3 s median with it on. The
  previous "roughly 50 seconds per turn" predated that run.

- Open WebUI filter 0.1.10: the declared floor moves to `privaite>=0.4.2`, which
  changes the file, so the version moves with it.

### Fixed
- `llms.txt` announced privaite 0.4.0 and filter 0.1.8 for a whole release. It
  states the current version in prose, which the concatenation check could not
  see, so a test now pins those strings to the shipped versions.

## [0.4.1] - 2026-07-28

### Fixed
- Restored values are escaped when they are spliced into tool-call arguments, so
  a value carrying a quote, a backslash or a newline keeps the arguments valid
  JSON on the streaming path and in both in-process integrations. The gateway
  already did this.
- Gateway restore handles a `type` key holding an object, which any tool schema
  with a property named `type` produces, and an unexpected restore failure
  returns the documented error shape.
- The gateway no longer forwards the client's `accept-encoding`, so responses
  always arrive in an encoding it can decode. Duplicate header names are relayed
  verbatim, and a header allowlist keeps `content-type` and `anthropic-version`.
- Anthropic blocks of an unrecognized type are scanned through the engine.
  Binary, opaque, thinking and encrypted payloads still travel byte for byte.
- `block_entities` covers the agent prompt fields the gateway relays verbatim
  (Responses `instructions`, Anthropic `system`), so a blocked type there stops
  the request.
- An unknown configuration key refuses startup and names the offending path.
- `/ready` reports the engine's real state, the container healthcheck targets it,
  and the image runs as a non-root user.
- Registered recognizers contribute their entity types to the Presidio allowlist,
  a named capture group in a custom pattern defines the span, and an unknown
  `device` value is refused at startup.

### Changed
- Detection recall is 84.9% span and 81.0% strict, up from 84.5 and 80.6, with
  false positives unchanged. The `light` preset moves to 36.5% for the same
  reason.
- Documentation now covers the measured limit of secret detection (a `KEY=value`
  assignment is detected on its own and missed once about one preceding line of
  log-shaped context is present, on every surface), the irreversible entity
  overrides in the shipped configs, and the gateway routes accepting requests
  without a PrivAiTe key by design.

## [0.4.0] - 2026-07-20

### Added
- Opt-in agent CLI gateway (`gateway.enabled`, off by default): native
  `/v1/messages` and `/v1/messages/count_tokens` routes for Claude Code
  (Anthropic Messages API, validated live end to end) and a `/v1/responses`
  route for Codex (OpenAI Responses API, beta). Requests are scrubbed at the
  same engine choke point, tool-call arguments included, the client's own auth
  is relayed verbatim (no key injected or validated on gateway routes), and
  responses are restored, streaming included. See `docs/gateway.md`.
- Opt-in engine-level detection cache (`pii.detection_cache`) for clients that
  resend conversation history every turn (agent CLIs). Caches merged detection
  spans per exact text leaf, keyed by salted hash, bounded LRU with TTL; stores
  span metadata only, never text or values. Output is byte-identical with the
  cache on or off, and `block_entities` still blocks on cache hits. Off by
  default; the README threat model documents what enabling it changes.

### Fixed
- LiteLLM guardrail parity with the 0.3.3 core fixes: the `/v1/completions`
  `prompt` (string and batch list shapes) and `suffix`, chat
  `prediction.content` and `web_search_options.user_location` are now scrubbed
  through the engine choke point (masking and `block_entities` included), and
  `message.refusal` and `message.audio.transcript` are restored in both the
  non-streaming and streaming paths. This fix also ships on its own as the
  0.3.4 patch, so it is not new to this release.
## [0.3.4] - 2026-07-19

### Fixed
- LiteLLM guardrail: the auxiliary request fields the core proxy already
  scrubs were forwarded raw by the guardrail, and a `/v1/completions` request
  reached the model with its whole `prompt` intact (live-reachable through
  LiteLLM text completions, whose pre-call hook received the top-level
  `prompt` and returned early). The guardrail now scrubs the completions
  `prompt` (string and batch list-of-strings shapes; tokenized integer-array
  prompts stay unscanned as documented) and `suffix`, chat `prediction.content`
  (string or content-part list) and `web_search_options.user_location` through
  the same engine choke point, so masking and the `block_entities` gate apply,
  and the proxy's shallow body snapshot is overwritten for the top-level
  string fields. A request whose only user text sits in these fields no longer
  bypasses the pre-call scan.
- LiteLLM guardrail restore parity with the core: `message.refusal` and
  `message.audio.transcript` are now restored in both the non-streaming and
  streaming paths.

## [0.3.3] - 2026-07-19

### Fixed
- Text-bearing request fields outside `messages` are now scrubbed before
  forwarding: chat `prediction.content` (OpenAI predicted outputs carry the
  client's current document), `web_search_options.user_location`, and the
  `/v1/completions` `suffix`. They previously rode the request passthrough to
  the provider unscrubbed. Request inputs only, so they scrub without a restore
  path, and the `block_entities` gate now applies to them.
- Startup refuses a `block_entities` policy that lists a type no enabled
  detector can emit, instead of leaving a gate that silently never fires. The
  check is conservative: it never rejects a configuration whose producible set
  cannot be determined.
- Custom patterns are no longer filtered out by the Presidio entity allowlist
  under the `onnx`/`max` presets, so a configured `custom_patterns` entity now
  actually fires.
- `message.refusal` and `message.audio.transcript` are restored on the response,
  in both the non-streaming and streaming paths (previously placeholders reached
  the client).
- Anonymization fails closed if a unique fake cannot be generated after repeated
  collisions, rather than returning a value that could cross-restore.
- The pip quickstart in the README launched the server without exporting
  `PRIVAITE_API_KEYS`, so every request hit the fail-closed 401. The variable is
  now on the launch line.

### Changed
- Documentation and discovery assets refreshed: `llms.txt`/`llms-full.txt`
  regenerated for the current release, per-page metadata and a sitemap on the
  docs site, and read-only default token scope plus SHA-pinned actions in the
  CI and publish workflows.

## [0.3.2] - 2026-07-17

### Fixed
- ONNX detector no longer selects the CoreML execution provider under
  `device: "auto"` on Apple Silicon. CoreML ran only a fraction of the model
  graph, was slower than CPU at every input size, and grew memory per input
  shape until the host process was killed. Because the Open WebUI filter and the
  LiteLLM guardrail run the engine in-process, a large request could take the
  host down with it. `auto` now uses CPU (or CUDA when present); `device: "coreml"`
  stays available as an explicit opt-in.

### Changed
- ONNX detector scans long inputs through overlapping token windows instead of a
  single full-sequence pass. Large tool outputs and file contents that used to
  run multi-minute inferences at multi-GB peak memory (or were silently truncated
  past 128k tokens) are now scanned whole in bounded time and memory. Detection
  spans are identical to full inference on the benchmark corpus, so recall is
  unchanged.

## [0.3.1] - 2026-07-10

### Security
- Dependency floors raised past known CVE fixes (litellm 1.84, pydantic 2.4,
  transformers 5.3, torch 2.6, setuptools 78.1.1); the Docker base image is
  digest-pinned, with Dependabot keeping the digest and floors current.
- **Failure logs can no longer serialize PII from detector/anonymizer exceptions.**
  Unexpected PII-processing and streaming failures now cross request boundaries as
  a safe, non-chained error; the proxy's JSON and text formatters omit exception
  tracebacks, which third-party libraries may populate with inspected text.
- Built-in Hugging Face detector models are pinned to immutable commits by
  default, including the Docker image pre-download path. Fresh installs no
  longer follow a mutable model branch unless the operator explicitly sets
  `revision: null` or another ref.

### Fixed
- Open WebUI now restores placeholders inside structured `function_call`
  output-item arguments, not only chat-shaped tool calls and text parts.

### Changed
- CI and the PyPI publish workflow enforce `ruff format --check`; the repository
  is normalized with the pinned Ruff formatter version.

## [0.3.0] - 2026-07-09

### Added
- Official Docker image at `ghcr.io/crp4222/privaite` (multi-arch, detection model
  baked in, runs offline). Drop-in for OpenAI:
  `docker run -e PRIVAITE_API_KEYS=change-me -e OPENAI_API_KEY=sk-... ghcr.io/crp4222/privaite`.

### Changed
- Internal refactoring for a cleaner, less-duplicated codebase. No change in
  behavior or detection.
- Clearer Open WebUI / Docker connection docs (which URL, which key).

## [0.2.13] - 2026-07-03

### Added
- **Dry-run inspection endpoint** `POST /v1/pii/inspect` (off by default,
  `pii.inspect.enabled: true` to expose it): submit a text, get back the
  detections (type, exact span, score, source detector), the anonymized preview
  exactly as the provider would have received it, and `would_block` (the types
  your `block_entities` policy would reject). Nothing is forwarded to any
  provider, nothing is logged, nothing is counted in `/stats`, and no mapping
  outlives the request. The caller already knows the text it submitted, so
  returning its own detections leaks nothing. Built for "how do I check what
  was redacted?" (asked in discussion #1). README gained a "Verify what gets
  redacted" section covering this and the `deanonymization.enabled: false`
  recipe.

### Changed
- The chat, completions and embeddings endpoints now share one request
  pipeline (`privaite/api/pipeline.py`) for the policy-sensitive plumbing:
  model validation, the fail-closed anonymization error policy, provider error
  mapping, SSE headers and response serialization. Previously three
  near-identical copies; a security fix could land in one and miss the others
  (the bare-string content scan fix had to be applied more than once). No
  behavior change: the whole pre-existing test suite passes unchanged.

## [0.2.12] - 2026-07-03

### Added
- **`max` preset**: the onnx suite (Presidio + `openai/privacy-filter`) plus a
  GLiNER detector (`urchade/gliner_multi_pii-v1`), an independent PII model trained
  on non-AI4Privacy data. It raises out-of-distribution recall at the cost of more
  false positives, so it is opt-in and never the default (`onnx` stays the default).
  GLiNER needs torch + the `gliner` package, installed via the new optional extra
  `pip install 'privaite[gliner]'`; with the `max` preset selected but the package
  absent, startup fails loudly with an install hint (fail closed, not silently off).
  Rationale and off-distribution numbers: the OOD cross-check in privaite-bench
  (`OOD_COMPARISON.md`).

## [0.2.11] - 2026-07-02

### Fixed
- Second audit pass (a three-reviewer sweep of the 0.2.10 diff, the integrations,
  and the never-reviewed periphery), everything below reproduced by a test that
  failed first:
  - **Fuzzy de-anonymization no longer injects the wrong person's PII.** A
    hallucinated placeholder with attached punctuation or different case
    (`<PERSON_3>,`, `<person_3>`) defeated the guard and got rewritten to a known
    identity; matching now runs on the punctuation-stripped core, skips any
    angle-bracketed token case-insensitively, replaces only the core (so
    punctuation survives), and compares whitespace-normalized so a fake re-typed
    across a newline is still caught.
  - **Bare strings inside a `content` list are scanned.** `content: ["...", 42]`
    (and a mixed `/v1/completions` prompt list) forwarded the string raw and
    skipped the block gate.
  - **LiteLLM guardrail: Responses `input` is scanned item by item.** A mixed
    agentic turn (a message plus a `function_call_output`/tool call, or a bare
    string) bypassed detection AND the block gate; every text-bearing item is now
    scanned through the shared mapping.
  - **Open WebUI filter: original PII no longer lingers in metadata.** `outlet`
    now pops the reversible map (Open WebUI may persist message metadata), the map
    is only stashed when restore is on, an incoming client-supplied map is
    cleared, and list/reasoning assistant content is restored. Adds a build lock
    and moves the first-use spaCy download off the event loop.
  - **`huggingface_hub` import floor.** `EntryNotFoundError` was imported from a
    module that only exists in hub >= 0.25 while pyproject floors at 0.23;
    imported defensively now.
  - **Streaming: a fully-held-back chunk carrying `logprobs`, `refusal`, `usage`
    or any other payload is emitted** instead of suppressed; a nonstandard finish
    chunk with `function: None` no longer crashes the stream.
  - **Auth runs before the size limiter**, so an unauthenticated request is
    rejected on its headers instead of having up to `max_request_bytes` buffered
    first.
  - **Contextual name recognizer** keeps exact offsets on names with a double
    space (the last character used to leak); **French date recognizer** no longer
    matches month-prefix words (`3 maintenant`, `1 marseillais`).
  - **Config loader** raises on an explicitly requested missing file (a `--config`
    typo used to start an empty proxy) and never echoes interpolated secret values
    in a validation error.
  - `pii.enabled: true` with zero detectors is refused at startup; the numeric
    tool-argument scan is gated to values with >= 7 digits (ordinary
    counts/years/coordinates keep their type); `intersection` merge requires two
    DIFFERENT detectors to confirm a span; non-streaming chat restores the
    reasoning trace; batch `/v1/embeddings` counts one request in `/stats`, not
    one per item.

### Changed
- The example config (also the Docker fallback) comments out the cloud providers
  that need `${OPENAI_API_KEY}`, so the image boots as-is; `docker-compose` marks
  `.env` optional; `CONTRIBUTING.md` code-style matches the actual (commented,
  docstringed) codebase; the FastAPI app version tracks `__version__`.

## [0.2.10] - 2026-07-02

### Fixed
- Fuzzy de-anonymization no longer destroys formatting: it used to rebuild the
  text with split()/join(), flattening every newline and indent (markdown,
  code blocks) in any response that carried PII. Replacements are now spliced
  into the original string. Fuzzy is also **opt-in now** (`fuzzy_matching`
  defaults to `false`): it carries a small wrong-substitution risk on lookalike
  spans, and it never rewrites an intact-but-unknown placeholder.
- `/v1/completions` actually works against real providers: it is backed by
  litellm's text-completion path now, so the response is a real
  `text_completion` (not a chat object), PII restoration in `choices[].text`
  works, batched list prompts stay a list of strings, and streaming emits
  `text_completion` chunks instead of chat deltas.
- The Docker `CMD` pointed at `config/privaite.yaml`, which is gitignored (it
  is the operator's own file), so an image built from a fresh clone had no
  config at all; it now falls back to the tracked example config. The
  Dockerfile pre-download step also stops passing `trust_remote_code=True`.
- Overlapping detections merge to the UNION of their spans: a short
  higher-scored entity overlapping a longer one used to discard the longer one
  entirely, forwarding its uncovered remainder raw to the provider.
  `presidio_priority` overlap resolution falls back to score when neither
  entity is from Presidio.
- `mask` and `redact` are truly irreversible: the original no longer enters the
  reversible map, so it is never restored into responses, and two values that
  mask to the same string ("****") can no longer cross-restore each other's
  PII. Detections are still counted in `/stats`.
- Numeric JSON values in tool-call arguments are scanned: a card number sent as
  a bare JSON number used to bypass both masking and the `block_entities` gate.
  On a hit the leaf becomes the masked string; ordinary numbers keep their type.
- Streaming rework: provider chunks are forwarded as-is (ids, `usage`,
  `logprobs` and the real finish chunk survive; no more synthetic duplicate
  finish chunk), `n>1` choices keep independent restore buffers instead of
  sharing one, the initial role chunk is no longer swallowed, held-back text is
  flushed even when the stream ends without a `finish_reason`, and
  `reasoning_content` / `reasoning` deltas are restored too.
- Misconfigurations fail fast at startup instead of silently or per-request:
  `merge_strategy: intersection` with fewer than 2 enabled detectors (it would
  detect nothing and forward all PII raw), duplicate provider `model_name`
  aliases (last-wins overwrite), and a Presidio language with no spaCy model
  mapping (it used to pass init and then crash every request).
- JSON log lines are built with `json.dumps`, so messages containing quotes or
  newlines no longer produce invalid JSON.
- `/stats` now counts detections from `/v1/completions` and `/v1/embeddings`,
  not just chat.
- The `bert_ner` and `mlmodel` detectors (presets `standard`/`full`) scan long
  inputs in overlapping windows: the HF pipeline silently truncates at
  model_max_length (~512 tokens), so PII past that point was invisible to
  them. The default `onnx` path was not affected (128k max_length).
- `download_onnx_model` no longer requires a `.onnx_data` side file: a variant
  packed into a single `.onnx` used to fail at startup.

### Security
- Hugging Face model loading defaults to `trust_remote_code=False` (the default
  model needs no repo code; the flag was granting an unused permission) and
  every detector accepts a `revision` pin so a rewritten model repo cannot
  silently swap the weights this proxy runs.

### Removed
- Dead config knobs that were accepted but never read: `logging.redact_fields`
  and `anonymization.entity_overrides.<TYPE>.domain_preserve`.

### Documentation
- The README now states exactly which request fields are NOT scanned
  (`messages[].name`, `user`, `metadata`, tool definitions, JSON object keys)
  and that `passthrough.*` also bypasses `block_entities`.

## [0.2.9] - 2026-07-01

### Added
- `pii.block_entities`: a hard policy gate. A request containing any listed PII
  type is rejected with HTTP 400 and nothing is forwarded, instead of being
  pseudonymized. Empty by default, so the default behavior (mask everything with a
  placeholder) is unchanged. Enforced at the single anonymization choke point, so
  it covers message content, multimodal text, and tool-call arguments alike. The
  error names the blocked type(s) only, never the value.

### Changed
- Anonymization now fails closed. On an internal detector error the request is
  blocked by default (`pii.on_error`, default `block`) rather than forwarded with
  raw PII; set `on_error: allow` to opt back into the old behavior.
- The `light` preset runs full Presidio instead of a pinned entity allowlist that
  quietly capped its recall.
- The engine warms its detectors once at startup, so the first real request does
  not pay the model cold-start cost.

### Integrations
- LiteLLM custom guardrail (`integrations/litellm/privaite_guardrail.py`). Mount it
  next to a LiteLLM `config.yaml` and reference it by dot-notation to anonymize
  requests and restore responses inline, including inside tool-call arguments and
  the legacy `function_call`, the Responses API, and streaming, which LiteLLM's
  built-in Presidio guardrail does not cover. This is a repo integration, not part
  of the PyPI package; it imports `privaite` at run time.
- Both the LiteLLM guardrail and the Open WebUI filter honor `block_entities`: they
  reject blocked types (an HTTP 400 for the guardrail, a raised error for the
  filter) and fail closed if the installed `privaite` is too old to enforce the
  gate, rather than silently forward the PII.

### Documentation
- The benchmark now cites its data source: the open AI4Privacy `pii-masking-200k`
  dataset on Hugging Face. Because that dataset declares no explicit license, the
  benchmark repo commits only derived labels and fetches the source text on demand.

## [0.2.8] - 2026-06-27

### Documentation
- Discoverability pass. Richer PyPI keywords and Trove classifiers, a clearer
  one-line description, and a canonical positioning tagline in the README. Added
  `docs/comparison.md` (PrivAiTe vs Presidio, LLM Guard, and LiteLLM PII masking)
  and an `llms.txt` so AI coding assistants can identify PrivAiTe as a
  PII-redaction proxy. GitHub repository topics and About were refreshed too.

## [0.2.7] - 2026-06-27

### Fixed
- Anonymization method dispatch now lives in one place (the `Anonymizer`); the
  faker generator is reduced to producing fake values only, removing a duplicate
  implementation that had drifted. Two entity-override bugs are fixed as a result:
  an override with method `placeholder` now produces a numbered `<TYPE_n>`
  placeholder (it previously emitted a literal `<REDACTED>`), and an override with
  method `fake_replacement` now generates a fake for the correct entity type (it
  previously used a generic fallback). An override with method `redact` now
  returns `[TYPE]`, matching the global redact output and the documentation (it
  previously returned `[REDACTED]`).

## [0.2.6] - 2026-06-27

### Changed
- The default anonymization method is now `placeholder` (`<PERSON_1>`), matching
  the example config, the README, and the threat model, which already documented
  placeholders as the default. Previously the schema default was
  `fake_replacement`, so running with no config file produced realistic fake
  names instead of placeholders. Set `method: "fake_replacement"` to keep fakes.
- The default Presidio language order is now `["fr", "en"]`, matching the example
  config and README.

### Documentation
- Reworked the README hero around the differentiator (tool-call arguments,
  multimodal, agent egress, zero telemetry) with legally careful wording
  (pseudonymization, not anonymization). Corrected the "What's NOT detected by
  default" section: the default `onnx` preset does detect personal addresses and
  URLs; only Presidio's broad LOCATION/URL recognizers stay off. Added website
  landing copy under `docs/landing.md`, fixed stale latency and language figures,
  and noted the removed `[onnx]` extra in the 0.2.3 changelog entry.

### Tooling
- CI typecheck now also covers the Open WebUI filter; the publish workflow runs
  lint, typecheck, and tests before publishing; a `.dockerignore` keeps a local
  config out of the image. Added a test that locks the Open WebUI filter contract.

## [0.2.5] - 2026-06-25

### Fixed
- The ONNX preset was discarding two entity types the Privacy Filter model
  actually detects: `private_address` and `private_url`. They are now mapped to
  `LOCATION` and `URL`, so addresses and personal URLs are anonymized instead of
  silently reaching the provider. On the comparative benchmark this lifts ONNX
  recall from about 72% to 84%.
- Streaming responses now de-anonymize tool-call argument deltas and the legacy
  `function_call` arguments, not just message content. Each tool call buffers its
  own argument stream, so a placeholder split across deltas is still restored.
  Previously a streamed tool call delivered placeholders to the client instead of
  the real values; non-streaming already restored them.

## [0.2.4] - 2026-06-23

### Changed
- The default preset is now `onnx` (the full Privacy Filter suite). A fresh
  install detects everything including secrets and passwords out of the box, not
  only the classic regex and spaCy PII. Set `preset: "light"` for the fast, zero
  false-positive Presidio-only path, or `preset: null` to drive detectors by hand.
- `onnxruntime`, `transformers`, and `huggingface_hub` are now core dependencies
  (previously the separate `onnx` extra), so the default preset works without an
  extra install. The Privacy Filter model is downloaded on first start. The `ml`
  extra now only adds torch, used by the `standard` and `full` BERT presets.

### Documentation
- README leads with the ONNX full suite as the default preset and emphasizes that
  secrets and passwords are detected by default.

### Integrations
- The Open WebUI filter now defaults to the `onnx` preset as well (valve), and
  requires `privaite>=0.2.4` so the ONNX dependencies are present. Bumped to v0.1.2.
- The Docker image pre-downloads the ONNX Privacy Filter model at build time, so a
  container with the default preset starts fast and works offline from the first
  request instead of downloading the model on startup.

## [0.2.3] - 2026-06-22

### Security
- The request size limit is now enforced at the body-stream level, not only from
  the `Content-Length` header. The middleware was rewritten as pure ASGI and
  counts bytes as they arrive, so a chunked request that omits `Content-Length`
  can no longer exceed `server.max_request_bytes`. The buffered body is replayed
  to the application unchanged.
- The size check runs against the projected total before each chunk is appended,
  so a single oversized frame is rejected without first being copied into the
  buffer.

### Added
- Open WebUI Filter Function (`integrations/openwebui/`) that runs the engine
  in-process, with a setup guide. The response path now also restores PII inside
  the legacy `function_call`, matching the proxy.
- `onnx` install extra (`pip install "privaite[onnx]"`, removed in 0.2.4 once
  onnxruntime, transformers and huggingface_hub became core dependencies, so no
  extra is needed anymore). The ONNX privacy-filter preset runs on onnxruntime
  plus the transformers tokenizer and does not need torch, so this extra installed
  that path without pulling torch or scipy. The `ml` extra (BERT NER) still
  installs torch, as that detector requires it.

### Tooling
- CI lint now runs `ruff check .` over the whole tree, including `integrations/`,
  rather than only `privaite/` and `tests/`.

## [0.2.2] - 2026-06-22

### Security
- The `/stats` tracker now stores a salted hash of the session identifier instead
  of the raw value. The identifier can be derived from the `Authorization` header
  (an API key), so it is no longer kept in memory or exposed by `/stats`.

## [0.2.1] - 2026-06-22

### Added
- Request bodies are capped at `server.max_request_bytes` (10 MB by default);
  larger requests are rejected with HTTP 413.

### Documentation
- Added an explicit **Threat model** section to the README (what the proxy does
  and does not protect against) so the privacy posture is not over-claimed.

## [0.2.0] - 2026-06-22

### Added
- PII anonymization for structured request content, not just plain-string
  message bodies:
  - **Multimodal content** (`content` as a list of parts): text parts are
    anonymized, other parts (images, audio) are passed through untouched.
  - **Tool / function calls** (`tool_calls[].function.arguments` and the legacy
    `function_call.arguments`): the argument string is parsed as JSON and scrubbed
    value by value (object keys and the function name are left intact), or treated
    as free text when it is not valid JSON. Nested objects and arrays are walked
    recursively.
  - **`/v1/completions` list prompts**: a `prompt` given as a list of strings is
    anonymized element by element under a single mapping.
- Response-side de-anonymization of tool-call arguments for non-streaming
  `/v1/chat/completions`, so the client receives the real values back.
- The `pii.passthrough.tool_calls` config flag is now honored. It was previously
  declared in the schema but never read. Default is `false` (anonymize); set it
  to `true` to forward tool-call arguments unchanged.
- `pii.strict` option (default `false`): when enabled, a request whose content
  cannot be inspected (a shape that is neither text nor a known media part) is
  rejected with HTTP 400 instead of being forwarded to the provider.

### Security
- Authentication now fails closed: when `auth.enabled` is set but no API keys are
  configured (`PRIVAITE_API_KEYS` empty), requests are rejected with 401 instead
  of being forwarded to the provider. A startup warning is logged for this case.

### Fixed
- `--config <path>` is now honored by the app factory: the CLI exports
  `PRIVAITE_CONFIG_PATH`, and config loading uses `load_dotenv(override=False)` so
  the explicit value is not overwritten by a `.env`. Providers, PII, and auth now
  load from the file passed on the command line (previously only host and port
  were honored).
- Contextual name detection now normalizes typographic apostrophes (U+2019 and
  similar) before matching intro patterns, so "je m'appelle X" typed with smart
  quotes (the default on macOS/iOS and most chat UIs) is recognized. Previously a
  lowercase name after a curly-apostrophe intro leaked to the provider.
- Integration test fixture used `asyncio.get_event_loop()`, which raises on
  Python 3.12+. It now creates a dedicated event loop, so the suite is green on
  Python 3.11 through 3.13.

### Tooling
- The type checker is now clean (`mypy privaite/` passes) and runs in CI as a
  dedicated job. CI also installs the spaCy models so the integration suite runs.
- Added endpoint tests (chat, completions, embeddings) and streaming-handler
  tests covering the request and response anonymization paths.

### Notes
- Streaming responses do not yet de-anonymize tool-call argument deltas. This is
  restoration only and never leaks: request-side anonymization runs before the
  stream opens, so placeholders, not real PII, are what may appear in streamed
  tool calls.

## [0.1.0]

### Added
- Initial release: OpenAI-compatible proxy with local PII detection (Presidio +
  optional ONNX privacy filter), placeholder/fake/redact/mask anonymization,
  reversible de-anonymization, streaming support, and YAML configuration.
