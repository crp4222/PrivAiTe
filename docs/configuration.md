---
description: >-
  PrivAiTe configuration reference: providers, presets, anonymization methods,
  block_entities, custom regex patterns, languages, Docker with a custom
  config, pinned model revisions.
---

# Configuration reference

Everything below goes in your `privaite.yaml` (`python -m privaite --config privaite.yaml`).
The [README quick start](https://github.com/crp4222/PrivAiTe#quick-start) has the minimal
working file; this page covers every knob.

## Unknown keys fail at boot

Every section is validated strictly: a key PrivAiTe does not know refuses
startup and names the offending path, rather than being ignored.

```
ValueError: Invalid config privaite.yaml: pii.block_entites (unknown config key, check the spelling)
```

That typo used to start a proxy with an **empty** policy gate while the operator
believed those types were rejected, and a `gateway: enable:` typo used to leave
the gateway silently off. The single exception is `litellm_params`, which is
handed to LiteLLM as-is and therefore accepts any provider parameter.

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

## Agent CLI gateway

Opt-in native routes for Claude Code (Anthropic Messages, validated live) and
Codex (OpenAI Responses, beta). Gateway routes relay the client's own provider
credentials as-is; PrivAiTe injects and validates no key there. Enable the
[detection cache](#detection-cache-agent-sessions) alongside it. Full setup,
scanned surface and limits: [gateway.md](gateway.md).

```yaml
gateway:
  enabled: false    # default: off, the routes do not exist
  anthropic:
    base_url: "https://api.anthropic.com/v1"
  openai_responses:                            # beta (Codex)
    base_url: "https://api.openai.com/v1"
```

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

### Helping an agent preserve placeholders

Restoration matches the actual placeholder, not the model's interpretation of it.
If the model invents a plausible email instead of copying `<EMAIL_ADDRESS_1>`,
there is no matching value to restore. A cooperative agent can use these
instructions in its system/developer prompt (also available as a
[copyable English file](placeholder-instructions.txt)):

> Preserve privacy placeholders exactly, including in tool arguments and structured
> output. Do not rename them, substitute plausible examples, or infer their original
> values. Keep the surrounding output format valid. Treat `[SECRET]` and masked
> values as unrecoverable. State when required information is missing.

PrivAiTe does not inject this prompt automatically or change agent permissions.
The instruction improves response fidelity; it does not enforce confidentiality
or make an untrusted endpoint obey. If a lab endpoint replaces client system
messages, place the instruction in the effective operator prompt for a cooperative
test, or explicitly test the behavior without it.

Incorrect detection boundaries are a separate issue: copying a placeholder exactly
also restores any extra characters mistakenly included in its original value.
The built-in detectors now preserve matched surrounding quotes/angle brackets for
emails, phones and URLs, common email assignment labels (including line-numbered
tool results), and the `<path>` wrapper around a detected path. Refinement happens
before overlapping detections are merged. Arbitrary punctuation in secrets and
explicit custom-pattern boundaries are not trimmed. Path contents are still
scanned, and false positives or other over-broad spans remain possible.

## Entity overrides (per-type methods)

`anonymization.method` is the default for every type; `entity_overrides` changes
it for named types. **Both shipped configs use this, and it is the one place
where PrivAiTe is deliberately not reversible:**

```yaml
pii:
  anonymization:
    method: "placeholder"
    entity_overrides:
      CREDIT_CARD:
        method: "mask"       # ****************, irreversible
        masking_char: "*"
      SECRET:
        method: "redact"     # [SECRET], irreversible
```

That block is what ships in `config/privaite.example.yaml` and
`config/privaite.openai.yaml`, and the Docker image picks one of those two when
you do not mount your own config, so **the documented Docker quickstart runs
with card numbers masked and secrets redacted**. Concretely: a card number or a
secret detected in your request is destroyed on the way out, the provider sees
`****************` or `[SECRET]`, and the reply comes back with that stand-in
still in place. It is never restored, because the original was never kept. Every
other type keeps the reversible `placeholder` method and does come back.

This is the safer default for the two types whose leak is worst, and it is a
choice, not a law. Delete the `entity_overrides` block (or set those types to
`placeholder`) if you would rather have card numbers and secrets restored in the
reply; keep it, or extend it to more types, if you would rather they never come
back at all. Each override takes `method` (`placeholder`, `fake_replacement`,
`redact`, `mask`) and, for `mask`, `masking_char`.

**Since 0.5.0:** when entity types overlap, `block_entities` takes
precedence, then irreversible methods (`redact`/`mask`), then the configured
overlap resolution. This prevents a higher-confidence EMAIL span from making
an overlapping redacted SECRET reversible. All detected characters remain
covered by the merged span. Both redact and mask have equal priority; neither
can be restored. The detection cache includes the policy in its fingerprint.

An override is about *how* a type is replaced. If a type must not be sent at
all, even as a stand-in, use [`block_entities`](#blocking-specific-pii-types-hard-policy-gate)
instead: that rejects the whole request.

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

The proxy refuses to start if a listed type cannot be emitted by any enabled
detector (for example `US_PASSPORT` under the default `onnx` preset): a block
rule that can never fire would be silently unenforceable. Fix it by removing the
type or enabling a detector that produces it (a `label_mapping` value, a Presidio
entity, or a `custom_patterns` entity type).

## Detection cache (agent sessions)

### Repeated ONNX windows within one request

The ONNX detector reuses predictions for **identical model input windows within
one scrub operation** by default (`pii.detectors.onnx.deduplicate_windows: true`).
Small changes to a log header no longer force identical windows farther down that
log to be evaluated again when several tool results appear in the same request.
The model, window size, overlap, thresholds and scan coverage stay the same.

This is separate from the opt-in cache below. Each outer engine or gateway scrub
call gets its own bounded cache (128 windows). Keys are salted hashes of the
session identity and the exact input tensors, including their shapes and masks.
Only per-token detection labels and confidence scores are kept. Text, input token
IDs, logits, original offsets and reversible maps are not cached. Offsets are
taken from the current text. The cache is cleared at the end of the operation,
including on errors and cancellation; a late worker cannot repopulate it.

In-process integrations inherit this through their engine calls. Separate engine
calls outside a shared scope do not retain predictions between them. Concurrent
requests have separate caches. This optimization helps repeated content, not a
stream of entirely new windows; set `deduplicate_windows: false` for an A/B check.

### Repeated text across requests

Agent CLIs (Claude Code, Codex) resend the entire growing conversation on every
turn, so the proxy re-scans mostly identical bytes: O(n) detector work per turn,
O(n^2) per session. The detection cache remembers the merged detection result
for each exact text leaf, so a resent leaf skips the detectors entirely. On a
real captured 11-turn Codex session with the `onnx` preset, per-turn scrub time
drops to well under a second from turn 2 on, with byte-identical output.

```yaml
pii:
  detection_cache:
    enabled: false      # default: off (see the README threat model)
    max_entries: 4096   # LRU bound
    ttl_seconds: 1800   # entries expire after 30 minutes (swept on the next write)
```

What it stores and what it never stores:

- Stored: salted BLAKE2b hashes of scanned text leaves (the salt is random per
  process and never leaves it), plus span metadata (start, end, entity type,
  score, detector source) for each hash.
- Never stored: the text itself, the matched PII values, the anonymized output,
  or any placeholder mapping. Placeholder numbering stays per-request.

The `block_entities` gate and the anonymizer run on every request, cached or
not, so a cached detection still blocks and still fails closed. A change to any
detector setting changes the cache key fingerprint, so stale results are never
served after a config change.

Why it is off by default: with the cache enabled, PII-derived metadata (hashes,
positions, types) survives in process memory up to `ttl_seconds` after a
request ends, instead of nothing outliving the request. An expired entry is
never served again; it is removed from memory on the first cache write after
its expiry, and the whole cache is cleared at engine shutdown, so only a
process that goes completely idle holds its last (expired, unusable) entries
longer, until that next write or shutdown. The full delta,
including the multi-user dedup timing side channel, is spelled out in the
[README threat model](../README.md#threat-model). Enable it if you use the
[agent CLI gateway](gateway.md) or any client that resends conversation
history; leave it off if the stricter memory posture matters more than latency.

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

By default the whole match is anonymized. When the pattern needs context that
must stay readable, name the part that carries the value: the anonymized span
is then the named group alone, and the surrounding text is left untouched.

```yaml
pii:
  custom_patterns:
    # "api_key=SECRETVALUE" becomes "api_key=<SECRET_1>", not "<SECRET_1>"
    - pattern: "api_key=(?P<value>[A-Za-z0-9_\\-]{16,})"
      entity_type: "SECRET"
```

`value` is the conventional name; with several named groups it wins, otherwise
the first declared group is used. Unnamed `(...)` groups are not markers, so a
pattern without a named group keeps anonymizing the whole match.

Custom patterns are an explicit opt-in: their entity types are never filtered
out by a preset's Presidio entity allowlist.

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

## Built-in recognizers

On top of Presidio's own recognizers, PrivAiTe registers a few of its own:
contextual names, contextual locations, structured secrets and dates. Two things
about them are worth knowing, because neither is obvious from the config:

**The `entities` allowlist does not scope them.** It scopes Presidio's own
recognizers to the types the preset trusts them for; ours are exempt, otherwise
a preset allowlist that happens to omit their type (the location recognizer
emits `LOCATION`, the secret one emits `SECRET`) would register them on every
analyzer and filter them out of every result, silently. So removing a type from
`entities` does **not** stop a built-in recognizer emitting it. Their types are
named in a warning at startup when they fall outside the allowlist.

**Language-specific vocabulary follows the language.** The date recognizer
carries French and German month names and applies each only to that language.
Applying both to every configured language is how an English or Dutch
deployment used to see `11 September` and `11 April` masked while `11 October`
and `11 March` went through: the masked ones are exactly the months spelled like
the German ones. Its numeric, birth-word pattern (`born 15/03/1987`) carries no
month vocabulary and stays active for every language.

To switch one off, name it:

```yaml
pii:
  detectors:
    presidio:
      disabled_recognizers: ["FrenchDateRecognizer"]
```

Accepted names: `ContextualNameRecognizer`, `FrenchDateRecognizer`,
`ContextualLocationRecognizer`, `StructuredSecretRecognizer`. The list is empty
by default, so secret and contextual detection keep working unless you say
otherwise. Disabling one also narrows what `block_entities` considers
enforceable, so a rule that only that recognizer could satisfy is refused at
boot rather than never firing. An unrecognized name is refused when the config
loads, so a typo cannot leave you believing a recognizer is off while it is
still masking.

These recognizers carry vocabulary, and vocabulary follows the language they are
built for: a French deployment gets the French cues, an Italian one the Italian
cues. Configure every language your traffic actually uses (the default is
`["fr", "en"]`), because a cue written in a language you did not configure will
not fire.

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

## Detector device

`device` selects the accelerator per detector. An unknown value is refused at
boot, naming the offending key: onnxruntime does not fail when an execution
provider is missing (it warns and builds a CPU session), so a typo, or `cuda` on
a CPU-only build, used to look like a working accelerator. The startup log now
reports the providers the session really runs on, and warns when a requested one
is unavailable.

| Detector | Accepted values |
|---|---|
| `onnx` | `auto`, `cpu`, `cuda`, `coreml`, `mps` (execution provider names, no index) |
| `mlmodel`, `bert_ner`, `gliner` | `auto`, `cpu`, `cuda`, `mps`, with an optional index such as `cuda:1` |

`auto` never selects CoreML for the ONNX detector: it is slower than CPU at every
measured input size and accumulates compiled-model memory until the host process
is killed. `device: "coreml"` stays available as an explicit opt-in.
