---
description: >-
  How PrivAiTe detects PII: Microsoft Presidio (regex + spaCy NER) and the
  openai/privacy-filter ONNX model, what each engine catches, presets and known
  limitations.
---

# How PrivAiTe detects PII locally, with Presidio and OpenAI's privacy-filter model

PrivAiTe uses two detection engines that can run together or separately.

## Presidio (Microsoft): regex + spaCy NER

The default engine. Handles structured PII through pattern matching and basic NER.

| What it detects | How |
|---|---|
| Emails | Regex |
| Phone numbers | Regex + international format validation |
| Credit cards | Regex + Luhn checksum |
| IBAN | Regex + checksum validation |
| IP addresses | Regex |
| US SSN | Regex + format validation |
| Person names (capitalized, 2+ words) | spaCy NER, only kept if all words are capitalized |
| Person names (lowercase or single word) | Contextual regex, only after "je m'appelle X", "my name is X", "ich heiße X", "Nom: X", etc. A cue that only means "I am" ("je suis", "I'm") additionally requires a capitalized name |
| Dates (FR/DE) | Custom regex, "15 mars 1987", "3. März 1990", under a French or German configuration |
| Structured secrets (0.5.0) | PrivAiTe patterns for credential assignments, URI passwords and Authorization bearer values |

Presidio is faster than the contextual model and produces few false positives on the clean benchmark documents. It misses names that spaCy doesn't recognize and arbitrary passwords without a recognized field name or URI/header structure.

## OpenAI Privacy Filter: contextual ML model

