# PrivAiTe

Privacy-first LLM proxy with transparent PII anonymization. Drop-in replacement for any OpenAI-compatible API.

PrivAiTe sits between your client (OpenWebUI, custom app, etc.) and your LLM providers (OpenAI, Anthropic, Ollama...). It automatically detects and anonymizes personal data before it reaches the LLM, then de-anonymizes the response before returning it to you. All processing is 100% local.

## How it works

```
Client (OpenWebUI) → PrivAiTe Proxy → Anonymize PII → LLM Provider → De-anonymize → Client
```

**Example:**
- You type: *"Je m'appelle Jean Dupont, mon email est jean@acme.com"*
- LLM receives: *"Je m'appelle Samuel Lewis, mon email est tford@example.com"*
- LLM responds: *"Bonjour Samuel ! Votre email tford@example.com est noté."*
- You see: *"Bonjour Jean ! Votre email jean@acme.com est noté."*

## PII types detected

| Type | Method | Example |
|------|--------|---------|
| Person names | spaCy NER + contextual patterns | Jean-Pierre Dupont |
| Email addresses | Regex | jean@acme.com |
| Phone numbers | Regex | +33 6 12 34 56 78 |
| Credit cards | Regex + Luhn | 4111 1111 1111 1111 |
| IBAN | Regex | FR76 3000 6000 ... |
| IP addresses | Regex | 192.168.1.42 |
| SSN | Regex | 2 87 03 69 123 456 78 |
| Locations | spaCy NER | Paris, Lyon |
| French dates | Custom regex | 15 mars 1987 |
| Lowercase names | Context patterns | "je m'appelle dénis navarros" |

## Performance

Benchmarked on Apple M1 Pro, preset `light` (Presidio only), 20 runs per input size.

### PII processing latency

| Input | Chars | PII entities | Avg | P50 | P95 |
|-------|------:|-------------:|----:|----:|----:|
| Short (name + email) | 55 | 2 | 17ms | 10ms | 11ms |
| Medium (8 mixed types) | 250 | 8 | 38ms | 37ms | 39ms |
| Long (repeated, 40 PII) | 1250 | 8 | 116ms | 116ms | 118ms |

### Proxy overhead (vs direct Ollama)

| Path | Avg latency |
|------|------------|
| Ollama direct | ~400ms |
| Proxy + PII anonymization | ~900ms |
| **Overhead** | **~500ms** |

On cloud providers (OpenAI, Anthropic) where latency is 1-5s, the overhead is negligible.

### Boot time

| Preset | Detectors | Boot time |
|--------|-----------|-----------|
| `light` | Presidio (spaCy + regex) | ~1.5s |
| `standard` | + BERT NER (110M params) | ~10s |
| `full` | + OpenAI privacy-filter (1.5B params) | 30s+ |

### Known limitations

- Names written entirely in lowercase are only detected via contextual patterns ("je m'appelle X", "my name is X"). Without such patterns, spaCy NER misses them.
- Dates in French ("15 mars 1987") are detected, but informal date references ("il y a deux ans") are not.
- The MISC→PERSON mapping in spaCy can occasionally flag proper nouns that are not people (e.g., country names detected as PERSON instead of LOCATION). This does not leak PII but may cause unnecessary anonymization of place names.
- Passwords and secrets are not detected unless the `full` preset is used (OpenAI privacy-filter model supports `secret` entity type).
- De-anonymization in streaming mode relies on exact string matching in a token buffer. If the LLM heavily paraphrases or abbreviates a fake name, the original may not be restored.

## Quick start

### 1. Install

```bash
pip install -e .
python -m spacy download en_core_web_lg
python -m spacy download fr_core_news_md
```

For ML-based detectors (standard/full presets):
```bash
pip install -e ".[ml]"
```

### 2. Configure

```bash
cp .env.example .env
cp config/privaite.example.yaml config/privaite.yaml
```

Edit `.env` with your API keys and `config/privaite.yaml` with your providers.

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

| Preset | Detectors | Use case |
|--------|-----------|----------|
| `light` | Presidio (spaCy NER + regex) | Fast, works everywhere |
| `standard` | + BERT NER (dslim/bert-base-NER) | Better name detection |
| `full` | + OpenAI privacy-filter (1.5B) | Best coverage, needs GPU |

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
python -m pytest tests/ -v   # 92 tests
```

## Architecture

```
privaite/
├── api/          FastAPI endpoints (OpenAI-compatible)
├── config/       YAML config + Pydantic schemas
├── middleware/    Auth (Bearer token, constant-time comparison)
├── pii/          PII detection, anonymization, de-anonymization
│   ├── detector_presidio.py    spaCy NER + regex (FR/EN)
│   ├── recognizer_context.py   "je m'appelle X" pattern matching
│   ├── recognizer_fr_date.py   French date detection
│   ├── anonymizer.py           Deterministic Faker-based replacements
│   └── deanonymizer.py         Reverse mapping (exact + fuzzy)
├── providers/    LiteLLM-based multi-provider routing
├── streaming/    SSE streaming with token-level de-anonymization (trie buffer)
└── utils/        Logging, security, error handling
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
