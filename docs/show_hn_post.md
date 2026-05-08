# Show HN: PrivAiTe – Privacy proxy for LLMs that anonymizes PII before it leaves your machine

**Title:** Show HN: PrivAiTe – Open-source proxy that strips PII from LLM requests automatically

**URL:** https://github.com/crp4222/PrivAiTe

**Text:**

I built PrivAiTe because I wanted to use ChatGPT and other LLMs from OpenWebUI without sending personal data to cloud providers.

It's an OpenAI-compatible proxy that sits between your client and any LLM provider. Before forwarding a request, it detects and replaces PII (names, emails, phones, credit cards, IBANs, IPs, etc.) with numbered placeholders. When the response comes back, it reverses the mapping so you see the real data — but the LLM never did.

How it works:

- You type: "My name is Sarah Johnson, email sarah@acme.com"  
- LLM receives: "My name is <PERSON_1>, email <EMAIL_ADDRESS_1>"  
- LLM responds: "Hello <PERSON_1>, I've noted <EMAIL_ADDRESS_1>"  
- You see: "Hello Sarah Johnson, I've noted sarah@acme.com"

All PII detection runs locally using Microsoft Presidio (spaCy NER + regex). No data ever leaves your machine for anonymization.

Key features:
- Drop-in replacement for OpenAI API (works with OpenWebUI, any SDK)
- Supports 100+ LLM providers via LiteLLM (OpenAI, Anthropic, Ollama...)
- Streaming SSE with real-time de-anonymization
- Deterministic mappings for multi-turn conversation consistency
- ~15ms overhead for short texts, ~40ms for long texts
- Docker-ready, configurable via YAML

Stack: Python, FastAPI, Presidio, LiteLLM, spaCy

Would love feedback on the approach and detection coverage.

---

# r/selfhosted post

**Title:** PrivAiTe: self-hosted privacy proxy that anonymizes PII before sending to ChatGPT/Claude/Ollama

I built a proxy that automatically strips personal data from your LLM conversations before they reach the provider. Names, emails, phone numbers, credit cards, IBANs — all get replaced with placeholders, and the responses get de-anonymized back transparently.

Works as a drop-in OpenAI API replacement. I use it with OpenWebUI + GPT-4o + local Ollama models behind a single endpoint.

- GitHub: https://github.com/crp4222/PrivAiTe
- Docker: `docker compose up -d`
- Config: YAML file, env vars for API keys
- ~15ms processing overhead per request

Open source (BSD-3). Looking for feedback and contributors.
