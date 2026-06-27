# PrivAiTe vs Presidio, LLM Guard, and LiteLLM PII masking

PrivAiTe is a drop-in, self-hosted LLM proxy that redacts PII before it reaches the provider and restores it in the reply. Unlike Microsoft Presidio (a library you assemble), Protect AI LLM Guard, or LiteLLM's built-in Presidio guardrail, it also redacts PII inside **tool-call arguments and multimodal content**, the part flat-text scrubbers miss, with zero telemetry.

This is local pseudonymization, not anonymization, and detection is best-effort. You remain the data controller. See the [threat model](../README.md#threat-model).

## Feature comparison

| | PrivAiTe | Microsoft Presidio | Protect AI LLM Guard | LiteLLM PII guardrail |
|---|---|---|---|---|
| Shape | Drop-in OpenAI-compatible proxy | Python library | Python library | Gateway feature |
| Setup | Point your client at it | Assemble it yourself | Assemble it yourself | Config in the LiteLLM proxy |
| Reversible (restore on reply) | Yes | Manual | Yes (anonymize/deanonymize) | Limited |
| Redacts PII in tool-call arguments | Yes | No | No | No |
| Redacts PII in multimodal text | Yes | OCR only | No | No |
| Streaming de-anonymization | Yes | n/a | n/a | n/a |
| Secrets and passwords | Yes (ONNX preset) | No | Yes | Partial |
| Detection engine | Presidio + local ONNX model | Presidio | Own scanners | Presidio |
| Self-hosted, zero telemetry | Yes | Yes | Yes | Yes |

Presidio is excellent and PrivAiTe builds on it. The point of this table is not that PrivAiTe detects better than Presidio in isolation; it is that PrivAiTe is the ready-to-run proxy around it that also covers the structured and multimodal cases, and restores the original values on the way back.

## The tool-call gap, measured

The [reproducible benchmark](https://github.com/crp4222/privaite-bench) places the same PII inside a tool-call argument and measures how much survives. A vanilla Presidio setup, which is the engine behind LiteLLM's guardrail and most flat-text tools, leaves about **99%** of it exposed, because it only scans message text. PrivAiTe removes everything it detects from the tool call. On plain-text recall over 120 real documents labeled by independent auditors, the full PrivAiTe preset leads the field (about 84% recall vs about 70% for the Presidio baseline), and its tool-call protection is **100% vs 0.6%**.

## When to pick which

- **Pick Presidio** if you want a detection library to embed in your own pipeline and you will handle the proxying, reversal, and tool-call cases yourself.
- **Pick LLM Guard** if you want a broader prompt-security toolkit (prompt injection, toxicity) and PII is one part of it.
- **Pick LiteLLM's guardrail** if you already run the LiteLLM proxy and only need flat message-text PII handling.
- **Pick PrivAiTe** if you want a drop-in proxy that protects the whole prompt-egress path, including tool-call arguments and multimodal content, reversibly, with zero telemetry, and works with any OpenAI-compatible client.

## Reproduce it

```bash
pip install privaite
python solutions/ai4privacy_loader.py   # in the privaite-bench repo
python -m solutions.compare
```
