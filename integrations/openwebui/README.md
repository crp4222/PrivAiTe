# PrivAiTe filter for Open WebUI

`privaite_filter.py` is an Open WebUI Filter Function. It anonymizes PII in the
outgoing request and restores it in the reply, in-process, so you do not have to
run a separate proxy. It covers message text, tool-call arguments, and multimodal
text.

## Install

1. Make sure `privaite` is available to Open WebUI. The filter declares
   `requirements: privaite>=0.2.4`, so Open WebUI installs it automatically from
   PyPI. You can also install it into the Open WebUI environment yourself
   (`pip install privaite`).
2. In Open WebUI: Admin Panel, Functions, "+", paste the contents of
   `privaite_filter.py`, save, then enable it.
3. Open the function's valves to pick the preset and the languages. The default
   `onnx` preset detects everything including secrets; switch to `light` for the
   fast, zero false-positive path.

## Blocking specific PII types

By default every detected PII item is pseudonymized and the request goes through.
To make some types a hard stop instead, set the `block_entities` valve to a
comma-separated list of PII types (e.g. `US_SSN,CREDIT_CARD`). A message containing
any listed type is refused before it reaches the model, and Open WebUI shows the
error; it names the type(s), never the value. Types not listed are still masked as
usual. This needs a `privaite` build that supports `pii.block_entities`; on an
older one the filter refuses the request rather than silently forwarding the PII.

## Notes

- The filter runs Presidio and spaCy inside Open WebUI and downloads the spaCy
  models for your languages on first use (en_core_web_lg alone is ~560MB). The
  default `onnx` preset also downloads the Privacy Filter model on first use, so
  the first request after enabling it can be slow. Pre-installing the models, or
  using the `light` preset, avoids that.
- This is local pseudonymization, not guaranteed anonymization. See the Threat
  model in the main README.
- For a lighter Open WebUI instance, run PrivAiTe as a standalone proxy and point
  your connection at it instead of using this filter.
