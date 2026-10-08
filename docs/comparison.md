---
description: >-
  PrivAiTe vs Microsoft Presidio, Protect AI LLM Guard and LiteLLM's Presidio
  guardrail: recall, false positives and tool-call PII coverage, measured on a
  reproducible benchmark. Plus what the gateways built for agent traffic
  (PasteGuard, maskit, CosyRedactGateway, LangSmith and others) state in their
  own docs, and why guard models answer a different question.
---

# PrivAiTe vs Presidio, LLM Guard, and LiteLLM PII masking

PrivAiTe is a drop-in, self-hosted LLM proxy that redacts PII before it reaches the provider and restores it in the reply. Compared with the three tools measured below (Microsoft Presidio, a library you assemble; Protect AI LLM Guard; LiteLLM's built-in Presidio guardrail), it also redacts PII inside **tool-call arguments** and multimodal content, reversibly and with zero telemetry: 100% of the PII placed in a tool-call argument survives in both measured guardrails. That is a statement about those tools, not about the field: several gateways built for agent traffic now handle tool calls too, and they are listed [further down](#gateways-built-for-agent-traffic).

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
| Self-hosted | Yes | Yes | Yes | Yes |

Presidio is excellent and PrivAiTe builds on it. The point of this table is not that PrivAiTe detects better than Presidio in isolation; it is that PrivAiTe is the ready-to-run proxy around it that also covers the structured and multimodal cases, and restores the original values on the way back.

## The tool-call gap, measured

The [reproducible benchmark](https://github.com/crp4222/privaite-bench) runs the REAL competitor integrations, configured at their genuine best, and places the same PII inside a tool-call argument and a multimodal text part. Measured results on 120 real documents labeled by independent auditors:

| | Recall (flat text) | Tool-call leak | Multimodal leak |
|---|---|---|---|
| PrivAiTe `onnx` (default) | **85.2%** | **14.8%** | **14.8%** |
| LLM Guard (Anonymize) | 76.9% | 100% | 100% |
| LiteLLM Presidio guardrail | 70.3% | 100% | 29.7% |

LiteLLM's guardrail does scrub multimodal text parts (hence its low multimodal leak), and LLM Guard's DeBERTa model actually out-recalls it on flat text. But neither parses tool-call JSON, so **100%** of the same PII survives inside a tool-call argument, even values they detect in plain text; PrivAiTe removes everything it detects from the tool call (**100% vs 0%** tool-call protection).

Contamination note, in the competitors' favor and still insufficient: LLM Guard's detection model is fine-tuned on the exact dataset family behind this corpus, so its 76.9% is an optimistic upper bound, while PrivAiTe's default model did not train on it. On an out-of-distribution corpus (non-AI4Privacy), the PrivAiTe onnx stack holds ~84% recall while the AI4Privacy-tuned model drops to ~62%: see [OOD_COMPARISON.md](https://github.com/crp4222/privaite-bench/blob/main/OOD_COMPARISON.md).

## Gateways built for agent traffic

These projects sit in front of coding agents or LLM APIs like PrivAiTe does. None of them was run in the benchmark above: the table reports what each project's own README or docs state, as read on 6 October 2026, not a measurement. "Not stated" means the page read does not say, not that the feature is missing.

| Project | Shape | Detection, as stated | Reply restored | Tool-call arguments |
|---|---|---|---|---|
| [PasteGuard](https://github.com/sgasser/pasteguard) | Local proxy, browser extension, coding agents | Checksums and format checks, plus GLiNER for names and places; secrets (API keys, private keys, JWTs, passwords, connection strings) | Yes ("restores supported placeholders in the response"); streaming responses are handled | Not stated |
| [maskit](https://github.com/xiaYuTian11/maskit) | Local gateway for coding tools, browser extension | 21 rule categories (7 on by default); optional local ONNX model for names, organizations and addresses, off by default | Yes, streaming for OpenAI and Anthropic | Restored; signed thinking blocks keep their placeholders |
| [CosyRedactGateway](https://github.com/CassiopeiaCode/CosyRedactGateway) | Single-file credential gateway (OpenAI Chat Completions, Responses, Anthropic Messages) | High-entropy credential detection, structured rules, Gitleaks-compatible rules; emails, bank cards, ID numbers | Yes, in responses and supported streams | Restored, including streamed deltas |
| [packyme/privacy-filter](https://github.com/packyme/privacy-filter) | Go library with HTTP and gRPC services | Regex for structured PII; gitleaks rules, contextual regex and entropy for secrets; no names, by design | No: placeholders "are irreversible" | Not applicable (a text filter) |
| [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI) | Proxy for AI-assisted pentesting (Claude Code, OpenAI-compatible clients) | Local Ollama model for hostnames, organization names and credentials in prose; regex for IPs, hashes, tokens and API keys | Yes | Not stated |
| [LangSmith LLM Gateway](https://docs.langchain.com/langsmith/llm-gateway-redaction) | Managed gateway policy | PII and secrets policy | Yes, streamed or not | Structured arguments are scanned; arguments that arrive as a single JSON string are skipped, as are system and developer prompts |

So looking inside tool calls and restoring a streamed reply are no longer what sets PrivAiTe apart from every alternative. What it brings, stated about itself only:

- **Tool-call arguments are parsed as JSON and scrubbed value by value**, including when they arrive as a single JSON string, which is how the OpenAI chat format sends them.
- **A contextual model for names and addresses with a published recall** on a public corpus, and a [benchmark anyone can rerun](https://github.com/crp4222/privaite-bench).
- **A wire-level measurement on real Claude Code and Codex sessions**, misses included ([write-up](agent-leak-measurement.md)).
- **The same engine three ways**: proxy, Open WebUI filter, LiteLLM guardrail. It fails closed: a detection error blocks the request.

## When to pick which

- **Pick Presidio** if you want a detection library to embed in your own pipeline and you will handle the proxying, reversal, and tool-call cases yourself.
- **Pick LLM Guard** if you want a broader prompt-security toolkit (prompt injection, toxicity) and PII is one part of it.
- **Pick LiteLLM's guardrail** if you already run the LiteLLM proxy and only need flat message-text PII handling.
- **Pick a guard model** (Llama Guard, OpenAI's gpt-oss-safeguard, Mistral's Shieldstral) if your question is "is this content acceptable?" rather than "what personal data is in it, and how do I get it back?": they classify, they do not redact. [Why the two don't substitute for each other](#guard-models-answer-a-different-question).
- **Pick PasteGuard** if you also want a browser extension for web chat, a dashboard of what was masked, or routing sensitive requests to a local model.
- **Pick CosyRedactGateway** if credentials are your concern, including random-looking tokens with no known prefix and no `password=` label, or if you want a single-file gateway.
- **Pick maskit** if your data is Chinese-language (ID cards, licence plates, USCC codes) and you want a console and a browser extension.
- **Pick packyme/privacy-filter** if you need millisecond, irreversible redaction embedded in a Go gateway and do not need names.
- **Pick DontFeedTheAI** for pentest engagements, where hostnames, IPs and hashes are the sensitive data.
- **Pick LangSmith's gateway** if you already run LangSmith and want a managed policy.
- **Pick PrivAiTe** if you want measured detection, names and addresses included, on the whole egress path (message text, tool-call arguments, tool results, multimodal text), reversibly, with zero telemetry, and a benchmark you can rerun to check it.

## Guard models answer a different question

The open guard models (Llama Guard, OpenAI's gpt-oss-safeguard, Mistral's
Shieldstral as of August 2026) take a document and a moderation policy — the
recent ones accept the policy as plain language at inference time — and return
a verdict: acceptable or not, as a probability. That is content-safety
classification, and it differs from what PrivAiTe does in two structural ways,
not two incidental ones.

**A verdict has no spans.** A guard model reports *that* a document crosses
the policy, not *where* the offending value sits. Without character offsets it
cannot replace a value with a placeholder, cannot restore it in the reply, and
its only enforcement is to pass or reject the request whole. PrivAiTe's entire
mechanism — replace on the way out, restore on the way back, tool-call
arguments included — depends on spans, which is why its detectors are span
extractors and a verdict model cannot slot in as one.

**A judged policy is probabilistic; a declared one is not.** A plain-language
policy is flexible, and the model judging it returns a score: the same
borderline text can land on either side of the threshold depending on the
surrounding context, and the model cards themselves note reduced reliability
on long or obfuscated inputs. PrivAiTe's [policy layer](policy.md) is
deterministic rules over span detections: the same request always produces the
same outcome, the whole policy is [dry-runnable](verify.md) before you trust
it, and a block rule that can never fire refuses to start instead of silently
never firing. When the audience is an auditor rather than a demo, that
difference is the product.

Honest in both directions: a guard model expresses semantic policies a regex
never will ("anything that describes self-harm"), and covers harmful-content
moderation, which PrivAiTe deliberately does not do at all. The two compose
rather than compete — a guard model deciding what is acceptable, PrivAiTe
deciding what leaves for the provider.

## Reproduce it

```bash
pip install privaite
python solutions/ai4privacy_loader.py   # in the privaite-bench repo
python -m solutions.compare
```
