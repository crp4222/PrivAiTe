# PrivAiTe — guide for AI agents (read this before changing code)

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
   new field/path that carries user text, route it through the engine — never
   forward it directly.
3. **Restore parity.** Whatever is anonymized must be restored on the way back, in
   **both streaming and non-streaming**: `content`, `tool_calls[].function.arguments`,
   legacy `function_call.arguments`, and `reasoning_content`/`reasoning`.
4. **`mask` and `redact` are irreversible.** They must NOT enter the reversible
   mapping (`mapping.note()`, not `mapping.add()`). Two values that mask to the same
   string must never cross-restore. Only `placeholder` and `fake_replacement` are
   reversible.
5. **Never log or echo PII.** Errors name entity **types**, never values
   (`PIIBlockedError`, `UnsupportedContentError`). No logger call may emit message
   content or entity values. The reversible map is per-request, in-memory only.
6. **Startup fails fast on unsafe config**, it never degrades silently: `pii.enabled`
   with 0 detectors, `merge_strategy: intersection` with <2 detectors, a Presidio
   language with no spaCy model, and duplicate provider `model_name` aliases all
   raise at boot.
7. **Config defaults are the safety posture — do not flip them casually.**
   `preset: onnx` (~84.5% recall), `on_error: block`, `deanonymization.fuzzy_matching: false`
   (fuzzy can mis-substitute), detector `trust_remote_code: false`, `block_entities: []`.

## What is and isn't scanned (know the surface)

Scanned: `messages[].content` (string, multimodal text parts, or bare strings in a
list); `tool_calls`/`function_call` arguments parsed as JSON and scrubbed value by
value **including numeric leaves with >=7 digits** (a card number sent as a bare
number is caught); `/v1/completions` `prompt`; `/v1/embeddings` `input`.

NOT scanned (documented limitation, keep it documented if you change it):
`messages[].name`, top-level `user`/`metadata`, `tools`/`functions` definitions,
JSON object keys, and tokenized (integer-array) inputs.

## Streaming handler (`privaite/streaming/handler.py`)

Forward the provider's own chunks (preserve `id`, `usage`, `logprobs`, and the real
finish chunk — do not synthesize a duplicate finish). One restore buffer **per
choice index** (n>1 must not share). Flush held-back text onto the finish chunk and
again after the stream ends without a `finish_reason`. Never suppress a chunk that
carries any payload other than fully-held-back content.

## Before you push or release

- Run in the repo venv (`.venv`): `pytest`, `ruff check`, `ruff format --check`,
  and **`mypy privaite/ integrations/openwebui/privaite_filter.py integrations/litellm/privaite_guardrail.py`**.
  The publish workflow type-checks the integrations even though the push CI does
  not — skipping them once broke a release.
- Version: bump **both** `pyproject.toml` and `privaite/__init__.py`; date the
  `CHANGELOG.md` section; bump the integration pins (`privaite>=X`).
- Benchmark: if you touched detection, re-run `privaite-bench`
  (`python -m solutions.compare` from that repo root) and update `COMPARISON.md` +
  the README numbers. The published numbers must match the shipped code.
- Release: `gh release create vX.Y.Z` triggers `publish.yml` → PyPI (trusted
  publisher; its Environment field must stay empty). A PyPI version can never be
  reused, so verify green CI first.
- Commit as the real git user, not claude

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
