# PrivAiTe

[![CI](https://github.com/crp4222/PrivAiTe/actions/workflows/ci.yml/badge.svg)](https://github.com/crp4222/PrivAiTe/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-green.svg)](LICENSE)

Privacy-first LLM proxy with transparent PII anonymization. Drop-in replacement for any OpenAI-compatible API.

PrivAiTe sits between your client (OpenWebUI, custom app, etc.) and your LLM providers (OpenAI, Anthropic, Ollama...). It automatically detects and anonymizes personal data before it reaches the LLM, then de-anonymizes the response before returning it to you. All processing is 100% local.

## How it works

```
Client (OpenWebUI) → PrivAiTe Proxy → Anonymize PII → LLM Provider → De-anonymize → Client
```

**What the user sees** — real data, normal conversation:

![User view](docs/opwui.png)

**What the LLM provider receives** — only placeholders, no PII:

![Provider logs](docs/openailogs.png)

## PII detection coverage

Multiple detection engines can run in parallel. Results are merged with configurable overlap resolution.

| Type | Presidio (light) | ONNX (openai/privacy-filter) | Notes |
|------|:---:|:---:|-------|
| Person names (capitalized) | yes | yes | spaCy NER + ONNX NER |
| Person names (lowercase) | yes | no | Via contextual patterns ("je m'appelle X", "my name is X", etc.) |
| Email addresses | yes | yes | Regex |
| Phone numbers | yes | yes | Regex + ML |
| Credit cards | yes | yes | Regex + Luhn |
| IBAN | yes | no | Regex |
| IP addresses | yes | yes | Regex (Presidio) / ML as URL (ONNX) |
| US SSN | yes | no | Regex |
| Locations | yes | yes | spaCy NER + ONNX NER |
| French dates | yes | yes | Custom regex + ONNX NER |
| URLs | yes | yes | Regex + ML |
| Passwords / secrets | no | yes | Only ONNX detects `secret` type |
| Account numbers | no | yes | Only ONNX detects `account_number` type |

**Language support:** Presidio uses spaCy models (FR + EN). The ONNX model (openai/privacy-filter) was trained primarily on English and performs best on English text. Detection quality degrades on non-English text. For French, Presidio's spaCy FR model + contextual patterns provide better coverage than ONNX alone.

## Performance

Benchmarked on Apple M1 Pro (16GB), 20 runs per input. Cached models (not first download).

### Boot time

| Preset | Detectors | Boot | Extra deps |
|--------|-----------|------|------------|
| `light` | Presidio (spaCy + regex + context patterns) | ~2s | spacy |
| `standard` | + BERT NER (dslim/bert-base-NER, 110M) | ~10s | spacy, torch |
| `onnx` | Presidio + OpenAI privacy-filter (Q4F16, 809MB) | ~7s | spacy, onnxruntime |
| `full` | Presidio + BERT + privacy-filter (transformers) | 30s+ | spacy, torch |

### PII processing latency per request

| Preset | Short text (50 chars, 2 PII) | Medium text (250 chars, 8 PII) | Notes |
|--------|---:|---:|-------|
| `light` | ~15ms | ~40ms | Fastest. Good for local LLMs. |
| `onnx` | ~210ms | ~350ms | Adds ~250ms for ML inference. |

### Proxy overhead (vs direct Ollama, ministral 8.9B local)

| Path | Avg latency |
|------|------------|
| Ollama direct | ~400ms |
| Proxy + PII (light) | ~900ms |
| Proxy + PII (onnx) | ~1150ms |

On cloud providers (OpenAI, Anthropic) where latency is 1-5s, the overhead is negligible.

### Detection coverage comparison (tested)

| Test case | light | onnx |
|-----------|:-----:|:----:|
| FR: 8 mixed PII types (name, email, phone, card, IBAN, IP, date, location) | 8/8 | 8/8 |
| EN: name, email, phone, SSN, card, location, password | 5/7 | 7/7 |
| FR lowercase: "je m'appelle dénis navarros" | 2/2 | 2/2 |
| Mixed FR/EN: names, emails, phones | 4/5 | 5/5 |

The `onnx` preset catches passwords/secrets and account numbers that `light` misses. The `light` preset has better French date detection via custom regex.

## Privacy model

PrivAiTe performs **local pseudonymization**, not guaranteed anonymization. The re-identification mapping exists in memory for the duration of each request to enable de-anonymization of responses. The mapping is never persisted to disk and is destroyed after the response is returned.

This means:
- PII is never sent to the LLM provider in cleartext.
- The proxy operator (you) can still re-identify data during a request's lifetime.
- Once the request completes, the mapping is gone — but the original data still exists in the calling application (OpenWebUI, etc.).

For compliance purposes (GDPR, HIPAA), treat this as **pseudonymization + transfer control**, not full anonymization. If your threat model requires that no one — including the proxy operator — can re-identify data, you should use `method: "redact"` (which destroys the original values) instead of `method: "placeholder"`.

## Known limitations

- **Lowercase names** are detected via contextual patterns ("je m'appelle X", "my name is X", "appelez-moi X", "mon nom est X", "je suis X"). Without such intro patterns, lowercase names may be missed.
- **French dates** ("15 mars 1987") are detected by a custom regex recognizer. Informal references ("il y a deux ans") are not.
- **MISC entity mapping:** spaCy sometimes labels proper nouns as MISC. PrivAiTe maps MISC→PERSON for French, which can occasionally anonymize place names as persons. This does not leak PII.
- **No policy gate:** all requests are forwarded after pseudonymization. There is no content classification or approval step before sending to the provider.
- **No audit trail:** detected PII types and counts are not logged by default. Enable debug logging for development, but never log PII values in production.

## Quick start

### 1. Install

```bash
pip install -e .
python -m spacy download en_core_web_lg
python -m spacy download fr_core_news_md
```

For the ONNX detector:
```bash
pip install onnxruntime
```

For the BERT/transformers detectors (standard/full presets):
```bash
pip install -e ".[ml]"
```

### 2. Configure

```bash
cp .env.example .env
cp config/privaite.example.yaml config/privaite.yaml
```

Edit `.env` with your API keys and `config/privaite.yaml` with your providers and PII preset.

### 3. Run

```bash
PRIVAITE_API_KEYS=sk-your-key python -m privaite

# Development (auto-reload on changes)
PRIVAITE_API_KEYS=sk-your-key python -m privaite --reload
```

### 4. Connect your client

Point any OpenAI-compatible client to `http://localhost:8400/v1` with your proxy API key.

**OpenWebUI:** Admin → Settings → Connections → OpenAI API:
- URL: `http://host.docker.internal:8400/v1` (if OpenWebUI runs in Docker)
- Key: your `PRIVAITE_API_KEYS` value

## Docker

```bash
docker compose up -d
```

## Configuration

### PII presets

```yaml
pii:
  preset: "light"    # Recommended for most users
  # preset: "onnx"   # Adds password/secret detection
```

| Preset | Detectors | Best for |
|--------|-----------|----------|
| `light` | Presidio (spaCy NER + regex + context patterns) | Fast, low resource, good coverage |
| `standard` | + BERT NER (dslim/bert-base-NER) | Better name detection |
| `onnx` | Presidio + OpenAI privacy-filter (ONNX Q4F16) | Secret/password detection, no PyTorch |
| `full` | Presidio + BERT + privacy-filter (transformers) | Maximum coverage, heavy |

### Anonymization modes

```yaml
pii:
  anonymization:
    method: "placeholder"        # <PERSON_1>, <EMAIL_ADDRESS_1> (recommended)
    # method: "fake_replacement" # Realistic fakes via Faker
    # method: "redact"           # [PERSON], [EMAIL_ADDRESS]
    # method: "mask"             # ********
```

### Provider examples

```yaml
providers:
  - model_name: "gpt-4o"
    litellm_params:
      model: "openai/gpt-4o"
      api_key: "${OPENAI_API_KEY}"

  - model_name: "claude-sonnet"
    litellm_params:
      model: "anthropic/claude-sonnet-4-20250514"
      api_key: "${ANTHROPIC_API_KEY}"

  - model_name: "llama3"
    litellm_params:
      model: "ollama/llama3.1"
      api_base: "http://localhost:11434"
```

Any provider supported by [LiteLLM](https://docs.litellm.ai/docs/providers) works out of the box.

## API

OpenAI-compatible endpoints:

- `POST /v1/chat/completions` — Chat (streaming + non-streaming)
- `POST /v1/completions` — Text completions
- `POST /v1/embeddings` — Embeddings (PII anonymized, no de-anonymization)
- `GET /v1/models` — List available models
- `GET /health` — Health check
- `GET /ready` — Readiness check

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Architecture

```
privaite/
├── api/          FastAPI endpoints (OpenAI-compatible)
├── config/       YAML config + Pydantic schemas
├── middleware/    Auth (Bearer token, constant-time comparison)
├── pii/          PII detection, anonymization, de-anonymization
│   ├── detector_presidio.py    spaCy NER + regex (FR/EN)
│   ├── detector_onnx.py        OpenAI privacy-filter via ONNX Runtime
│   ├── recognizer_context.py   "je m'appelle X" pattern matching
│   ├── recognizer_fr_date.py   French date detection
│   ├── anonymizer.py           Numbered placeholders or Faker-based replacements
│   └── deanonymizer.py         Reverse mapping (exact + fuzzy)
├── providers/    LiteLLM-based multi-provider routing
├── streaming/    SSE streaming with token-level de-anonymization (trie buffer)
└── utils/        Logging, security, error handling
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
