# Discoverability pass, consolidated summary

PR: https://github.com/crp4222/PrivAiTe/pull/2 (branch `chore/discoverability`, no functional change)

## Files created or modified

| Path | Change |
|---|---|
| `llms.txt` | Rewritten per llmstxt.org: summary blockquote, user-question phrasings, absolute links to docs, benchmarks, integrations |
| `llms-full.txt` | New, 305 lines: self-contained doc an LLM can ingest in one fetch (how it works, install, presets, endpoints, benchmark numbers, limitations) |
| `README.md` | Plain subtitle under the title, PyPI downloads badge, intro keyword dedupe (each key phrase once), one early line naming the three run modes. Structure, tables and numbers untouched |
| `AGENTS.md` | New canonical agent guide, renamed from `CLAUDE.md` (git rename preserved), em-dashes removed, rules intact |
| `CLAUDE.md` | Removed from the repo and gitignored: it is now a local, uncommitted personal file that imports `AGENTS.md` |
| `.github/dependabot.yml` | Weekly grouped updates: pip (minor+patch grouped, majors individual, limit 5) and github-actions (all grouped, limit 3) |

## Applied outside the working tree

GitHub topics (cap is 20, repo was full): swapped `chatgpt-privacy` (2 repos on its topic page) for `pii-detection` (415 repos). Wishlist items rejected on measured grounds: `data-anonymization` (118) duplicates `anonymization` (612), `pii-masking` (60) duplicates `pii-redaction` (182), `ai-privacy` (43) too small to displace anything.

## Drafts pending human validation

The distribution drafts (awesome-list entries, one Reddit post, one Show HN post) were produced but kept LOCAL and gitignored (`drafts/`), not committed: publishing marketing drafts in a public PR would defeat their purpose. They live on the maintainer's machine, marked never-auto-submit.

## Decisions and omissions

- release-please evaluated and not adopted: it would replace the hand-written Keep-a-Changelog prose with generated notes for little gain in a solo, manually released project.
- Dependabot labels omitted: the repo has none of the labels they would reference (verified with `gh label list`).
- `llms-full.txt` is 305 lines against a 150-250 target: the extra sections answer real user questions (irreversible modes, blocking types); kept.

## Blocked by missing rights

Nothing. The token had write access for topics; everything else stayed in the branch.
