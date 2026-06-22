# PrivAiTe filter for Open WebUI

`privaite_filter.py` is an Open WebUI Filter Function. It anonymizes PII in the
outgoing request and restores it in the reply, in-process, so you do not have to
run a separate proxy. It covers message text, tool-call arguments, and multimodal
text.

## Install

1. Make sure `privaite` is available to Open WebUI. The filter declares
   `requirements: privaite==0.2.1`, so Open WebUI installs it automatically once
   the package is on PyPI. Until then, install it into the Open WebUI environment
   yourself (`pip install privaite`).
2. In Open WebUI: Admin Panel, Functions, "+", paste the contents of
   `privaite_filter.py`, save, then enable it.
3. Open the function's valves to choose the preset (`light` or `onnx`) and the
   languages.

## Notes

- The filter runs Presidio and spaCy inside Open WebUI and downloads the spaCy
  models for your languages on first use (en_core_web_lg alone is ~560MB), so the
  first request after enabling it can be slow. Pre-installing the models avoids
  that.
- This is local pseudonymization, not guaranteed anonymization. See the Threat
  model in the main README.
- For a lighter Open WebUI instance, run PrivAiTe as a standalone proxy and point
  your connection at it instead of using this filter.
