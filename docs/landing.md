# PrivAiTe landing copy

Draft copy for the project website. Plain language on purpose. No compliance
promises, no "anonymized" claims, because the output is pseudonymized and that
distinction matters legally.

---

## Hero

**Keep personal data out of your LLM calls.**

PrivAiTe is a local proxy that sits between your app and the model provider. It
finds the personal data in a request, swaps it for stand-ins before anything
leaves your machine, and puts the real values back in the reply. It works across
message text, tool-call arguments, and multimodal content, which is where most
tools stop looking. Nothing phones home.

[Get started](#install) | [See the benchmark](https://github.com/crp4222/privaite-bench) | [GitHub](https://github.com/crp4222/PrivAiTe)

---

## The problem

People paste real customer names, emails, contracts, and API keys into chatbots
and coding agents every day. In 2025, security researchers reported that a large
share of corporate AI usage includes sensitive data, and that around 40 percent
of files sent to AI tools contain personal data. Once that text reaches a
third-party model, you no longer control where it is stored, logged, or used.

Most existing tools only scrub the visible chat message. Modern traffic is not
just chat. It is agents calling tools with your data in the arguments, documents
and images attached to a prompt, and CLIs that read files and environment
variables where secrets live. That is the gap PrivAiTe was built for.

---

## How it works

1. You point your OpenAI-compatible client at PrivAiTe instead of the provider.
2. On the way out, it detects personal data and replaces each item with a
   stand-in. A placeholder like `<PERSON_1>`, or a realistic fake, your choice.
   The mapping stays in memory on your machine for the length of the request.
3. On the way back, it restores the real values in the response, including inside
   tool-call arguments, so your application receives what it expected.

The model only ever sees the stand-ins. You can prove it: PrivAiTe ships a
benchmark and the engine is open source, so you can read exactly what it sends.

---

## What other tools miss

- **Tool-call arguments.** When an agent calls a function, the PII sits in the
  JSON arguments, not the chat text. Flat-text scrubbers leave it untouched.
  PrivAiTe scrubs it and restores it.
- **Multimodal content.** Text parts inside a multimodal message are handled too.
- **Agent and CLI egress.** The same engine protects the whole prompt path, not
  only a chat box.
- **Zero telemetry.** Detection runs on your machine. PrivAiTe does not call
  home, and there is no hosted dashboard collecting your traffic.
- **Reversible by design.** You get the real values back, so the output stays
  usable. Redaction that throws data away breaks the reply.
- **Drop-in.** Swap one base URL. It works with any OpenAI-compatible client.

---

## Proof, not promises

We built a comparison on real documents, not toy examples. The corpus is 120 real
documents whose personal data was labeled independently, then cross-checked. Each
solution runs on the same data.

The headline: on the same personal data placed inside a tool-call argument, a
vanilla Presidio setup (the engine behind several drop-in tools) leaves about 99
percent of it exposed, because it never looks there. PrivAiTe removes everything
it detects from the tool call. On plain text recall, the full PrivAiTe preset
leads the field.

The benchmark is public and reproducible. Run it yourself.

---

## Two ways to run it

- **Standalone proxy.** Run it next to your app, point your client at it. Best
  when you want one place to protect every model and client.
- **Open WebUI filter.** Drop the filter into Open WebUI and it runs in process,
  no separate service.
- **LiteLLM guardrail.** Coming, for teams already running the LiteLLM proxy.

---

## Presets

- **onnx (default).** The full suite. Detects classic personal data plus secrets
  and passwords. Downloads a small model on first run.
- **light.** Faster, zero false positives, classic personal data only. Good when
  you want the lowest latency and no model download.

---

## Install

```bash
pip install privaite
python -m spacy download en_core_web_lg
python -m privaite
```

Then point your client at `http://localhost:8400/v1`. Docker and a full config
reference are in the README.

---

## What PrivAiTe is not

Honesty is part of the pitch, so here is the fine print in plain words.

- It performs **pseudonymization, not anonymization**. The mapping is reversible
  by design, so the data it protects remains personal data under the law. Use it
  to reduce exposure and minimize what you send, not as a guarantee.
- **Detection is best-effort.** It is statistical and never catches 100 percent.
  A name it does not recognize can still reach the provider. Treat the output as
  best-effort.
- **You stay the data controller.** PrivAiTe is a tool you run. It does not take
  responsibility for your compliance program, and it makes no compliance claim.
- The software is provided as is, under an open-source license.

---

## FAQ

**Does it send my data anywhere?** No. Detection runs locally and there is no
telemetry. The only outbound request is the one you make to your chosen model
provider, carrying stand-ins instead of real values.

**Does this make me GDPR or HIPAA compliant?** No tool can do that for you. It
helps reduce the personal data you expose to a third party, which can support
your own compliance work. You remain responsible.

**Which models does it work with?** Anything with an OpenAI-compatible API, plus
local models through Ollama and others.

**What about latency?** The light preset adds very little. The onnx preset adds
more because it runs a model. You choose per use case.

**Is it really open source?** Yes. Read the engine, run the benchmark, open an
issue.
