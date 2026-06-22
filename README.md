# PrivAiTe

[![CI](https://github.com/crp4222/PrivAiTe/actions/workflows/ci.yml/badge.svg)](https://github.com/crp4222/PrivAiTe/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-green.svg)](LICENSE)

A privacy proxy for LLMs. It sits between your app and the provider, replaces personal data with placeholders before the request leaves your machine, and restores it in the response — across message text, **tool-call arguments, and multimodal content**. Works with any OpenAI-compatible client.

```
You type: "Je m'appelle Marie Dupont, email marie@acme.com"
LLM sees: "Je m'appelle <PERSON_1>, email <EMAIL_ADDRESS_1>"
LLM says: "Bonjour <PERSON_1>, votre email <EMAIL_ADDRESS_1> est noté."
You  see: "Bonjour Marie Dupont, votre email marie@acme.com est noté."
```

Detection runs locally. This is **local pseudonymization, not guaranteed anonymization** — what it does and doesn't protect against is spelled out in [Threat model](#threat-model).

## How detection works

PrivAiTe uses two detection engines that can run together or separately:

### Presidio (Microsoft) — regex + spaCy NER

The default engine. Handles structured PII through pattern matching and basic NER.

| What it detects | How |
|---|---|
| Emails | Regex |
| Phone numbers | Regex + international format validation |
| Credit cards | Regex + Luhn checksum |
| IBAN | Regex + checksum validation |
| IP addresses | Regex |
| US SSN | Regex + format validation |
| Person names (capitalized, 2+ words) | spaCy NER — only kept if all words are capitalized |
| Person names (lowercase or single word) | Contextual regex — only after "je m'appelle X", "my name is X", "ich heiße X", "Nom: X", etc. |
| Dates (FR/DE) | Custom regex — "15 mars 1987", "3. März 1990" |

Presidio is fast (~25ms/request) and produces zero false positives on code, news articles, and technical text. The tradeoff: it misses names that spaCy doesn't recognize (unusual names, single-word names without context) and doesn't detect secrets/passwords.

### OpenAI Privacy Filter — contextual ML model

[OpenAI's open-source PII model](https://openai.com/index/introducing-openai-privacy-filter/) (1.5B params, 50M active, Apache 2.0). Runs locally via ONNX Runtime (~800MB, no PyTorch needed).

| What it adds over Presidio | How |
|---|---|
| Person names (any format, any case) | ML NER — understands context, not just capitalization |
| Passwords and secrets | Detects "SuperSecret2024!", API keys like "sk-proj-..." |
| Account numbers | Detects bank account numbers, policy numbers, etc. |
| Dates (all languages) | ML-based, not limited to FR/DE regex |

The Privacy Filter is slower (~400ms/request) and occasionally flags technical identifiers as account numbers (e.g., "CMD-2024-98765"). It runs as a second pass alongside Presidio — Presidio handles regex-based entities, the Privacy Filter handles contextual NER.

### Why two engines?

Neither is perfect alone:
- **Presidio alone** misses names that spaCy doesn't recognize, and can't detect secrets. But it has zero false positives.
- **Privacy Filter alone** misses some names in credit/list formats, and doesn't have regex validators for IBAN/credit card checksums.
- **Both together** cover each other's blind spots. Presidio handles structured formats with validation, the Privacy Filter handles context-dependent PII.

## Presets

Choose based on your needs:

| Preset | What runs | Detection | False positives | Speed | Install |
|--------|-----------|-----------|-----------------|-------|---------|
| `light` | Presidio only | 97% | **0%** | **23ms** | `pip install -e .` + spaCy models |
| `onnx` | Presidio + Privacy Filter | **100%** | ~7% | 400ms | + `pip install onnxruntime` |

```yaml
pii:
  preset: "light"   # Zero false positives, fast. Recommended for most users.
  # preset: "onnx"  # Catches everything including secrets. Needs onnxruntime.
```

**When to use `light`:** You want zero disruption. Code, news, business text all pass through untouched. Only clearly identifiable PII (names, emails, phones, cards, IBANs) is anonymized.

**When to use `onnx`:** You need maximum coverage. Secrets, passwords, API keys, account numbers, unusual names. Accept occasional false positives on technical identifiers.

Two other presets exist (`standard`, `full`) but are less useful in practice — they add BERT NER which doesn't improve much over spaCy and requires PyTorch.

## Benchmark

Tested on 61 documents across 5 languages (FR, EN, DE, ES, IT). Corporate letters, contracts, invoices, medical referrals, CVs, bank transfers, news articles, codebases. Mix of synthetic data (valid checksums) and real-world public report extracts.

| | **light** | **onnx** |
|---|---|---|
| Detection | 96.7% (236/244) | **100% (244/244)** |
| False positives | **0/14 (0%)** | 1/14 (7%) |
| PERSON | 93% | **100%** |
| EMAIL | 98% | **100%** |
| PHONE | 100% | 100% |
| IBAN | 100% | 100% |
| CREDIT_CARD | 100% | 100% |
| DATE | 100% | 100% |
| SSN | 100% | 100% |
| Secrets | no | **yes** |

The `light` misses are all PERSON entities: single-word names, long multi-part Spanish names, and names spaCy doesn't recognize. Regex entities are 100% on both presets.

Full benchmark with all test data: [privaite-bench](https://github.com/crp4222/privaite-bench)

## What's NOT detected by default

- **Locations/cities** — "Paris", "London" alone aren't PII (they don't identify anyone). Detecting them causes massive false positives on any text ("Kubernetes", "PIB", "Saturday" all get flagged as locations by spaCy). Disabled by default.
- **URLs** — Presidio's URL regex matches code like `logging.getLogger` because `.ge` is a valid TLD. Disabled by default.
- **Passwords/secrets** — Only the `onnx` preset detects these via the Privacy Filter model.

All of these can be re-enabled in the YAML config if your use case needs them.

## Threat model

PrivAiTe performs **local pseudonymization**, not guaranteed anonymization. Detection runs on your machine; the real ↔ placeholder mapping lives in memory only for the duration of a request and is dropped afterwards.

**What it protects against:** the LLM provider storing, training on, or logging your raw PII. The provider receives placeholders (`<PERSON_1>`, …) for everything the detector catches — across message content, tool-call arguments, and multimodal text.

**What it does NOT protect against:**

- **PII the detector misses.** Detection is statistical and never 100% (see the [benchmark](https://github.com/crp4222/privaite-bench)). A name it doesn't recognize reaches the provider. The `onnx` preset has the best recall; treat the output as best-effort, not a guarantee.
- **Re-identification from context.** Even with names replaced, the surrounding text can stay identifying ("the CEO of `<ORG_1>` who resigned in March").
- **A compromised local machine.** The mapping and raw text live in local memory; this is not a defense against a local attacker.
- **The provider correlating** requests within a session.

For GDPR/HIPAA: treat this as pseudonymization + transfer minimization, not anonymization. If you need irreversible removal, use `method: "redact"` instead of `method: "placeholder"`.

## Alternatives

Keeping PII out of LLM calls is a crowded space, and PrivAiTe is not always the right pick. Based on each project's public docs as of June 2026:

- [AI Security Gateway](https://github.com/aisecuritygateway/aisecuritygateway) does more than PII: it adds secret detection and prompt-injection blocking. If you want those in the same proxy, start there. Its PII scanning targets plain message text.
- [Philter](https://philterd.ai/) is a mature, drop-in "change one URL" redaction proxy for plain-text prompts.
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

For the `onnx` preset (optional):
```bash
pip install onnxruntime
```

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

Point any OpenAI-compatible client to `http://localhost:8400/v1` with your proxy API key.

**OpenWebUI (Docker):** Admin → Settings → Connections → OpenAI API:
- URL: `http://host.docker.internal:8400/v1`
- Key: your `PRIVAITE_API_KEYS` value

## Docker

```bash
docker compose up -d
```

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
    method: "placeholder"        # <PERSON_1>, <EMAIL_ADDRESS_1> — recommended
    # method: "fake_replacement" # Realistic fakes via Faker (Jean → Michel)
    # method: "redact"           # [PERSON], [EMAIL_ADDRESS] — irreversible
    # method: "mask"             # ********
```

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

7 languages supported with spaCy NER + contextual patterns: FR, EN, DE, ES, IT, PT, NL.

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

PII is stripped from every field that carries user text to the provider:

- `messages[].content`, whether a plain string or a multimodal list of parts (text parts are scrubbed, images and audio are left alone).
- `tool_calls[].function.arguments` and the legacy `function_call.arguments`: parsed as JSON and scrubbed value by value, so object keys and the function name stay intact. Arguments that are not valid JSON are scrubbed as free text.
- `/v1/completions` `prompt` and `/v1/embeddings` `input`, as a string or a list of strings.

On the way back, the original values are restored in `message.content` and, for non-streaming chat, in returned `tool_calls`. Set `pii.passthrough.tool_calls: true` to forward tool-call arguments unchanged.

For a stricter posture, set `pii.strict: true`: any request whose content can't be inspected (a shape that is neither text nor a known media part) is rejected with `400` instead of being forwarded.

## Known limitations

- **Single-word names** from spaCy are dropped (too many false positives). Caught by contextual patterns ("Nom: X") or the `onnx` preset.
- **Lowercase names** need intro patterns ("je m'appelle X"). The `onnx` preset catches them without patterns.
- **Informal dates** ("last Tuesday", "il y a deux ans") are not detected.
- **No policy gate** — all requests are forwarded after pseudonymization.
- **Streaming tool calls**: argument deltas are not de-anonymized, so a streamed tool call may show placeholders instead of the original values. Request-side anonymization still applies, so no PII leaks.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
