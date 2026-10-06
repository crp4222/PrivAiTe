---
description: >-
  What PrivAiTe protects against and what it does not: local pseudonymization,
  missed detections, unrecognized secret formats, the agent itself in gateway
  mode, and what the optional detection cache keeps in memory.
---

# Threat model

The [README](https://github.com/crp4222/PrivAiTe#threat-model) carries the short
version. This page is the full one.

PrivAiTe performs **local pseudonymization**, not guaranteed anonymization. Detection runs on your machine; the real ↔ placeholder mapping lives in memory only for the duration of a request and is dropped afterwards.

Identical ONNX windows can reuse detection predictions within that same scrub
operation. This bounded cache contains salted input hashes and detection labels/
scores only, uses the current text's offsets, and is cleared on completion or
cancellation. It does not retain input text, token IDs or mappings between requests.
See [request-local window reuse](configuration.md#repeated-onnx-windows-within-one-request).

**What it protects against:** the LLM provider storing, training on, or logging your raw PII. The provider receives placeholders (`<PERSON_1>`, …) for everything the detector catches, across message content, tool-call arguments, and multimodal text.

**What it does NOT protect against:**

- **PII the detector misses.** Detection is statistical and never 100% (see the [benchmark](https://github.com/crp4222/PrivAiTe#benchmark)). A name it doesn't recognize reaches the provider. Treat the output as best-effort, not a guarantee.
- **Unrecognized secret formats.** Context changed the model's predictions in the historical log benchmark. 0.5.0 adds rules for common credential assignments, URI passwords and bearer headers, including that fixture's field names. Unknown names, encoded or split values, and bare values without their field context can still survive; since 0.7.1 a bare copy is covered when the same request showed the value in a recognized field first. This affects every surface that uses the engine. Supported formats and remaining boundary limits are in [detection](detection.md).
- **Re-identification from context.** Even with names replaced, the surrounding text can stay identifying ("the CEO of `<ORG_1>` who resigned in March").
- **A compromised local machine.** The mapping and raw text live in local memory; this is not a defense against a local attacker.
- **The provider correlating** requests within a session.
- **A model inventing replacement values.** Restoration requires the model to
  preserve the placeholder. The [English agent instructions](placeholder-instructions.txt)
  help a cooperative model copy placeholders, including in tool arguments; they
  cannot enforce its behavior or repair a missed detection.
- **The agent itself, in [gateway mode](gateway.md).** The CLI keeps the real values in its own context and local transcripts; only the traffic to the provider is scrubbed. And the agent's own prompt (the Anthropic `system` field, the Responses `instructions` field) is relayed unscanned, so PII in your `CLAUDE.md` or injected project context reaches the provider.

**If you enable the detection cache** (`pii.detection_cache`, off by default), one nuance is added to the promise above. The reversible mapping is still per-request and still dropped when the request ends. But the cache keeps PII-derived **metadata** in process memory for up to its TTL (default 30 minutes) after a request ends: salted BLAKE2b hashes of recently scanned text fragments, plus the positions, types, scores and detector sources of the PII spans found in them. An expired entry is never served again; it is removed from memory on the first cache write after its expiry, and the whole cache is cleared at shutdown, so only a process that goes completely idle keeps its last (expired, unusable) entries longer, until that next write or shutdown. No text, no PII values, no anonymized output, and nothing on disk. The honest delta: an attacker who can already read process memory (who today sees every in-flight request and its full mapping) additionally gains, for up to the TTL after traffic stops (longer only in the idle-process case above), (a) confirmation that a specific candidate text was recently processed, since the hash salt sits in the same memory, and (b) the positions and types of PII inside documents they obtained elsewhere. They gain no raw values and no ability to reverse placeholders. In multi-user deployments there is also a dedup timing side channel: the cache is shared across auth keys, and a cache hit is observably faster than a miss, so one user can in principle probe whether an exact text was recently sent by another. Leave the cache off if any of this matters for your deployment; enable it for [agent CLI sessions](gateway.md), where it removes the cost of re-scanning the entire resent conversation on every turn.

For GDPR/HIPAA: treat this as pseudonymization + transfer minimization, not anonymization. If you need irreversible removal, use `method: "redact"`; the shipped configs already do that for `SECRET` and mask `CREDIT_CARD`, per-type, on top of reversible placeholders for everything else ([entity overrides](configuration.md#entity-overrides-per-type-methods)). Audit it on your own data: [docs/verify.md](verify.md).
