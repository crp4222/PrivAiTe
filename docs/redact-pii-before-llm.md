---
description: >-
  How to redact PII before sending prompts to OpenAI, Anthropic or any LLM API:
  the three places PII actually hides, the three ways to remove it (in your own
  code, in a gateway guardrail, or in a proxy), what each one costs, and how to
  check the result on the wire rather than in the reply.
---

# How to redact PII before sending prompts to an LLM

Calling a model API sends a request body off your machine. Whatever is in that
body reaches the provider: names, emails, card numbers, the contents of files
your code read, and anything an agent put in a tool call. This page is about
removing that before it leaves, and about how to check that it actually did.

PrivAiTe is one of the three options below. It is not the right answer for
everyone, and the page says where it is not.

## First, know where PII actually hides

Most tools scan `messages[].content` and stop there. In an application that
only chats, that is enough. In anything that uses tools, it is not, because the
same values live in two more places:

- **Tool-call arguments.** The model emits `{"to": "marie@example.com"}` as a
  JSON string inside `tool_calls[].function.arguments`. A text-only scrubber
  forwards it untouched, because it is not message text.
- **Tool output.** When an agent reads a file, the file comes back into the
  conversation as a tool result and is resent with every following turn. This is
  how `.env` contents and log lines reach a provider without anyone pasting
  them: [measured here on real Claude Code and Codex sessions](https://github.com/crp4222/PrivAiTe/blob/main/docs/agent-leak-measurement.md).

Whatever you pick below, check it against all three. A tool that scores well on
plain text can still forward everything in the other two.

## Option 1: do it in your own code

Call [Microsoft Presidio](https://github.com/microsoft/presidio) (or any
detector) on the text before you build the request. Free, no new component to
run, and you keep full control of what counts as sensitive.

It works well when there is exactly one place in your code that talks to the
model. It stops working when there are several: every new call site is a path
someone has to remember to route through the scrubber, and the structured fields
above are easy to forget because they do not look like user text. There is also
no restore step, so the model's reply comes back full of placeholders and your
application has to put the real values back itself.

Pick this if your surface is small and you want no extra infrastructure.

## Option 2: a guardrail inside a gateway you already run

If you already route model traffic through a gateway, it probably has a hook for
this. [LiteLLM](https://docs.litellm.ai/docs/proxy/guardrails/quick_start) ships
a Presidio guardrail, [Kong's AI Gateway](https://developer.konghq.com/plugins/ai-custom-guardrail/)
calls any HTTP guardrail service, and the cloud gateways have their own.

The advantage is that there is no new hop: the thing is already on the path, so
you configure rather than deploy. The limits are the guardrail's own. Check
whether it covers tool-call arguments, and whether it restores values on the way
back or only deletes them.

Pick this if a gateway is already in your path.

## Option 3: a proxy in front of the provider

Put an OpenAI-compatible proxy between your client and the provider, and point
the client's base URL at it. Nothing in your application changes, every call
site is covered because they all go through the same address, and clients you do
not control (a chat UI, a coding agent) are covered too.

This is what PrivAiTe does. What it adds over the two options above:

- values are replaced with **reversible** placeholders and put back in the
  reply, so your application still sees real data;
- tool-call arguments and tool output are scanned, not just message text;
- native routes for Claude Code and Codex, so agent traffic is covered without
  the agent knowing.

The cost is a component to run and latency on every request. Detection runs
locally, so nothing is sent anywhere for analysis.

Other proxies exist in this shape. [Occludra Gateway](https://github.com/aisecuritygateway/aisecuritygateway)
is simpler and also blocks prompt injection, but per its documentation it
replaces values irreversibly and covers message text only.
[LLM Guard](https://github.com/protectai/llm-guard) was the reference
open-source scanner suite and is archived as of July 2026. A detailed
side-by-side, including where PrivAiTe loses, is on the
[comparison page](https://github.com/crp4222/PrivAiTe/blob/main/docs/comparison.md).

Pick this if several clients talk to the model, or if any of them is an agent.

## None of this is a guarantee

Detection is statistical. Published recall for PrivAiTe's default preset is
84.9% span-level on a public corpus, with the
[harness and labels open so the number can be rerun](https://github.com/crp4222/privaite-bench).
Per type it ranges from 100% on emails, cards, IBANs, phones and IPs down to
42% on URLs and 61% on organisation names. Any tool that tells you nothing gets
through is either not measuring or not telling you.

Treat the result as pseudonymisation and transfer minimisation, not
anonymisation, and decide per type what must never come back at all. In
PrivAiTe that is `entity_overrides` with `redact` or `mask`, and `block_entities`
for values that must not leave even as a placeholder.

## Check it on the wire, not in the reply

The common way to check is to send a test prompt and read the answer. That shows
what came back, which is not what went out. A span that covers only part of a
value looks clean in a restored reply and still put the rest of it on the wire.

Capture the outbound request body instead. With PrivAiTe:

```
privaite verify
```

It runs a throwaway provider on 127.0.0.1, sends the same agent-shaped request
straight to it and then through a real proxy, and prints both request bodies. It
exits non-zero if a planted value reached the wire. With any other tool, point it
at a local endpoint that logs what it receives and compare the two bodies
yourself. More ways to audit, including on your own data:
[verification](https://github.com/crp4222/PrivAiTe/blob/main/docs/verify.md).
