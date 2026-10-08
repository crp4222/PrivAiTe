# PrivAiTe

**Your coding agent reads your `.env` files, logs and command output, and everything it reads goes to the LLM provider.** PrivAiTe is a local proxy that replaces the secrets and personal data in every request before it leaves your machine, then puts the personal data back in the reply.

[![CI](https://github.com/crp4222/PrivAiTe/actions/workflows/ci.yml/badge.svg)](https://github.com/crp4222/PrivAiTe/actions) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![License](https://img.shields.io/badge/license-BSD--3--Clause-green.svg)](https://github.com/crp4222/PrivAiTe/blob/main/LICENSE) [![PyPI](https://img.shields.io/pypi/v/privaite.svg)](https://pypi.org/project/privaite/)

```text
# your agent reads and sends:
OPENAI_API_KEY=sk-demo-fake000
DB_PASSWORD=demo-pass-4821
ADMIN_EMAIL=marie@example.com
# the provider receives:
OPENAI_API_KEY=[SECRET]
DB_PASSWORD=[SECRET]
ADMIN_EMAIL=<EMAIL_ADDRESS_1>
```

That is real engine output with the shipped config, not a mock-up. In the reply, `<EMAIL_ADDRESS_1>` becomes the real address again, streaming included. `[SECRET]` does not come back: the shipped configs redact secrets and mask card numbers for good, a [per-type setting](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md#entity-overrides-per-type-methods). Detection runs locally and is best-effort, not a guarantee: the [threat model](https://github.com/crp4222/PrivAiTe#threat-model) says what it does not cover.

## See it work in one command, no API key

```bash
docker run --rm \
  ghcr.io/crp4222/privaite:0.7.2 \
  python -m privaite verify
```

```text
  value                                          where                  direct   proxied
  Marie Dupont                                   message text             LEAK     clean
  marie.dupont@example.invalid                   tool-call argument       LEAK     clean
  4111 1111 1111 1111                            tool-call argument       LEAK     clean
  SERVICE_API_KEY=sk-demo-0000-not-a-real-key    tool output              LEAK     clean
RESULT: PASSED, every planted value was replaced before the request left.
```

It starts a throwaway provider on 127.0.0.1, sends the same agent-shaped request straight to it and then through PrivAiTe, and prints what each request body contained: every planted value shows `LEAK` when sent directly and `clean` through the proxy. About 20 seconds on a laptop CPU once the image is pulled (it carries the detection model and takes about 3 GB on disk). With pip, `privaite verify` does the same after the install below; its first run downloads the model (about 900 MB). It exits non-zero if a value leaks: [docs/verify.md](https://github.com/crp4222/PrivAiTe/blob/main/docs/verify.md).

## Quick start

**Docker:** the detection model is baked in, so it runs offline. `PRIVAITE_API_KEYS` is the key your client sends to PrivAiTe (pick any value); `OPENAI_API_KEY` is your real provider key, which stays in the container.
```bash
docker run -d -p 8400:8400 \
  -e PRIVAITE_API_KEYS=change-me \
  -e OPENAI_API_KEY=sk-... \
  ghcr.io/crp4222/privaite:0.7.2
```

**pip:** the same proxy with the same shipped config, which exposes `gpt-4o-mini` and `gpt-4o`.
```bash
python -m pip install --upgrade "privaite>=0.7.2"
python -m spacy download en_core_web_lg && python -m spacy download fr_core_news_md
curl -fsSLO https://raw.githubusercontent.com/crp4222/PrivAiTe/v0.7.2/config/privaite.openai.yaml
OPENAI_API_KEY=sk-... PRIVAITE_API_KEYS=change-me python -m privaite --config privaite.openai.yaml
```

**Connect:** point any OpenAI-compatible client at `http://localhost:8400/v1` with the key `change-me` (from Open WebUI running in Docker: `http://host.docker.internal:8400/v1`). For Ollama, Azure or any other provider, [write your own config](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md) and [mount it in Docker](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md#docker-with-a-custom-config). The image is also on Docker Hub as `crp4222/privaite:0.7.2`, and client snippets are in [`examples/`](https://github.com/crp4222/PrivAiTe/tree/main/examples/).

## Claude Code and Codex

Gateway mode (opt-in, off by default) speaks the native protocols of agent CLIs. It scrubs each request, tool results and tool-call arguments included, relays the CLI's own auth upstream untouched, and restores the reply.
```yaml
gateway:
  enabled: true
  anthropic:
    base_url: "https://api.anthropic.com/v1"
```

Then run `ANTHROPIC_BASE_URL=http://localhost:8400 claude`. Also set `pii.detection_cache.enabled: true`: agent CLIs resend the whole conversation every turn, and the cache avoids re-scanning it (the [threat model](https://github.com/crp4222/PrivAiTe/blob/main/docs/threat-model.md) spells out its memory tradeoff). Codex support is beta; setup, scanned surface and limits are in [docs/gateway.md](https://github.com/crp4222/PrivAiTe/blob/main/docs/gateway.md). In wire-level captures of real sessions on a repository with 24 planted values, Claude Code sent 24 of 24 to its provider and Codex 20 of 24 when run directly. Through the gateway, none reached it on the small fixture and 2 of 24 did on a larger session: [the measurement](https://github.com/crp4222/PrivAiTe/blob/main/docs/agent-leak-measurement.md).

- **Measured, not promised.** Those numbers do not establish zero leaks on arbitrary agent traffic.
- **It protects the egress, not the agent.** The CLI keeps the real values in its own context and local transcripts.
- **The agent's own prompt is not scanned.** The `system` and `instructions` fields pass through as-is, and Claude Code puts your `CLAUDE.md` there.
- **The gateway routes are open.** They accept a request that carries no `PRIVAITE_API_KEYS` value, and the server binds `0.0.0.0` by default with no rate limit: bind it to localhost or keep the port off untrusted networks.

## What it does

- **It reads inside the JSON.** Tool-call arguments are parsed and scrubbed value by value; tool results and multimodal text parts are scanned like any message.
- **It restores the reply, streaming included**, in content, tool calls and reasoning.
- **It fails closed and stays local.** If detection errors, the request is blocked, not forwarded. Detection (Presidio plus OpenAI's open privacy-filter model) runs on your machine, with no telemetry: the [outbound connections](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md#outbound-connections) page lists what the dependencies tried to send and how each is turned off.

## Benchmark

| Solution | Recall (span) | Recall (strict) | False positives | Tool-call protection |
|---|---|---|---|---|
| `onnx` (default) | **85.2%** | **81.7%** | 2 / 14 | **100%** |
| `light` (full Presidio) | 62.7% | 58.1% | 3 / 14 | **100%** |
| LiteLLM Presidio guardrail | 70.3% | 65.3% | 3 / 14 | 0.0% |
| LLM Guard (Anonymize) | 76.9% | 74.9% | 5 / 14 | 0.0% |

Measured on 120 real documents from the open [AI4Privacy `pii-masking-200k`](https://huggingface.co/datasets/ai4privacy/pii-masking-200k) dataset (458 PII items in DE, EN, FR, IT), plus 14 clean documents for false positives. Read the 100% precisely, it is structural: of the PII PrivAiTe detects in plain text, all of it is also removed from tool-call JSON, so its tool-call leak equals its detection misses (14.8% on this corpus). The two guardrails measured here do not look inside tool-call arguments; other gateways do, and the [comparison](https://github.com/crp4222/PrivAiTe/blob/main/docs/comparison.md) lists what each project states and when to pick it instead. LLM Guard's model is fine-tuned on this exact dataset, so its recall here is optimistic. Methodology, per-language tables, out-of-distribution checks and reproduction: [privaite-bench](https://github.com/crp4222/privaite-bench).

## Integrations

- **Open WebUI filter** ([setup](https://github.com/crp4222/PrivAiTe/blob/main/integrations/openwebui/README.md), [hub listing](https://openwebui.com/posts/privaite_pii_anonymizer_351aa088)): the engine in-process, no separate proxy. Open WebUI sends its title, tag and follow-up calls without running filters, so with a cloud model use the standalone proxy instead: it sees those calls too.
- **LiteLLM guardrail** ([setup](https://github.com/crp4222/PrivAiTe/blob/main/integrations/litellm/README.md)): a custom guardrail for teams already on the LiteLLM proxy. It anonymizes requests and restores responses inline, including tool-call arguments, which LiteLLM's built-in Presidio guardrail does not scan.

## Threat model

PrivAiTe does **local pseudonymization**, not anonymization: the real-to-placeholder mapping lives in memory for the duration of a request. It protects against the provider storing, training on or logging your raw values: for everything the detector catches, the provider receives placeholders, in message text, tool-call arguments and multimodal text. It does not protect against:

- **Missed detections.** Detection is statistical; a value it does not recognize reaches the provider (see the [benchmark](https://github.com/crp4222/PrivAiTe#benchmark)).
- **Unrecognized secret formats:** unknown field names, encoded or split values, bare values without their field context.
- **Re-identification.** The surrounding text can stay identifying ("the CEO of `<ORG_1>` who resigned in March"), and the provider can correlate requests within a session.
- **A compromised local machine.** The mapping and the raw text live in local memory.
- **A model inventing a value** instead of copying a placeholder: nothing can be restored then.
- **The agent itself, in gateway mode.** The CLI keeps the real values locally, and its own prompt is relayed unscanned.

For GDPR or HIPAA, treat this as pseudonymization plus transfer minimization; you remain the data controller. The full version, including what the optional detection cache keeps in memory: [docs/threat-model.md](https://github.com/crp4222/PrivAiTe/blob/main/docs/threat-model.md).

## Docs

- **Presets:** `onnx` is the default. `light` is faster and needs no model download, but it misses names the default finds: the demo above fails with `--preset light`. [Presets](https://github.com/crp4222/PrivAiTe/blob/main/docs/detection.md#presets)
- **Your own types and hard blocks:** regex `custom_patterns`, a per-type fate, and `block_entities` for what must never leave. [Your policy, your types](https://github.com/crp4222/PrivAiTe/blob/main/docs/policy.md)
- **What is scanned and what is not:** the exact request fields. [API reference](https://github.com/crp4222/PrivAiTe/blob/main/docs/api.md#what-gets-anonymized)
- **Everything else:** [detection](https://github.com/crp4222/PrivAiTe/blob/main/docs/detection.md), [configuration](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md), [redacting PII before an LLM call](https://github.com/crp4222/PrivAiTe/blob/main/docs/redact-pii-before-llm.md), [changelog](https://github.com/crp4222/PrivAiTe/blob/main/CHANGELOG.md), and the same pages as a site: [crp4222.github.io/PrivAiTe](https://crp4222.github.io/PrivAiTe/). To develop: clone, `pip install -e ".[dev]"`, download the two spaCy models above, then `python -m pytest tests/`.

## License

BSD 3-Clause. See [LICENSE](https://github.com/crp4222/PrivAiTe/blob/main/LICENSE).
