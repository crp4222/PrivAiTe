# Verify what gets redacted

Do not trust the proxy blindly: check it on your own data. Two built-in ways, both
local. For the quickest proof, run the
[tool-call leak demo](https://github.com/crp4222/PrivAiTe/blob/main/examples/demo_tool_call_leak.py)
first: it shows you exactly what a provider receives, without and with PrivAiTe.

## 1. See exactly what the provider receives

Turn de-anonymization off and send test traffic:

```yaml
pii:
  deanonymization:
    enabled: false
```

The response comes back without the real values restored, so what you read is
literally what left for the provider (`<PERSON_1>`, `<EMAIL_ADDRESS_1>`, ...).
Diff it against your input: every placeholder is a catch, every real value still
visible is a miss. Works for tool-call arguments and streaming too.

## 2. Dry-run inspection endpoint

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
