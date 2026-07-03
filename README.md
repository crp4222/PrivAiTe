# PrivAiTe

[![CI](https://github.com/crp4222/PrivAiTe/actions/workflows/ci.yml/badge.svg)](https://github.com/crp4222/PrivAiTe/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-green.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/privaite.svg)](https://pypi.org/project/privaite/)

**A drop-in, self-hosted LLM proxy that reversibly redacts PII before it reaches the provider, including inside tool-call arguments and multimodal content, with zero telemetry.**

<p align="center">
  <img src="docs/demo.gif" alt="PrivAiTe demo: a tool call carrying an email, name and credit card has each value replaced with a placeholder before it reaches the LLM provider, with the JSON keys left intact, then de-anonymized in the reply" width="820">
</p>

Keep personal data out of your LLM calls. PrivAiTe is a local proxy that sits between your app and the model provider. It finds names, emails, phone numbers, cards, IBANs, secrets and more, swaps them for stand-ins before anything leaves your machine, and puts the real values back in the reply. It does this across message text, **tool-call arguments, and multimodal content**, which is where most tools stop looking. Detection runs on your machine and nothing phones home. By default it runs the full ONNX suite, so it also catches **secrets and passwords**, not just the easy regex entities. Point any OpenAI-compatible client at it.

```
You type: "Je m'appelle Marie Dupont, email marie@acme.com"
LLM sees: "Je m'appelle <PERSON_1>, email <EMAIL_ADDRESS_1>"
LLM says: "Bonjour <PERSON_1>, votre email <EMAIL_ADDRESS_1> est noté."
You  see: "Bonjour Marie Dupont, votre email marie@acme.com est noté."
```

This is local pseudonymization, not anonymization, and detection is best-effort rather than a guarantee. You remain the data controller. The [Threat model](#threat-model) spells out exactly what it protects against and what it does not.

## How detection works

PrivAiTe uses two detection engines that can run together or separately:

### Presidio (Microsoft): regex + spaCy NER

The default engine. Handles structured PII through pattern matching and basic NER.

| What it detects | How |
|---|---|
| Emails | Regex |
| Phone numbers | Regex + international format validation |
| Credit cards | Regex + Luhn checksum |
| IBAN | Regex + checksum validation |
| IP addresses | Regex |
| US SSN | Regex + format validation |
| Person names (capitalized, 2+ words) | spaCy NER, only kept if all words are capitalized |
| Person names (lowercase or single word) | Contextual regex, only after "je m'appelle X", "my name is X", "ich heiße X", "Nom: X", etc. |
| Dates (FR/DE) | Custom regex, "15 mars 1987", "3. März 1990" |

Presidio is fast (tens of ms/request) and produces very few false positives on code, news articles, and technical text. The tradeoff: it misses names that spaCy doesn't recognize (unusual names, single-word names without context) and doesn't detect secrets/passwords.

### OpenAI Privacy Filter: contextual ML model

[OpenAI's open-source PII model](https://openai.com/index/introducing-openai-privacy-filter/) (1.5B params, 50M active, Apache 2.0). Runs locally via ONNX Runtime (~800MB, no PyTorch needed).

| What it adds over Presidio | How |
|---|---|
| Person names (any format, any case) | ML NER, understands context, not just capitalization |
| Passwords and secrets | Detects "SuperSecret2024!", API keys like "sk-proj-..." |
| Account numbers | Detects bank account numbers, policy numbers, etc. |
| Dates (all languages) | ML-based, not limited to FR/DE regex |

The Privacy Filter is slower (~400ms/request) and occasionally flags technical identifiers as account numbers (e.g., "CMD-2024-98765"). It runs as a second pass alongside Presidio, which handles the regex-based entities while the Privacy Filter handles contextual NER.

### Why two engines?

Neither is perfect alone:
- **Presidio alone** misses names that spaCy doesn't recognize, and can't detect secrets. But it has very few false positives.
- **Privacy Filter alone** misses some names in credit/list formats, and doesn't have regex validators for IBAN/credit card checksums.
- **Both together** cover each other's blind spots. Presidio handles structured formats with validation, the Privacy Filter handles context-dependent PII.

## Presets

`onnx` is the default. It runs the full suite and detects everything, including secrets and passwords. `light` is a faster, Presidio-only option with very few false positives, for when you only care about classic PII.

| Preset | What runs | Recall\* | False positives | Latency | Secrets |
|--------|-----------|----------|-----------------|---------|---------|
| `onnx` (default) | Presidio + Privacy Filter | **84.5%** | 2 / 14 | ~0.5s | **yes** |
| `light` | Presidio only | 62.4% | 3 / 14 | ~60ms | no |
| `max` | onnx + GLiNER | higher OOD\*\* | more | ~0.7s | **yes** |

\*Recall on the independent 120-document AI4Privacy benchmark (span-level; strict token-level ~80% / ~58%). Latency is hardware-dependent. See [Benchmark](#benchmark).

\*\*`max` adds GLiNER, a PII model trained on data independent of AI4Privacy. On an out-of-distribution corpus it raises recall from ~84% to ~89% where the default already generalizes well, at the cost of more false positives and a torch dependency. It is opt-in; `onnx` stays the default. Numbers: the OOD cross-check ([`OOD_COMPARISON.md`](https://github.com/crp4222/privaite-bench/blob/main/OOD_COMPARISON.md)).

```yaml
pii:
  preset: "onnx"    # Default. Detects everything including secrets. Downloads the model on first run.
  # preset: "light" # Faster, Presidio-only, classic PII only.
  # preset: "max"   # onnx + GLiNER: higher out-of-distribution recall. Needs: pip install 'privaite[gliner]'
```

> **Footgun:** do not pin `detectors.presidio.entities` to a short allowlist on the `light` path. It restricts detection to only those types and roughly halves recall (to ~35%). Leave `entities` unset; the proxy logs a warning at startup if it detects a low-recall configuration.

The default install already includes onnxruntime and the tokenizer, so the `onnx` preset works out of the box. The model is downloaded the first time the proxy starts. Two optional extras add torch: `ml` (the `standard`/`full` BERT presets) and `gliner` (the `max` preset). With `max` selected but `privaite[gliner]` not installed, the proxy fails to start with an install hint rather than running with detection silently reduced.

**When to use `onnx` (default):** You want maximum coverage. Secrets, passwords, API keys, account numbers, unusual names. Accept occasional false positives on technical identifiers.

**When to use `light`:** You want zero disruption and the fastest path. Code, news, business text all pass through untouched. Only clearly identifiable PII (names, emails, phones, cards, IBANs) is anonymized.

Two other presets exist (`standard`, `full`) but are less useful in practice: they add BERT NER, which does not improve much over spaCy and pulls in PyTorch.

## Verify what gets redacted

Do not trust the proxy blindly: check it on your own data. Two built-in ways, both local.

**1. See exactly what the provider receives.** Turn de-anonymization off and send test traffic:

```yaml
pii:
  deanonymization:
    enabled: false
```

The response comes back without the real values restored, so what you read is literally what left for the provider (`<PERSON_1>`, `<EMAIL_ADDRESS_1>`, ...). Diff it against your input: every placeholder is a catch, every real value still visible is a miss. Works for tool-call arguments and streaming too.

**2. Dry-run inspection endpoint.** Enable it explicitly (off by default), then submit text and get the detections back. Nothing is forwarded to any provider, nothing is logged, nothing is counted in `/stats`:

```yaml
pii:
  inspect:
    enabled: true
```

```bash
curl -s localhost:8400/v1/pii/inspect -H 'Content-Type: application/json' \
  -d '{"text": "Contact Marie Dupont at marie@acme.com"}'
```

```json
{
  "language": "en",
  "entities": [
    {"type": "PERSON", "text": "Marie Dupont", "start": 8, "end": 20,
     "score": 0.99, "source": "onnx", "replacement": "<PERSON_1>"},
    {"type": "EMAIL_ADDRESS", "text": "marie@acme.com", "start": 24, "end": 38,
     "score": 1.0, "source": "presidio", "replacement": "<EMAIL_ADDRESS_1>"}
  ],
  "anonymized": "Contact <PERSON_1> at <EMAIL_ADDRESS_1>",
  "would_block": []
}
```

`anonymized` is the exact string the provider would have seen, and `would_block` lists any types your `block_entities` policy would have rejected outright. There is deliberately no admin view of live traffic: the reversible map is per-request and in-memory only, never logged or persisted.

## Benchmark

Measured on 120 real documents from the open [AI4Privacy `pii-masking-200k`](https://huggingface.co/datasets/ai4privacy/pii-masking-200k) dataset on Hugging Face (458 PII items, labeled by 10 independent auditor agents and cross-checked against the dataset's own sensitive mask) across DE, EN, FR, IT, plus 14 clean documents for false positives. The dataset declares no explicit license, so the [benchmark repo](https://github.com/crp4222/privaite-bench) commits only derived labels and fetches the source text on demand.

| Solution | Recall (span) | Recall (strict) | False positives | Tool-call protection |
|---|---|---|---|---|
| `onnx` (default) | **84.5%** | **80.6%** | 2 / 14 | **100%** |
| `light` (full Presidio) | 62.4% | 57.9% | 3 / 14 | **100%** |
| Presidio baseline (flat-text) | 70.3% | 65.3% | 3 / 14 | 0.6% |

Two recall columns: **span** credits a multi-token PII span as caught when its exact full string disappears (an upper bound); **strict** requires every token of the span to be removed. The Presidio baseline is the common flat-text approach (the engine behind most drop-in PII proxies); by design it does not touch tool-call arguments or multimodal content, which is the gap PrivAiTe closes — hence 100% tool-call protection vs 0.6%. Read the structured columns as "structured-aware vs the flat-text approach", not "vs every competitor".

Per-language and per-entity tables, the methodology, and how to reproduce: [privaite-bench](https://github.com/crp4222/privaite-bench). Head-to-head feature comparison with Presidio, LLM Guard, and LiteLLM PII masking: [docs/comparison.md](docs/comparison.md).

(An earlier, smaller curated set of 61 partly-synthetic documents scores higher — `light` ~97%, `onnx` ~100% — because that data is easier; the AI4Privacy figures above are the realistic, independent numbers.)

## What's NOT detected by default

The default `onnx` preset does detect personal addresses (as `LOCATION`) and personal URLs (as `URL`) through the Privacy Filter model, and replaces them. What stays off by default are Presidio's broad recognizers for those types, because they cause heavy false positives:

- **Generic place names (the Presidio LOCATION recognizer):** "Paris" or "London" on their own aren't PII, and spaCy flags ordinary words ("Kubernetes", "Saturday") as locations. The `onnx` preset keeps this recognizer off and relies on the model's context-aware address detection instead.
- **The Presidio URL regex:** it matches code like `logging.getLogger` because `.ge` is a valid TLD. The `onnx` preset keeps it off, and the model still catches genuine personal URLs.

On the `light` preset (Presidio only), addresses and URLs are not detected. Secrets and passwords are detected only by the `onnx` preset. Any recognizer can be turned on in the YAML config.

## Threat model

PrivAiTe performs **local pseudonymization**, not guaranteed anonymization. Detection runs on your machine; the real ↔ placeholder mapping lives in memory only for the duration of a request and is dropped afterwards.

**What it protects against:** the LLM provider storing, training on, or logging your raw PII. The provider receives placeholders (`<PERSON_1>`, …) for everything the detector catches, across message content, tool-call arguments, and multimodal text.

**What it does NOT protect against:**

- **PII the detector misses.** Detection is statistical and never 100% (see the [benchmark](https://github.com/crp4222/privaite-bench)). A name it doesn't recognize reaches the provider. The `onnx` preset has the best recall; treat the output as best-effort, not a guarantee.
- **Re-identification from context.** Even with names replaced, the surrounding text can stay identifying ("the CEO of `<ORG_1>` who resigned in March").
- **A compromised local machine.** The mapping and raw text live in local memory; this is not a defense against a local attacker.
- **The provider correlating** requests within a session.

For GDPR/HIPAA: treat this as pseudonymization + transfer minimization, not anonymization. If you need irreversible removal, use `method: "redact"` instead of `method: "placeholder"`.

## Alternatives

Keeping PII out of LLM calls is a crowded space, and PrivAiTe is not always the right pick. Based on each project's public docs as of June 2026:

- LiteLLM has a built-in Presidio guardrail, the natural choice if you already run the LiteLLM proxy and want PII handling inline (there are a few open bugs around scrubbing requests and responses).
- Managed/cloud options exist too, such as Microsoft PII Shield and [LangChain's gateway redaction](https://docs.langchain.com/langsmith/llm-gateway-redaction).

Where PrivAiTe differs: it anonymizes PII **inside tool-call arguments and multimodal content**, not just message text (LangChain's gateway docs, for instance, note that tool-call arguments are not scanned), it **restores** the original values in the response, and it ships a [reproducible benchmark](https://github.com/crp4222/privaite-bench). If your traffic is agentic or multimodal, that gap is the reason this exists.

## Quick start

### 1. Install

```bash
pip install -e .
python -m spacy download en_core_web_lg
python -m spacy download fr_core_news_md
```

The default `onnx` preset downloads its model the first time the proxy starts. Want the lighter, faster path with no model download? Set `preset: "light"` in your config.

### 2. Configure

```bash
cp .env.example .env
cp config/privaite.example.yaml config/privaite.yaml
```

Edit `.env` with your API keys and `config/privaite.yaml` with your LLM providers.

### 3. Run

```bash
python -m privaite

# Dev mode (auto-reload)
python -m privaite --reload
```

### 4. Connect

Point any OpenAI-compatible client to `http://localhost:8400/v1` with your proxy API key. Ready-to-run client snippets (curl, Python, Node) are in [`examples/`](examples/).

**OpenWebUI (Docker):** Admin → Settings → Connections → OpenAI API:
- URL: `http://host.docker.internal:8400/v1`
- Key: your `PRIVAITE_API_KEYS` value

If you would rather not run a separate proxy, there is also an in-process Open
WebUI filter (see [Open WebUI filter](#open-webui-filter) below).

## Docker

```bash
docker compose up -d
```

## Open WebUI filter

`integrations/openwebui/privaite_filter.py` is an Open WebUI Filter Function. It
runs the engine inside Open WebUI, so it anonymizes the outgoing request and
restores PII in the reply without a separate proxy. It covers message text,
tool-call arguments, and multimodal text.

To install it: Admin Panel → Functions → "+", paste the file, save, enable it,
then open its valves to pick the preset (`light` or `onnx`) and the languages.
The filter pulls Presidio and spaCy into Open WebUI and downloads the spaCy
models on first use, so the first request after enabling it can be slow. Setup
notes are in [`integrations/openwebui/README.md`](integrations/openwebui/README.md).

## LiteLLM guardrail

`integrations/litellm/privaite_guardrail.py` is a LiteLLM custom guardrail. If you
already run the LiteLLM proxy, mount it next to your `config.yaml` and reference it
by dot-notation to anonymize requests and restore responses inline, including
inside tool-call arguments, which LiteLLM's built-in Presidio guardrail does not
cover. Setup is in [`integrations/litellm/README.md`](integrations/litellm/README.md).

## Configuration

### LLM providers

Any [LiteLLM-supported provider](https://docs.litellm.ai/docs/providers) works:

```yaml
providers:
  - model_name: "gpt-4o"
    litellm_params:
      model: "openai/gpt-4o"
      api_key: "${OPENAI_API_KEY}"

  - model_name: "local-llama"
    litellm_params:
      model: "ollama/llama3.1"
      api_base: "http://localhost:11434"
```

### Anonymization method

```yaml
pii:
  anonymization:
    method: "placeholder"        # <PERSON_1>, <EMAIL_ADDRESS_1> (recommended)
    # method: "fake_replacement" # Realistic fakes via Faker (Jean → Michel)
    # method: "redact"           # [PERSON], [EMAIL_ADDRESS] (irreversible)
    # method: "mask"             # ******** (irreversible)
```

`redact` and `mask` are lossy on purpose: the original never enters the reversible map, so nothing is restored in responses for those types (and two values that mask to the same string can never cross-restore).

### Blocking specific PII types (hard policy gate)

By default every detected PII item is pseudonymized and the request goes through. If some PII types must **never** leave your network at all, even as a placeholder, list them under `block_entities`. A request containing any listed type is rejected with `400` and nothing is forwarded to the provider. The error names the type(s), never the value.

```yaml
pii:
  block_entities: []                     # default: block nothing, mask everything
  # block_entities: ["US_SSN", "CREDIT_CARD"]  # opt-in: reject these outright
```

Types not listed are still masked as usual, so blocking is purely additive on top of the default behavior.

### Custom regex patterns

Add your own PII patterns without touching code:

```yaml
pii:
  custom_patterns:
    - pattern: "KD-\\d{6}"
      entity_type: "CUSTOMER_ID"
    - pattern: "REF-[A-Z]{3}-\\d+"
      entity_type: "REFERENCE"
```

### Languages

7 languages supported with spaCy NER and contextual patterns: FR, EN, DE, ES, IT (benchmarked), plus PT and NL (best-effort, not yet in the benchmark).

```yaml
pii:
  detectors:
    presidio:
      languages: ["fr", "en"]  # Add "de", "es", etc.
```

Each language needs its spaCy model: `python -m spacy download de_core_news_md`

## API

OpenAI-compatible:

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat (streaming + non-streaming) |
| `POST /v1/completions` | Text completions |
| `POST /v1/embeddings` | Embeddings (anonymized, no de-anonymization) |
| `GET /v1/models` | List configured models |
| `GET /health` | Health check |
| `GET /ready` | Readiness check |
| `GET /stats` | PII detection stats per session |

### What gets anonymized

Scanned before anything is forwarded to the provider:

- `messages[].content`, whether a plain string or a multimodal list of parts (text parts are scrubbed, images and audio are left alone).
- `tool_calls[].function.arguments` and the legacy `function_call.arguments`: parsed as JSON and scrubbed value by value, including numeric values (a card number sent as a bare JSON number is detected too; on a hit the leaf becomes the masked string). Object keys and the function name stay intact. Arguments that are not valid JSON are scrubbed as free text.
- `/v1/completions` `prompt` and `/v1/embeddings` `input`, as a string or a list of strings.

NOT scanned (know your surface): `messages[].name`, top-level fields like `user` and `metadata`, and `tools`/`functions` definitions are forwarded as-is; JSON object keys inside tool arguments are never rewritten (masking parameter names would break the tool schema). Keep PII out of those fields, or strip them upstream.

On the way back, the original values are restored in `message.content` and in returned `tool_calls` (including the legacy `function_call`), in both non-streaming and streaming responses. Set `pii.passthrough.tool_calls: true` to forward tool-call arguments unchanged.

For a stricter posture, set `pii.strict: true`: any request whose content can't be inspected (a shape that is neither text nor a known media part, e.g. tokenized `input` arrays) is rejected with `400` instead of being forwarded.

Note on `passthrough`: `passthrough.system_messages` and `passthrough.tool_calls` skip the engine entirely for those parts, which also skips `block_entities`. Both are `false` by default; do not enable them together with a block policy you rely on.

## Known limitations

- **Single-word names** from spaCy are dropped (too many false positives). Caught by contextual patterns ("Nom: X") or the `onnx` preset.
- **Lowercase names** need intro patterns ("je m'appelle X"). The `onnx` preset catches them without patterns.
- **Informal dates** ("last Tuesday", "il y a deux ans") are not detected.
- **Unscanned request fields** listed under "What gets anonymized" above (`messages[].name`, `user`, `metadata`, tool definitions, JSON keys).

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
