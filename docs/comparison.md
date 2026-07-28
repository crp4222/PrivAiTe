---
description: >-
  PrivAiTe vs Microsoft Presidio, Protect AI LLM Guard and LiteLLM's Presidio
  guardrail: recall, false positives and tool-call PII coverage, measured on a
  reproducible benchmark.
---

# PrivAiTe vs Presidio, LLM Guard, and LiteLLM PII masking

PrivAiTe is a drop-in, self-hosted LLM proxy that redacts PII before it reaches the provider and restores it in the reply. Unlike Microsoft Presidio (a library you assemble), Protect AI LLM Guard, or LiteLLM's built-in Presidio guardrail, it also redacts PII inside **tool-call arguments** (the part every tested competitor misses: 100% of the PII placed in a tool-call argument survives in both measured competitors) and multimodal content, reversibly and with zero telemetry.

This is local pseudonymization, not anonymization, and detection is best-effort. You remain the data controller. See the [threat model](../README.md#threat-model).

## Feature comparison

| | PrivAiTe | Microsoft Presidio | Protect AI LLM Guard | LiteLLM PII guardrail |
|---|---|---|---|---|
| Shape | Drop-in OpenAI-compatible proxy | Python library | Python library | Gateway feature |
| Setup | Point your client at it | Assemble it yourself | Assemble it yourself | Config in the LiteLLM proxy |
| Reversible (restore on reply) | Yes | Manual | Yes (anonymize/deanonymize) | Limited |
| Redacts PII in tool-call arguments | Yes | No | No | No |
| Redacts PII in multimodal text | Yes | OCR only | No | Yes (text parts) |
| Streaming de-anonymization | Yes | n/a | n/a | n/a |
| Secrets and passwords | Yes (ONNX preset) | No | Yes | Partial |
| Detection engine | Presidio + local ONNX model | Presidio | Own scanners | Presidio |
| Self-hosted, zero telemetry | Yes | Yes | Yes | Yes |

Presidio is excellent and PrivAiTe builds on it. The point of this table is not that PrivAiTe detects better than Presidio in isolation; it is that PrivAiTe is the ready-to-run proxy around it that also covers the structured and multimodal cases, and restores the original values on the way back.

## The tool-call gap, measured

The [reproducible benchmark](https://github.com/crp4222/privaite-bench) runs the REAL competitor integrations, configured at their genuine best, and places the same PII inside a tool-call argument and a multimodal text part. Measured results on 120 real documents labeled by independent auditors:

| | Recall (flat text) | Tool-call leak | Multimodal leak |
|---|---|---|---|
| PrivAiTe `onnx` (default) | **84.9%** | **15.1%** | **15.1%** |
| LLM Guard (Anonymize) | 76.9% | 100% | 100% |
| LiteLLM Presidio guardrail | 70.3% | 100% | 29.7% |

LiteLLM's guardrail does scrub multimodal text parts (hence its low multimodal leak), and LLM Guard's DeBERTa model actually out-recalls it on flat text. But neither parses tool-call JSON, so **100%** of the same PII survives inside a tool-call argument, even values they detect in plain text; PrivAiTe removes everything it detects from the tool call (**100% vs 0%** tool-call protection).

Contamination note, in the competitors' favor and still insufficient: LLM Guard's detection model is fine-tuned on the exact dataset family behind this corpus, so its 76.9% is an optimistic upper bound, while PrivAiTe's default model did not train on it. On an out-of-distribution corpus (non-AI4Privacy), the PrivAiTe onnx stack holds ~84% recall while the AI4Privacy-tuned model drops to ~62%: see [OOD_COMPARISON.md](https://github.com/crp4222/privaite-bench/blob/main/OOD_COMPARISON.md).

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
