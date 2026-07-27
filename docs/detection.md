---
description: >-
  How PrivAiTe detects PII: Microsoft Presidio (regex + spaCy NER) and the
  openai/privacy-filter ONNX model, what each engine catches, presets and known
  limitations.
---

# How detection works

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
| Person names (lowercase or single word) | Contextual regex, only after "je m'appelle X", "my name is X", "ich heiße X", "Nom: X", etc. |
| Dates (FR/DE) | Custom regex, "15 mars 1987", "3. März 1990" |

Presidio is fast (tens of ms/request) and produces very few false positives on code, news articles, and technical text. The tradeoff: it misses names that spaCy doesn't recognize (unusual names, single-word names without context) and doesn't detect secrets/passwords.

## OpenAI Privacy Filter: contextual ML model

[OpenAI's open-source PII model](https://openai.com/index/introducing-openai-privacy-filter/) (1.5B params, 50M active, Apache 2.0). Runs locally via ONNX Runtime (~800MB, no PyTorch needed).

| What it adds over Presidio | How |
|---|---|
| Person names (any format, any case) | ML NER, understands context, not just capitalization |
| Passwords and secrets | Detects "SuperSecret2024!", API keys like "sk-proj-..." |
| Account numbers | Detects bank account numbers, policy numbers, etc. |
| Dates (all languages) | ML-based, not limited to FR/DE regex |

The Privacy Filter is slower (~400ms/request) and occasionally flags technical identifiers as account numbers (e.g., "CMD-2024-98765"). It runs as a second pass alongside Presidio, which handles the regex-based entities while the Privacy Filter handles contextual NER.

## Why two engines?

Neither is perfect alone:

- **Presidio alone** misses names that spaCy doesn't recognize, and can't detect secrets. But it has very few false positives.
- **Privacy Filter alone** misses some names in credit/list formats, and doesn't have regex validators for IBAN/credit card checksums.
- **Both together** cover each other's blind spots. Presidio handles structured formats with validation, the Privacy Filter handles context-dependent PII.

## What's NOT detected by default

The default `onnx` preset does detect personal addresses (as `LOCATION`) and personal URLs (as `URL`) through the Privacy Filter model, and replaces them. What stays off by default are Presidio's broad recognizers for those types, because they cause heavy false positives:

- **Generic place names (the Presidio LOCATION recognizer):** "Paris" or "London" on their own aren't PII, and spaCy flags ordinary words ("Kubernetes", "Saturday") as locations. The `onnx` preset keeps this recognizer off and relies on the model's context-aware address detection instead. PrivAiTe's own cue-based location patterns ("elle habite à X", "lives in X", "domicilié à X") do fire under every preset: they require a residence cue, so they do not carry spaCy's false-positive rate. Any recognizer PrivAiTe registers, and any `custom_patterns` type, is exempt from a preset's entity allowlist; the allowlist only scopes Presidio's own recognizers.
- **The Presidio URL regex:** it matches code like `logging.getLogger` because `.ge` is a valid TLD. The `onnx` preset keeps it off, and the model still catches genuine personal URLs.

On the `light` preset (Presidio only), addresses and URLs are not detected. Secrets and passwords are detected only by the `onnx` preset. Any recognizer can be turned on in the YAML config.

## Known limitations

- **Single-word names** from spaCy are dropped (too many false positives). Caught by contextual patterns ("Nom: X") or the `onnx` preset.
- **Lowercase names** need intro patterns ("je m'appelle X"). The `onnx` preset catches them without patterns.
- **Informal dates** ("last Tuesday", "il y a deux ans") are not detected.
- **Unscanned request fields**: see [the scanned surface](api.md#what-gets-anonymized).
