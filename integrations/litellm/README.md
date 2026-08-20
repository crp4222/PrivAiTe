# PrivAiTe guardrail for LiteLLM

`privaite_guardrail.py` is a LiteLLM custom guardrail. It runs PrivAiTe's engine
in-process inside the LiteLLM proxy: it anonymizes PII in the outgoing request and
restores it in the response, including inside **tool-call arguments and the legacy
function_call**, which LiteLLM's built-in Presidio guardrail does not touch.
Chat streaming responses are restored too (Responses API streaming restore is not
yet implemented).

## Install

1. Make `privaite` available to the LiteLLM proxy: `pip install "privaite>=0.4.2"`
   in the same environment (or image) as LiteLLM.
2. Put `privaite_guardrail.py` next to your `config.yaml`.
3. Reference it by dot-notation in `config.yaml`:

```yaml
guardrails:
  - guardrail_name: "privaite"
    litellm_params:
      guardrail: privaite_guardrail.PrivaiteGuardrail
      mode: [pre_call, post_call]   # pre_call anonymizes input, post_call restores output
      default_on: true              # run on every request; or false to opt in per request
      preset: "onnx"                # "onnx" (full, detects secrets) or "light" (fast)
      languages: "en,fr"            # comma-separated spaCy languages
      deanonymize: true             # restore the real values in the response
      # block_entities: ["US_SSN", "CREDIT_CARD"]  # reject these types outright (see below)
```

If `default_on` is `false`, opt in per request by adding `"guardrails": ["privaite"]`
to the request body.

## Blocking specific PII types

By default every detected PII item is pseudonymized and the request goes through.
To make some types a hard stop, list them under `block_entities` (a YAML list or a
comma-separated string). A request containing any listed type is rejected with a
`400` before anything reaches the model; the error names the type(s), never the
value. Types not listed are still masked as usual. This needs a `privaite` build
that supports `pii.block_entities`; on an older one the guardrail refuses to start
with block rules set, rather than silently forwarding the PII.

## Notes

- The first request after enabling it is slow: importing `privaite` pulls in
  Presidio and spaCy (and, with `preset: "onnx"`, downloads the Privacy Filter
  model). Use `preset: "light"` to skip the ONNX model.
- This is local pseudonymization, not anonymization, and detection is best-effort.
  See the Threat model in the main README.
- If you do not want these dependencies inside your LiteLLM proxy image, run
  PrivAiTe as a standalone proxy instead and point a LiteLLM deployment at it.

## What it covers that the built-in Presidio guardrail does not

LiteLLM's Presidio guardrail scrubs message text only. PrivAiTe's engine also
anonymizes PII inside tool-call arguments and multimodal text, and restores all of
it (content, `tool_calls`, and `function_call`) on the way back. See the
[head-to-head comparison](../../docs/comparison.md).
