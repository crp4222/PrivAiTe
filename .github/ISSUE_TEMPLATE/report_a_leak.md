---
name: Report a PII leak
about: PrivAiTe forwarded something to the provider that should have been anonymized
title: "[leak] "
labels: leak
---

**What you sent**

The message, prompt, or tool call. Replace any real personal data with fake but realistic values.

**What reached the provider**

The part that was not anonymized. You can see this in the logs, or by pointing PrivAiTe at a request bin instead of a real provider.

**Config**

- Preset: light or onnx
- Languages:
- Anything custom (patterns, overrides):

**Expected**

What should have been replaced, and with what type.

---

Note: detection is statistical and never 100% (see the Threat model in the README). Recall misses on unusual or lowercase names are known and expected. The most valuable reports are structural ones: a whole field or payload shape that is never inspected at all.
