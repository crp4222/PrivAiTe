# Configuration reference

Everything below goes in your `privaite.yaml` (`python -m privaite --config privaite.yaml`).
The [README quick start](https://github.com/crp4222/PrivAiTe#quick-start) has the minimal
working file; this page covers every knob.

## Server

```yaml
server:
  host: "0.0.0.0"   # default
  port: 8400        # default
```

Both can also be overridden at launch: `python -m privaite --config privaite.yaml --host 127.0.0.1 --port 8452`.

## LLM providers

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

A `${VAR}` whose environment variable is unset fails startup on purpose, so a
missing key is caught at boot rather than at the first request.

## Docker with a custom config

With just `OPENAI_API_KEY` set, the image exposes `gpt-4o-mini` and `gpt-4o`. For
any other provider (Ollama, Azure, a self-hosted endpoint, or your own LiteLLM
proxy), mount a config:

```bash
docker run -d -p 8400:8400 \
  -e PRIVAITE_API_KEYS=change-me \
  -v $PWD/privaite.yaml:/app/config/privaite.yaml:ro \
  ghcr.io/crp4222/privaite
```

The image is published to both `ghcr.io/crp4222/privaite` and Docker Hub
(`crp4222/privaite`); either works in the commands above.

A minimal `privaite.yaml`:

```yaml
providers:
  - model_name: my-model
    litellm_params:
      model: openai/gpt-4o-mini   # any litellm model string, e.g. ollama/llama3
      api_base: https://api.openai.com/v1
      api_key: ${OPENAI_API_KEY}
pii:
  enabled: true
  preset: onnx
```

Auth is on by default, so `PRIVAITE_API_KEYS` is required. From a clone you can
also `docker compose up -d`; put the key in a `.env` file to keep it off the
command line.

## Anonymization method

```yaml
pii:
  anonymization:
    method: "placeholder"        # <PERSON_1>, <EMAIL_ADDRESS_1> (recommended)
    # method: "fake_replacement" # Realistic fakes via Faker (Jean → Michel)
    # method: "redact"           # [PERSON], [EMAIL_ADDRESS] (irreversible)
    # method: "mask"             # ******** (irreversible)
```

`redact` and `mask` are lossy on purpose: the original never enters the reversible
map, so nothing is restored in responses for those types (and two values that mask
to the same string can never cross-restore).

## Blocking specific PII types (hard policy gate)

By default every detected PII item is pseudonymized and the request goes through.
If some PII types must **never** leave your network at all, even as a placeholder,
list them under `block_entities`. A request containing any listed type is rejected
with `400` and nothing is forwarded to the provider. The error names the type(s),
never the value.

```yaml
pii:
  block_entities: []                     # default: block nothing, mask everything
  # block_entities: ["US_SSN", "CREDIT_CARD"]  # opt-in: reject these outright
```

Types not listed are still masked as usual, so blocking is purely additive on top
of the default behavior.

## Custom regex patterns

Add your own PII patterns without touching code:

```yaml
pii:
  custom_patterns:
    - pattern: "KD-\\d{6}"
      entity_type: "CUSTOMER_ID"
    - pattern: "REF-[A-Z]{3}-\\d+"
      entity_type: "REFERENCE"
```

## Languages

7 languages supported with spaCy NER and contextual patterns: FR, EN, DE, ES, IT
(benchmarked), plus PT and NL (best-effort, not yet in the benchmark).

```yaml
pii:
  detectors:
    presidio:
      languages: ["fr", "en"]  # the default; add "de", "es", etc.
```

Each language needs its spaCy model: `python -m spacy download de_core_news_md`.
The default list is `["fr", "en"]`, so a fresh install fetches `fr_core_news_md`
on first boot if it is missing; set `languages: ["en"]` for an English-only,
no-surprise-download setup.

## Detector model revisions

The built-in Hugging Face detector models are pinned to immutable commits, so a
fresh install does not silently pick up different weights from a moving `main`
branch:

- [`openai/privacy-filter`](https://huggingface.co/openai/privacy-filter/commit/7ffa9a043d54d1be65afb281eddf0ffbe629385b): `7ffa9a043d54d1be65afb281eddf0ffbe629385b` (the default ONNX detector and the optional torch detector)
- [`dslim/bert-base-NER`](https://huggingface.co/dslim/bert-base-NER/commit/d1a3e8f13f8c3566299d95fcfc9a8d2382a9affc): `d1a3e8f13f8c3566299d95fcfc9a8d2382a9affc`
- [`urchade/gliner_multi_pii-v1`](https://huggingface.co/urchade/gliner_multi_pii-v1/commit/1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d): `1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d`

When changing a detector's `model_name`, also set `revision` to a commit SHA
from that model's repository. A default SHA from a different repository fails to
load instead of silently using different weights. `revision: null` intentionally
follows the mutable default branch and is not reproducible.