[OpenAI's open-source PII model](https://openai.com/index/introducing-openai-privacy-filter/) (1.5B params, 50M active, Apache 2.0). Runs locally via ONNX Runtime (~800MB, no PyTorch needed).

| What it adds over Presidio | How |
|---|---|
| Person names (any format, any case) | ML NER, understands context, not just capitalization |
| Passwords and secrets | Detects "SuperSecret2024!", API keys like "sk-proj-..." |
| Account numbers | Detects bank account numbers, policy numbers, etc. |
| Dates (all languages) | ML-based, not limited to FR/DE regex |

The Privacy Filter adds model-inference cost and occasionally flags technical identifiers as account numbers (e.g., "CMD-2024-98765"). It runs concurrently with Presidio, which handles structured formats while the Privacy Filter handles contextual NER. Cost depends on input length; the benchmark reports the combined engine latency.

## Why two engines?

Neither is perfect alone:

- **Presidio with PrivAiTe's recognizers** catches specific credential formats, but misses unfamiliar names and secrets without those structural cues.
- **Privacy Filter alone** misses some names in credit/list formats, and doesn't have regex validators for IBAN/credit card checksums.
- **Both together** cover each other's blind spots. Presidio handles structured formats with validation, the Privacy Filter handles context-dependent PII.

## What's NOT detected by default

The default `onnx` preset does detect personal addresses (as `LOCATION`) and personal URLs (as `URL`) through the Privacy Filter model, and replaces them. What stays off by default are Presidio's broad recognizers for those types, because they cause heavy false positives:

- **Generic place names (the Presidio LOCATION recognizer):** "Paris" or "London" on their own aren't PII, and spaCy flags ordinary words ("Kubernetes", "Saturday") as locations. The `onnx` preset keeps this recognizer off and relies on the model's context-aware address detection instead. PrivAiTe's own cue-based location patterns ("elle habite à X", "lives in X", "domicilié à X") do fire under every preset: they require a residence cue, so they do not carry spaCy's false-positive rate. Any recognizer PrivAiTe registers, and any `custom_patterns` type, is exempt from a preset's entity allowlist; the allowlist only scopes Presidio's own recognizers. Use [`disabled_recognizers`](configuration.md#built-in-recognizers) to switch one off. Their cues are vocabulary, so each one only fires under the language it is written in: configure every language your traffic uses.
- **The Presidio URL regex:** it matches code like `logging.getLogger` because `.ge` is a valid TLD. The `onnx` preset keeps it off, and the model still catches genuine personal URLs.

The `light` preset has no contextual Privacy Filter model. 0.5.0 adds the same structured-secret rules to every preset that enables Presidio. Broad password recognition still requires an ML detector; these rules do not make `light` equivalent to `onnx`.

## Structured credentials and overlapping types

The new rules supplement detection; they never short-circuit the NLP engines.
They recognize common assignment names (`api_key`, `apiKey`, `access_token`,
`refresh_token`, `auth_token`, `client_secret`, `password`, `passwd`,
`smtp_secret`, `presented_key`) and underscore-prefixed environment variants
such as `OPENAI_API_KEY` and `DB_PASSWORD`, case-insensitively. They also
recognize passwords in `scheme://user:password@host` and plaintext
`Authorization: Bearer ...` headers.

For quoted assignments, only the value is replaced: `api_key="demo-only"`
becomes `api_key="[SECRET]"` with the shipped redaction policy. Escaped quotes
are included in the value. Bare values extend to whitespace; punctuation may
be part of a password and is not stripped. Use quotes when a comma or brace
must be unambiguously preserved as syntax.

Overlapping detections now respect policy before confidence: a blocked type
wins over other types; a type configured with `redact` or `mask` wins over a
reversible type. Equal-policy matches use the configured overlap resolution.
The union still covers every detected character, so an overlapping EMAIL
detection can widen a URI password's redaction to include the host. The cache
fingerprint includes this policy; it cannot replay an obsolete winner.

These changes shipped in **0.5.0**; 0.4.x does not have them. The earlier
agent-session measurements remain historical results; an offline replay of
the synthetic fixture is not a new live-agent measurement.
The [regression replay report](https://github.com/crp4222/privaite-bench/blob/main/agent_workflow/STRUCTURED_SECRETS.md)
contains the before/after counts, individual timings and reproduction commands.

## Experimental model evaluation

The [Privy and Kiji benchmark](https://github.com/crp4222/privaite-bench/blob/main/KIJI_PRIVY.md)
checks the 0.5.0 source on 300 synthetic protocol traces, the existing
120-document multilingual corpus, clean inputs, and long-log regressions.
Kiji is an adapter in the benchmark repository, not a supported PrivAiTe preset.

On Privy's 491 annotated spans, the current `onnx` stack fully removes 258
(52.55%). Replacing Privacy Filter with the tested Kiji ONNX artifact while
keeping the same Presidio configuration removes 147 (29.94%), with lower
character precision and lower latency. Password coverage drops from 12/15
to 2/15. These results support retaining the current default; they also expose
its remaining misses, including three passwords in SQL traces. A successful
replay of the planted log credentials does not establish detection of arbitrary
protocol data. The report gives per-type counts, artifact limitations, latency,
and the results of adding Kiji alongside the existing engines.

## Known limitations

- **Single-word names** from spaCy are dropped (too many false positives). Caught by contextual patterns ("Nom: X") or the `onnx` preset.
- **Lowercase names** need intro patterns ("je m'appelle X"). The `onnx` preset catches them without patterns.
- **Informal dates** ("last Tuesday", "il y a deux ans") are not detected.
- **Secrets without recognized structure can still survive.** The contextual
  model missed two secrets in the historical agent benchmark when preceding
  log lines changed its predictions. The new patterns target that fixture's
  `presented_key` and `smtp_secret` fields; arbitrary field names, encoded or
  fragmented values and unfamiliar formats still need detection or custom rules.
  Parsed tool-call JSON is scanned value by value: a `password` object key is
  not automatically supplied as context to a bare string value.
- **Boundaries remain conservative.** Policy-aware merging prevents a detected
  SECRET from becoming reversible because EMAIL scored higher. It cannot
  correct a missing SECRET detection, and overlapping spans can still remove
  useful surrounding text.
- **Unscanned request fields**: see [the scanned surface](api.md#what-gets-anonymized).
