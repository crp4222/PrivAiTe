---
description: >-
  PrivAiTe configuration reference: providers, presets, anonymization methods,
  block_entities, custom regex patterns, an allow list for strings that only
  look like PII, languages, Docker with a custom config, pinned model revisions.
---

# Configuration reference

Everything below goes in your `privaite.yaml` (`python -m privaite --config privaite.yaml`).
The [README quick start](https://github.com/crp4222/PrivAiTe#quick-start) runs the shipped
`config/privaite.openai.yaml`; this page covers every knob. The minimal working file is:

```yaml
providers:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: ${OPENAI_API_KEY}
pii:
  enabled: true
  preset: onnx    # or "light": faster, no model download, classic PII only
```

With that file every detected type gets a reversible placeholder. The shipped
configs add two per-type exceptions on top, a masked `CREDIT_CARD` and a redacted
`SECRET`: see [entity overrides](#entity-overrides-per-type-methods).

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

### Request parameters

Since 0.7.3, the OpenAI-compatible endpoints forward the OpenAI parameters of each endpoint
(`temperature`, `tools`, `response_format`...) and answer anything else with a
400 `unsupported_parameter` that names the field (a field sent as `null` is
dropped instead). A field such as `api_base` or
`extra_body` is a call setting for LiteLLM, not a provider parameter: forwarded
from the client, it could send the request and your API key to another server,
or replace the anonymized messages. To pass a provider-specific field, list it
on that provider:

```yaml
  - model_name: "local-llama"
    litellm_params:
      model: "ollama/llama3.1"
      api_base: "http://localhost:11434"
    forward_params: ["top_k"]
```

These fields reach the provider unscanned. Call settings (`api_base`,
`base_url`, `api_key`, `extra_body`, `extra_headers`, the `aws_`, `azure_` and
`vertex_` families...) are refused at startup: set them in `litellm_params`.

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

Placeholder-shaped text already in the request (a `<PERSON_1>` in a test fixture,
a template, a tool definition or the agent's system prompt) is never issued as a
placeholder: numbering skips it, so the provider never sees one placeholder with
two meanings and the restore never writes a real value into it. When such a
string only appears after its number went to a detected value (in a later
message), it is sent as a fresh placeholder of its own and restored to itself.

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
before overlapping detections are merged. A model span on a `.env` line is
fitted to the value: the upper-case variable name, the `:` and `@` around a URI
password and the following line are given back
([details](detection.md#structured-credentials-and-overlapping-types)).
Punctuation inside a secret and explicit custom-pattern boundaries are not
trimmed. Path contents are still scanned, and false positives or other
over-broad spans remain possible.

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
    enabled: false      # default: off (see the threat model)
    max_entries: 4096   # LRU bound
    ttl_seconds: 1800   # entries expire after 30 minutes (swept on the next write)
```

What it stores and what it never stores:

- Stored: salted BLAKE2b hashes of scanned text leaves (the salt is random per
  process and never leaves it), plus span metadata (start, end, entity type,
  score, detector source) for each hash, including the spans an
  [`allow_list`](#allowing-known-non-pii-values) entry let through.
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
[threat model](threat-model.md). Enable it if you use the
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

## Allowing known non-PII values

Some strings have the shape of PII and are nothing of the kind. OpenSSH names
its algorithms `chacha20-poly1305@openssh.com`, so every detector reads an
`sshd_config` as a list of e-mail addresses, and an agent editing it through
the proxy no longer sees which algorithms are set. Turning `EMAIL_ADDRESS` off
would let real addresses through; `allow_list` exempts values instead:

```yaml
pii:
  allow_list:
    - pattern: "[A-Za-z0-9.+_-]+@(openssh\\.com|libssh\\.org)"   # regex, matched in full
    - pattern: "(openssh\\.com|libssh\\.org)"                     # the hostnames seen inside them
    - value: "KexAlgorithms"                                     # exact string
      entity_types: ["ORGANIZATION"]   # optional: only when detected as one of these
```

An entry is either a `value` (compared exactly, case included) or a `pattern`
(a regex the detected text must match in full). What the list does, and what
it can never do:

- Detection and merging run exactly as without the list. A merged span is then
  let through only when every detection under it is listed, each on its own
  type and text. A span that merely contains a listed value stays redacted,
  and a listed e-mail address never unmasks a name another detector found
  inside it, with `merge_strategy: union` or `intersection` alike. That is why
  the recipe above lists the hostnames too: Presidio's URL pattern sees
  `openssh.com` inside each name, and that detection has to be listed as well.
- Without `entity_types`, an entry never applies to `SECRET` nor to a type in
  `block_entities`. Name the type to exempt one of those on purpose; the proxy
  says so at startup.
- A value the list lets through is never remembered as a known credential, so
  it is not propagated to its other copies in the request.
- Refused at startup: an entry with both or neither of `value` and `pattern`,
  a regex that does not compile, one that matches the empty string, and one
  that matches a sample e-mail, name, phone, card, IBAN, SSN, IP address, date
  or URL in full (`.+@.+` is a type switch, not a value exemption). A detection
  over 1,024 characters is never exempted.
- Prefer `value` for a finite list of names. A pattern lets through everything
  it matches: with the SSH pattern above, a real mailbox at `openssh.com` would
  pass too.

[`/v1/pii/inspect`](verify.md) returns what the list let through under
`allowed`, and under `not_allowed` the detections that kept a listed value
redacted, so an entry can be checked against your own files before you trust
it. The list applies wherever the engine scans: message content, tool-call
arguments, the agent CLI gateway and the detection cache (its key changes with
the list). The LiteLLM guardrail and the Open WebUI filter do not expose it yet.

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

The order matters. Pattern recognizers and the contextual rules run for every
listed language, but spaCy's statistical NER is kept only for the first one:
a model reading text in another language produced false positives, such as
"résumer" taken for a person. Put the language of most of your traffic first.
With the default `["fr", "en"]`, English text gets no spaCy names; the `onnx`
preset finds them with its own model, `light` does not ("I am Marie Dupont"
is missed there). An English-first list fixes that for `light`, at the cost of
the English model's guesses on French text ("Peux-tu" or "Python" as
organizations).

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
still masking. Disabling `StructuredSecretRecognizer` also turns off the
propagation of the credentials it identifies (see
[detection](detection.md#structured-credentials-and-overlapping-types)).

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
- [`crp4222/privaite-pii-minilm`](https://huggingface.co/crp4222/privaite-pii-minilm/commit/e7ea9dd38cd6d03198aeba57174a1d6d3a95618d): `e7ea9dd38cd6d03198aeba57174a1d6d3a95618d` (the opt-in `minilm` and `ultrafast` presets)

When changing a detector's `model_name`, also set `revision` to a commit SHA
from that model's repository. A default SHA from a different repository fails to
load instead of silently using different weights. `revision: null` intentionally
follows the mutable default branch and is not reproducible.

## ONNX variant

The `onnx` preset runs the Privacy Filter through ONNX Runtime, and
`onnx_variant` defaults to `q4`. The other supported choice is `q4f16`. Both
hold the same 4-bit weights: `q4` computes in fp32 and `q4f16` in fp16, which
CPUs do not run natively, so `q4` is the faster one on CPU and `q4f16` is the
one to try on a GPU session. The measured difference is in
[detection](https://github.com/crp4222/PrivAiTe/blob/main/docs/detection.md#onnx-variants).

```yaml
pii:
  preset: onnx
  detectors:
    onnx:
      onnx_variant: q4f16   # default: q4
```

Only the configured variant is downloaded: about 917 MB for `q4`, 809 MB for
`q4f16`. An install upgraded from a release that defaulted to `q4f16` downloads
`q4` once on its first start; set `onnx_variant: q4f16` to keep using the model
already in the cache. A host with no network access has to do one or the other
before upgrading: fetch `q4` into its Hugging Face cache, or pin `q4f16`.
Without either, the proxy refuses to start rather than run without its
detector. The Docker image bakes the default. To bake the other one,
build with `--build-arg ONNX_VARIANT=q4f16` and set the same `onnx_variant` in
the mounted config, otherwise the first start downloads the default.

## PrivAiTe MiniLM model (opt-in)

Two presets swap the Privacy Filter for PrivAiTe's own model,
[`crp4222/privaite-pii-minilm`](https://huggingface.co/crp4222/privaite-pii-minilm):
a multilingual MiniLM trained for this proxy, half the Privacy Filter's
download (a quarter under `ultrafast`) and several times faster, on CPU and more
so on the Apple Neural Engine. Nothing changes unless you set one of them: the default stays
`onnx`. What they detect and how they score is in
[detection](https://github.com/crp4222/PrivAiTe/blob/main/docs/detection.md#privaite-minilm-opt-in).

```yaml
pii:
  preset: minilm      # Presidio (spaCy) + the MiniLM model on CPU, any platform
  # preset: ultrafast # the MiniLM model on the Neural Engine + Presidio without spaCy (macOS)
```

| Preset | Presidio | Model | Extra install |
|---|---|---|---|
| `minilm` | with its spaCy models, as under `onnx` | ONNX fp32 on CPU | none |
| `ultrafast` | blank spaCy pipelines (`nlp_engine: blank`) | Core ML on the Neural Engine | `pip install 'privaite[ane]'`, macOS only; no spaCy model to download |

Both keep the Presidio allowlist, the structured-secret rules and every other
setting of `onnx`. `ultrafast` is meant for agent CLIs on a Mac (Claude Code,
Codex through the [gateway](#agent-cli-gateway)), which resend the whole
conversation every turn. Off macOS, or without `coremltools`, it refuses to
start and names the fix.

Each part can also be chosen on its own, and an explicit value wins over the
preset:

```yaml
pii:
  preset: ultrafast
  detectors:
    minilm:
      backend: onnx_int8     # onnx (default), onnx_int8, coreml
      score_threshold: 0.0   # the evaluated argmax decoding; raise to mask less
      # label_mapping: drop a type to stop masking it, rename one to change its placeholder
    presidio:
      nlp_engine: spacy      # spacy (default), blank
```

`backend`: `onnx` is the fp32 export and gives exactly the trained model's
spans; `onnx_int8` is 4 times smaller and about twice as fast on CPU, for a
point of recall or less; `coreml` runs on the Neural Engine.
`nlp_engine: blank` loads no spaCy model: the pattern recognizers and
PrivAiTe's own recognizers still run, but spaCy's named entities are gone, so a
name with no cue around it is left to the model. It is the faster choice on
long requests, and the one to avoid with `preset: light`, which has no model to
fall back on.

The model's types are `PERSON`, `CONTACT` (emails, phones, URLs, user names),
`LOCATION` (addresses and places), `ORGANIZATION`, `DATE_TIME`, `AGE`,
`DEMOGRAPHIC`, `GOV_ID`, `FINANCIAL`, `NETWORK_ID`, `RECORD_ID`, `SECRET` and
`OTHER`. Presidio still emits its own types for the same values (an email is
`EMAIL_ADDRESS` there), and a type listed in `block_entities` wins any overlap,
so blocking `EMAIL_ADDRESS` keeps working; list the model's types (`GOV_ID`,
`CONTACT`...) to block what only the model finds.

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
| `minilm` | no `device`: `backend` picks the runtime (`onnx` and `onnx_int8` on CPU, `coreml` on the Neural Engine) |
| `mlmodel`, `bert_ner`, `gliner` | `auto`, `cpu`, `cuda`, `mps`, with an optional index such as `cuda:1` |

`auto` never selects CoreML for the ONNX detector: it is slower than CPU at every
measured input size and accumulates compiled-model memory until the host process
is killed. `device: "coreml"` stays available as an explicit opt-in.

## Outbound connections

The only request PrivAiTe makes on your behalf is the one to your provider.
Two downloads happen once, on a first run outside Docker: the detection model
from the Hugging Face Hub (about 900 MB; about 490 MB under `minilm` and 250 MB
under `ultrafast`) and any missing spaCy language model. The Docker image
carries the default model and spaCy models; it carries the MiniLM model only
when built with `--build-arg MINILM=onnx` (or `onnx_int8`).

Several dependencies contact a third party on their own, and since 0.7.2
PrivAiTe turns each of them off (on an earlier version, export the variables
below yourself):

| Dependency | What it did by default | How it is turned off |
|---|---|---|
| ONNX Runtime 1.29 and later | Uploads telemetry (runtime, hardware and model metadata, a hashed device identifier) to `mobile.events.data.microsoft.com` | `ORT_DISABLE_TELEMETRY=1`, and `disable_telemetry_events()` when the session is created |
| LiteLLM | Fetches its price table from `raw.githubusercontent.com` at import | `LITELLM_LOCAL_MODEL_COST_MAP=True` (the bundled table is used) |
| Hugging Face Hub | Asks the Hub about model files on every start, cached or not | A pinned `revision` is read from the cache without a request; `HF_HUB_DISABLE_TELEMETRY=1` |
| tldextract (used by Presidio) | Downloads the public suffix list from `publicsuffix.org` | Its bundled list is used |

None of these carried request text. The three environment variables are set
when `privaite` is imported, before the libraries read them, and the Docker
image sets them too. A value you export yourself is kept, so
`LITELLM_LOCAL_MODEL_COST_MAP=False` brings the remote price table back.

Two limits. A host process that loaded one of these libraries before importing
`privaite` (Open WebUI with the filter, a LiteLLM proxy with the guardrail) has
already read its environment: set the variables on that process. And an
unpinned `revision` (a branch name) still asks the Hub what it points at; set
`HF_HUB_OFFLINE=1` for a process that must never do that.
