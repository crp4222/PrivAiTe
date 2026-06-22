# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[Semantic Versioning](https://semver.org/).

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
