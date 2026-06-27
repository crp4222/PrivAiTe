# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

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
