---
description: >-
  PrivAiTe's declarative policy layer: define your own PII types with regex
  patterns, choose each type's fate (restore, fake, mask, redact), and
  hard-block what must never leave. Deterministic, dry-runnable, refused at
  startup when unenforceable.
---

# Your policy, your types

PrivAiTe's answer to "what leaves the machine" has two halves. The statistical
half is the detector suite: benchmarked, best-effort, with a
[measured recall](../README.md#benchmark) that will never be 100%. The
declarative half is yours. Three configuration mechanisms, documented
separately in the [configuration reference](configuration.md), together form
something bigger than three settings: a policy, written in YAML, applied
deterministically, without retraining anything or waiting for a release.

| The question | The mechanism |
|---|---|
| What counts as sensitive here, beyond the built-in types? | [`custom_patterns`](configuration.md#custom-regex-patterns) |
| What happens to each type on the way out? | [`entity_overrides`](configuration.md#entity-overrides-per-type-methods) |
| What must never leave at all, even as a stand-in? | [`block_entities`](configuration.md#blocking-specific-pii-types-hard-policy-gate) |

This page is the narrative for how they compose; each option's full reference
stays on the configuration page.

## A worked example

One block, reading top to bottom like the policy it is:

```yaml
pii:
  # What counts as sensitive here. Internal identifiers the built-in types
  # cannot know about, declared as regex. The named group says "the value is
  # here": the label stays readable, the value alone is replaced.
  custom_patterns:
    - pattern: "KD-\\d{6}"
      entity_type: "CUSTOMER_ID"
    - pattern: "api_key=(?P<value>[A-Za-z0-9_\\-]{16,})"
      entity_type: "SECRET"

  # What happens to each type on the way out. The default is a reversible
  # placeholder, restored in the reply. The overrides are the exceptions.
  anonymization:
    method: "placeholder"
    entity_overrides:
      CREDIT_CARD:
        method: "mask"       # ****************, irreversible
        masking_char: "*"
      SECRET:
        method: "redact"     # [SECRET], irreversible, the original is destroyed

  # What must never leave at all, even as a placeholder. A request carrying
  # one of these is rejected with 400; the error names the type, never the value.
  block_entities: ["US_SSN", "CUSTOMER_ID"]
```

In prose: customer IDs are recognized and never leave the network at all, a
request carrying one is refused outright. API keys are secrets and secrets are
destroyed on the way out, not stored for restoration. Card numbers go out
masked and stay masked in the reply. Everything else the detectors find is
pseudonymized and restored, so the application still receives usable answers.

Note the interlock between the first and last mechanism: the `CUSTOMER_ID`
pattern is what makes the `CUSTOMER_ID` block rule enforceable. Remove the
pattern and the proxy [refuses to start](#what-makes-it-enforceable) rather
than keep a rule that can never fire.

## What makes it enforceable

Four properties, each there because a policy you cannot verify is a promise,
not a policy.

- **Deterministic.** A regex matches or it does not; a listed type is blocked
  or it is not. The same request produces the same outcome every time. There is
  no model judging your policy and no score drifting with the surrounding
  context. (One honest boundary to this claim, below.)
- **Dry-runnable before you trust it.** The opt-in
  [`/v1/pii/inspect` endpoint](verify.md) replays the whole policy on text you
  choose: it returns the detections, the exact string the provider would have
  seen, and `would_block`, the types your `block_entities` rules would have
  rejected. Nothing is forwarded, logged, or counted. Turning
  `deanonymization.enabled` off gives the same proof on live traffic.
- **Never silently unenforceable.** The proxy refuses to start if a
  `block_entities` type cannot be emitted by any enabled detector (for example
  `US_PASSPORT` under the default `onnx` preset). A dead rule is treated as a
  configuration bug, not a decoration.
- **Never silently overridden.** `custom_patterns` types are exempt from the
  presets' Presidio entity allowlist, so a preset change cannot quietly switch
  your own types off.

The policy also runs everywhere the engine runs: plain message text, text parts
of multimodal messages, and tool-call JSON arguments. In
[gateway mode](gateway.md), even the agent prompt fields that are deliberately
relayed verbatim (the Anthropic `system` field, the Responses `instructions`
field) are still scanned for blocked types, so a `block_entities` rule stops a
request whose forbidden type sits in the one place nothing rewrites.

## Where determinism ends

The claim above is precise: the policy layer is deterministic *given what the
detectors report*. For a regex-defined type — a custom pattern, or a
checksummed Presidio type like `CREDIT_CARD` or `IBAN_CODE` — detection itself
is deterministic too, so the whole chain is. For an ML-detected type like
`PERSON`, the rule is deterministic but the detection feeding it is
statistical: a `block_entities: ["PERSON"]` rule fires on every person the
detector finds and inherits the detector's
[measured recall](../README.md#benchmark) for the ones it does not. If a type
absolutely must be caught, give it a structure the regex layer can hold onto.

## What a regex cannot say

This layer expresses **structured** policy: identifiers, references,
`key=value` shapes, anything with a describable form. It cannot express
"anything that looks like a medical condition" — that is a semantic category,
and semantic detection is the ML model's job, at its benchmarked, best-effort
recall. The promise here is *your structured policy, applied
deterministically*, not *write any policy in plain language*. For how this
differs from the guard models that do take plain-language policies, and what
they give up for it, see
[the comparison page](comparison.md#guard-models-answer-a-different-question).
