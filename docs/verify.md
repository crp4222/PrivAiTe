---
description: >-
  Audit what PrivAiTe redacts on your own data: the dry-run /v1/pii/inspect
  `privaite verify` command that reads the outbound request body, the dry-run
  /v1/pii/inspect endpoint, and checks on your own traffic. Nothing leaves
  your machine.
---

# Verify what gets redacted

Do not trust the proxy blindly: check it on your own data. Everything below runs
on 127.0.0.1, uses no provider credential, and sends nothing anywhere.

## 1. One command, reads the wire

```
privaite verify
```

It starts a throwaway provider that records exactly what it receives, sends the
same agent-shaped request twice (straight to it, then through a real PrivAiTe
app), and prints both request bodies side by side. The planted values sit in the
three places an agent puts them: message text, tool-call arguments, and tool
output. It exits non-zero if any of them reached the wire, so it works as a gate
in a script, and `--json` gives the machine-readable form.

```
  value                                          where                  direct   proxied
  Marie Dupont                                   message text             LEAK     clean
  marie.dupont@example.invalid                   tool-call argument       LEAK     clean
  4111 1111 1111 1111                            tool-call argument       LEAK     clean
  SERVICE_API_KEY=sk-demo-0000-not-a-real-key    tool output              LEAK     clean
```

The `direct` column is the baseline: it is also what a text-only guardrail
forwards for the two structured fields, since those never pass through its
scrubber. `--preset light` skips the model download at the cost of recall.

This output is what to paste into an issue when something leaks, and it is
reproducible by anyone without your setup or your data.

## 2. See what the provider receives, on your own traffic

The command above uses planted values. For your own, turn de-anonymization off
and send real test traffic:

```yaml
pii:
  deanonymization:
    enabled: false
```

The response comes back without the real values restored, so what you read is
literally what left for the provider (`<PERSON_1>`, `<EMAIL_ADDRESS_1>`, ...).
Diff it against your input: every placeholder is a catch, every real value still
visible is a miss. Works for tool-call arguments and streaming too.

## 3. Dry-run inspection endpoint

Enable it explicitly (off by default), then submit text and get the detections
back. Nothing is forwarded to any provider, nothing is logged, nothing is counted
in `/stats`:

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

`anonymized` is the exact string the provider would have seen, and `would_block`
lists any types your `block_entities` policy would have rejected outright. There
is deliberately no admin view of live traffic: the reversible map is per-request
and in-memory only, never logged or persisted.
