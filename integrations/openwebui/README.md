# PrivAiTe filter for Open WebUI

`privaite_filter.py` is an Open WebUI Filter Function. It anonymizes PII in the
outgoing request and restores it in the reply, in-process, so you do not have to
run a separate proxy. It covers message text, tool-call arguments, and multimodal
text.

## Install

1. Make sure `privaite` is available to Open WebUI. The filter declares
   `requirements: privaite>=0.4.1`, so Open WebUI installs it automatically from
   PyPI. You can also install it into the Open WebUI environment yourself
   (`pip install privaite`), which is the recommended path: see the first-run
   memory note below. Note that Open WebUI only resolves that requirement when
   the package is absent; an environment that already has an older `privaite` is
   not upgraded for you.
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

- **Open WebUI's own task calls bypass every filter, including this one.** Title
  generation, tag generation and follow-up suggestions are issued by Open WebUI's
  Task Model, which calls the provider directly without running any Filter
  function's `inlet`. Verified live: the chat message itself is scrubbed, the
  task call carrying the same text is not. That is Open WebUI's architecture, not
  something this filter can intercept, and it applies to any redaction filter.
  If those calls must not leave your network, point the Task Model at a local
  model (Admin, Settings, Interface, Task Model) or disable the features.

- **First use can get the container OOM-killed. Pre-install instead.** The
  filter runs Presidio and spaCy inside Open WebUI and downloads the spaCy models
  for your languages on first use (en_core_web_lg alone is ~560MB); the default
  `onnx` preset downloads the Privacy Filter model on top of that. Installing and
  loading all of it inside a running Open WebUI process is a real memory spike on
  top of Open WebUI's own footprint. This is not theoretical: the one external
  user who reported back on this integration had to bake `privaite` and the large
  spaCy model into his Open WebUI image beforehand, because the first request
  after enabling the filter was killed for running out of memory. Do the same:

  ```dockerfile
  FROM ghcr.io/open-webui/open-webui:main
  RUN pip install --no-cache-dir privaite>=0.4.1 && \
      python -m spacy download en_core_web_lg
  ```

  Then the first request only loads what is already on disk. Using the `light`
  preset (no ONNX model) or raising the container's memory limit also helps, and
  running PrivAiTe as a standalone proxy avoids the problem entirely.
- This is local pseudonymization, not guaranteed anonymization. See the Threat
  model in the main README.
- For a lighter Open WebUI instance, run PrivAiTe as a standalone proxy and point
  your connection at it instead of using this filter.
