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

[OpenAI's open-source PII model](https://openai.com/index/introducing-openai-privacy-filter/) (1.5B params, 50M active, Apache 2.0). Runs locally via ONNX Runtime (about 900MB, no PyTorch needed).

| What it adds over Presidio | How |
|---|---|
| Person names (any format, any case) | ML NER, understands context, not just capitalization |
| Passwords and secrets | Detects "SuperSecret2024!", API keys like "sk-proj-..." |
| Account numbers | Detects bank account numbers, policy numbers, etc. |
| Dates (all languages) | ML-based, not limited to FR/DE regex |

The Privacy Filter adds model-inference cost and occasionally flags technical identifiers as account numbers (e.g., "CMD-2024-98765"). It runs concurrently with Presidio, which handles structured formats while the Privacy Filter handles contextual NER. Cost depends on input length; the benchmark reports the combined engine latency.

Since 0.7.1, PrivAiTe slices the full tokenization into overlapping ONNX windows
itself. Newer Transformers and tokenizers releases could silently return only the
first window when asked for overflow, leaving the rest of a long request unscanned.
Coverage checks now reject omitted tokens or source text before returning any
detections. Tokenizers that add special tokens are refused at startup.

The model is fetched into the Hugging Face cache. Since huggingface_hub 1.33, that
cache keeps the model and its weights file in two different folders, which ONNX
Runtime refuses to load. PrivAiTe then hard-links the two files side by side under
`models--openai--privacy-filter/privaite-onnx/` in the same cache: no second
download and no extra disk space, so a cache filled in advance for offline use keeps
working. A copy is made only on a filesystem that refuses hard links, and deleting
the model from the cache deletes that folder with it.

### ONNX variants

Two exports of the model are supported, selected with `onnx_variant`
([configuration](https://github.com/crp4222/PrivAiTe/blob/main/docs/configuration.md#onnx-variant)).
They hold the same 4-bit weights; `q4` computes in fp32 and `q4f16` in fp16. On
the comparative benchmark, CPU execution provider:

| Variant | Recall | Recall (strict) | False positives | Gretel recall | One 1,024-token window | Peak memory |
|---|---|---|---|---|---|---|
| `q4` (default) | 85.2% | 81.7% | 2 / 14 | 62.9% | 776 ms | 6.3 GB |
| `q4f16` | 84.9% | 81.0% | 2 / 14 | 62.9% | 1,118 ms | 5.3 GB |

`q4` runs a window 1.4x to 1.6x faster than `q4f16` (1,024 and 512 tokens), and
the whole benchmark finished faster with it in every run. The window figures
come from one process that alternates the variants, so machine load cannot favour
one of them; the full benchmark time also includes Presidio and moves with the
load, so the seconds are in the
[full report](https://github.com/crp4222/privaite-bench/blob/main/ONNX_VARIANTS.md)
and the ratio is what to read.

246 of the 256 anonymized outputs (benchmark, clean documents, Gretel finance
corpus, long code and log inputs) are identical between the two. The 10 that
differ are span boundaries moving in both directions; no labelled value is lost,
and `q4` catches one more. Measured on an Apple Silicon CPU; x86 and GPU sessions
were not measured, and fp16 is where a GPU is expected to favour `q4f16`.

The int8 `quantized` export is faster still but lost 13 labelled values for 8
gained, doubled the false positives and peaked at 8.8 GB, so it is not
recommended.

## Why two engines?

Neither is perfect alone:

- **Presidio with PrivAiTe's recognizers** catches specific credential formats, but misses unfamiliar names and secrets without those structural cues.
- **Privacy Filter alone** misses some names in credit/list formats, and doesn't have regex validators for IBAN/credit card checksums.
- **Both together** cover each other's blind spots. Presidio handles structured formats with validation, the Privacy Filter handles context-dependent PII.

## Presets

| Preset | What runs | Recall\* | False positives | Latency | Secrets |
|--------|-----------|----------|-----------------|---------|---------|
| `onnx` (default) | Presidio + Privacy Filter | **85.2%** | 2 / 14 | ~459ms | **yes** |
| `light` | Presidio + built-in rules | 62.7% | 3 / 14 | ~81ms | structured formats |
| `max` | onnx + GLiNER | higher OOD | more | ~0.7s | **yes** |

\*Span recall on the [AI4Privacy benchmark](https://github.com/crp4222/PrivAiTe#benchmark): 120 real documents (458 PII items, labeled by 10 independent auditor agents and cross-checked against the dataset's own mask) across DE, EN, FR, IT, plus 14 clean documents for false positives. The latencies are means per corpus document from that local run, not large agent-request latency guarantees. `max` adds GLiNER (trained on data independent of AI4Privacy): on out-of-distribution corpora it raises recall by several points at the cost of more false positives and a torch dependency (`pip install 'privaite[gliner]'`); with it selected but not installed, the proxy fails at startup with an install hint rather than silently degrading.

**`onnx`** combines contextual recognition with structured rules. **`light`** uses Presidio and the same structured-secret rules; it has no contextual Privacy Filter model, and needs no model download beyond the spaCy language models. It misses names that no cue introduces when the text is not in the first configured language: `privaite verify --preset light` reports the English name of its own demo as leaked.

> **Footgun:** do not pin `detectors.presidio.entities` to a short allowlist on the `light` path. It restricts detection to only those types and roughly halves recall (to ~36%). Leave `entities` unset; the proxy logs a warning at startup if it detects a low-recall configuration.

## What's NOT detected by default

The default `onnx` preset does detect personal addresses (as `LOCATION`) and personal URLs (as `URL`) through the Privacy Filter model, and replaces them. What stays off by default are Presidio's broad recognizers for those types, because they cause heavy false positives:

- **Generic place names (the Presidio LOCATION recognizer):** "Paris" or "London" on their own aren't PII, and spaCy flags ordinary words ("Kubernetes", "Saturday") as locations. The `onnx` preset keeps this recognizer off and relies on the model's context-aware address detection instead. PrivAiTe's own cue-based location patterns ("elle habite à X", "lives in X", "domicilié à X") do fire under every preset: they require a residence cue, so they do not carry spaCy's false-positive rate. Any recognizer PrivAiTe registers, and any `custom_patterns` type, is exempt from a preset's entity allowlist; the allowlist only scopes Presidio's own recognizers. Use [`disabled_recognizers`](configuration.md#built-in-recognizers) to switch one off. Their cues are vocabulary, so each one only fires under the language it is written in: configure every language your traffic uses.
- **The Presidio URL regex:** it matches code like `logging.getLogger` because `.ge` is a valid TLD. The `onnx` preset keeps it off, and the model still catches genuine personal URLs.
- **spaCy dates on code:** the English model also labels code as dates (`connect(api_key`, `f.write(json.dumps(entry`). A span that holds code (a call, a snake_case identifier, brackets, an assignment) is cut there, and only the pieces holding a digit are kept, so the code is not rewritten while every number the span covered stays covered. Any other span is kept as spaCy reported it.

The `light` preset has no contextual Privacy Filter model. 0.5.0 adds the same structured-secret rules to every preset that enables Presidio. Broad password recognition still requires an ML detector; these rules do not make `light` equivalent to `onnx`.

## Structured credentials and overlapping types

The new rules supplement detection; they never short-circuit the NLP engines.
They recognize common assignment names (`api_key`, `apiKey`, `access_token`,
`refresh_token`, `auth_token`, `client_secret`, `password`, `passwd`,
`smtp_secret`, `presented_key`) and underscore-prefixed environment variants
such as `OPENAI_API_KEY` and `DB_PASSWORD`, case-insensitively. They also
recognize passwords in `scheme://user:password@host` and plaintext
`Authorization: Bearer ...` headers.

Three more shapes are covered, because the model missed them on some `.env`
files:

- an upper-case name ending in `TOKEN`, `SECRET` or `KEY` at the start of a
  line (`API_TOKEN=`, `export JWT_SECRET=`, `OPENAI_KEY=`), when the value looks
  opaque: 16 characters or more with a letter and a digit, and not a path or a
  variable reference. Those three words are ordinary identifiers everywhere
  else (`token=None`, `cache_key=users`, `next_page_token=...`), so the rule is
  case-sensitive and anchored to the line. Names ending in `PASS` or `_PWD`
  (`DB_PASS`, `MYSQL_PWD`) take any value; `PWD` alone is the shell's working
  directory and is left alone. The line may start with what an agent's tools
  add: the line numbers of a file read (`12:`, `12<tab>`, `00012|`), a diff
  marker, a compose list item (`- NAME=value`), or `export`, `env`, `ENV`,
  `ARG`;
- a URI password with no user name, as in `redis://:password@host`;
- the key formats providers document for themselves, wherever they appear and
  whatever the field is called: OpenAI and Anthropic `sk-`, Stripe
  `sk_live_`/`sk_test_`/`rk_live_`, GitHub `ghp_` and `github_pat_`, GitLab
  `glpat-`, Slack `xox?-`, AWS access key ids (`AKIA`), Google API keys
  (`AIza`) and Hugging Face `hf_`. Prefix and length are both required, so
  `sk-` on its own or `hf_hub_download` is not a key.

Since 0.7.1, these rules also cover credentials passed as command-line options:
`--password`, `--passwd`, `--api-key`, `--client-secret`, the token options and
`--mot-de-passe`, with an optional prefix (`--db-password`), and quoted or
unquoted values separated by whitespace or `=`. What follows the option in help
text or prose is not a value and is left alone: a metavariable (`<value>`,
`PASSWORD`), a variable reference (`$DB_PASSWORD`, which stands for the secret
and must survive for the command to work) or a plain word ("use `--password` to
set it").
Commands inside JavaScript or JSON strings are scanned with their quotes and
backslashes decoded, up to two string layers. Detected values are replaced at
their original source positions, so restoration preserves the command's exact
syntax, including escaped quotes. Other encoded payloads remain outside these
rules.

Since 0.7.1, a credential these rules identified is also treated as a secret
wherever the same request repeats it afterwards, whatever surrounds it. An agent
that was handed a restored password repeats it bare, in its reasoning, in the
next command or in a tool output, and re-detecting it there is not reliable.
This is deliberately narrow:

- only values matched by a structured rule (a credential field, a command-line
  option, URI userinfo, a bearer header), never a secret that only a model
  labelled, because one mislabelled identifier would then be rewritten across
  the whole request;
- only values of 8 characters or more that contain a digit and are not a bare
  number: after a credential field, a plain word or an identifier is as often
  code (`api_key=api_key`) as a value, so `changeme` is protected where the
  rule sees it and not propagated;
- exact copies, in the texts that follow the first detection: the same value
  written with different escaping is a different string.

Disabling `StructuredSecretRecognizer` disables the propagation with it.

For quoted assignments, only the value is replaced: `api_key="demo-only"`
becomes `api_key="[SECRET]"` with the shipped redaction policy. Escaped quotes
are included in the value. Bare values extend to whitespace; punctuation may
be part of a password and is not stripped. Use quotes when a comma or brace
must be unambiguously preserved as syntax.

On `.env` files and shell exports, the contextual model's spans do not always
follow the values: a password runs on through the host and into the next line,
or a fragment of a variable name is flagged by itself. Before merging, a model
span of type `SECRET`, `URL` or `EMAIL_ADDRESS` is therefore fitted to the
line: it is cut where the next line assigns an upper-case name (behind the same
line numbers and markers as above), a piece that stays inside such a name is
dropped, a span that starts in the name is cut to the value, and a password in a URI loses the `:` before it and stops at the `@`
that closes it. With the shipped config,
`DATABASE_URL=postgres://shop:demo-pass-4821@db.internal:5432/shop` goes out as
`DATABASE_URL=postgres://shop:[SECRET]@db.internal:5432/shop`. Only structure is
given back, never a character of a value, and a name that itself looks like a
token (one long word with a digit) is not treated as a name. Lower-case names are left as the detector
returned them: `user=tag@example.com` is also one valid address.

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
  useful surrounding text: when the model labels a whole connection URI as a
  URL, the union with its password is redacted as one secret and the host goes
  with it. The model also flags harmless values on `.env` files (`DEBUG=true`,
  a working directory): the name is kept, the value is replaced.
- **Unscanned request fields**: see [the scanned surface](api.md#what-gets-anonymized).
