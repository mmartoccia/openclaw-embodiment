# AGENTS.md — Contributing as an Autonomous Agent

This document is for AI coding agents contributing to the OpenClaw Embodiment SDK. Human contributors should read [CONTRIBUTING.md](CONTRIBUTING.md).

If you are an AI agent and this file exists in the repo, read it before touching anything else.

---

## Why this file exists

Agents and humans have different failure modes.

Humans forget to test. Agents over-generate. Humans make one change. Agents change 15 files. Humans leave TODOs. Agents write obvious comments. Humans skip docs. Agents write too much documentation.

This file addresses agent-specific failure modes. It does not repeat CONTRIBUTING.md.

---

## Before you start

You have no memory of previous sessions. Assume nothing.

Read in this order:
1. `ARCHITECTURE.md` — the system boundary. SDK ends at the HTTP POST.
2. `CONTEXT_ENGINE_SPEC.md` — the authoritative spec. If the spec says it, build it. If it doesn't, don't.
3. `git log --oneline -15` — understand what just happened before you arrived.
4. The issue or task you were given. Only that.

If you were not given a specific task, stop. Ask for one.

---

## Scope discipline

Build exactly what is in the task. Nothing else.

**Do not:**
- Add abstractions that have no tests
- Create files not mentioned in the task
- Refactor code you were not asked to change
- Add "while I'm here" improvements
- Implement features from the roadmap that weren't assigned

If you see something broken that is outside your scope, open an issue. Do not fix it.

---

## The SDK boundary

This is a library. It has no opinion about what the agent does with context.

The SDK boundary ends at the HTTP POST (sending SensorContext) and the HTTP receive (getting ResponseBurst back). Everything inside the agent is not your concern.

**Concretely:** No OpenClaw imports. No hardcoded gateway URLs. No references to MEMORY.md, skills, or any specific agent runtime. Config is injected at runtime. If your code would break for someone using a different agent runtime, it doesn't belong here.

---

## Code standards

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full list. Agent-specific additions:

**No comments that restate the code.** If you write `# return result` before `return result`, delete it. The `grain` linter will catch this, but don't make it do your job.

**No `except Exception as e: logger.error(e)` without re-raise.** Handle what you can handle. Let the rest propagate.

**TODOs must be specific.** `# TODO: implement this` is rejected. `# TODO: replace edge-density heuristic with MobileNet person detector (needs ~50MB model)` is accepted. The difference: a human reading it knows exactly what work remains.

**Variable names carry meaning.** `result`, `data`, `response` as top-level function or variable names are flags. Name things after what they are.

---

## Running grain

`grain` is the anti-slop linter for this repo. Run it before every commit.

```bash
pip install -e ~/clawd/tools/grain   # or wherever it's installed
grain check --all
```

No suppressions without a comment explaining why the violation is acceptable. If `grain` flags something and you disagree with the flag, open an issue against grain. Do not add `# grain: suppress` without justification.

---

## Commit protocol

Follow the format already in the log:

```
feat: BLEProximityScanner -- bleak 2.x, ProximityContext, RSSI map
fix: arecord minimum poll is 1s, not configurable poll_duration_ms
spec: v0.2 -- Adaptive Attention Principle
```

**One change per commit.** If you built three things, make three commits. If you're writing "and" in the subject line, split it.

The body should answer: what constraint or discovery drove this? Not what you did — the diff shows that. Why you did it, or what you learned from hardware that changed the approach.

---

## Hardware validation

Any change to a HAL implementation requires a smoke test on actual hardware before the PR is opened.

Smoke test minimum:
- `validate()` returns `True` on the target device
- At least one full capture cycle completes without exception
- Output matches expected format (file size, schema)

Spec-only profiles — implementations written without hardware access — go in a branch named `hardware-unvalidated/<device-name>`. They are not merged to master until validated.

---

## Handoff protocol

At the end of your contribution, create `WHAT-I-DID.md` in the repo root (do not commit it — it's gitignored). Write:

1. What you built (file names, not descriptions)
2. What you stubbed and why (specific blockers, not "needs improvement")
3. What you discovered on hardware that changed the approach
4. What the next agent or human needs to know before continuing

This is not a status report. It is a knowledge transfer. Write it assuming the reader has zero context.

---

## What agents cannot modify

The following files require a linked GitHub issue and explicit human approval before any change:

- `SPEC.md` / `CONTEXT_ENGINE_SPEC.md` — architectural decisions live here
- `PRINCIPLES.md` — the design philosophy
- `ARCHITECTURE.md` — system boundary definitions
- `LICENSE`
- This file (`AGENTS.md`)

If your task requires changing one of these, stop. Comment on the issue with what you found and why you think a change is needed. Do not make the change.

---

## What agents cannot do

- Force push to any branch
- Create branches without a linked issue or task
- Merge their own PRs
- Modify git history (no `--amend` on pushed commits, no rebase of shared branches)
- Add dependencies without noting them in the PR description with justification

---

## A note on this file

This may be the first `AGENTS.md` of its kind in an open source hardware SDK. If you're an agent reading this: the project is designed to accept your contributions thoughtfully, not uncritically. The standards here exist because agentic contribution without guardrails produces low-signal, high-noise commits that erode a codebase over time.

The grain linter, the atomic commit standard, the hardware validation requirement — these are not obstacles. They are what make agentic contribution sustainable at scale.

If you find a gap in this document, note it in your `WHAT-I-DID.md`.
