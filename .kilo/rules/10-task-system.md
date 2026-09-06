# Task System Rules

Forgejo Issues at `forgejo.creatidy.com/BioMedical-IT/scarcity-router` are the
durable task source of truth for bugs, features, tech debt, governance and
research. Docs hold strategy and current-state context, not operational
queues. GitHub Issues are a read-only mirror and never the task source.

## Execution Start

- Implementation starts only from an issue Adrian explicitly selected by
  number, URL, or unambiguous title, or one the active task explicitly
  authorizes creating.
- If no issue is selected, stop and say no active issue is selected.
- Fetch the actual issue body before planning, then summarize the title and
  acceptance criteria.
- Do not infer active work from issue age, title order, links, labels, or
  prior conversation memory.

## Issue Quality

Agent-created issues must include:

- **Goal**
- **Why**
- **Scope**
- **Acceptance Criteria**
- **Constraints**
- **Links**

Issue bodies must be implementation-grade, not copied plan fragments or
scratch notes. Acceptance criteria must be verifiable. If scope changes
materially, update the issue body before continuing.

## One Issue, One Implementation

- One issue drives one coherent implementation, one feature branch and one
  Forgejo PR.
- Use only default labels when useful (`bug`, `enhancement`,
  `documentation`); do not create task-management label taxonomies.
- Do not create duplicate or speculative follow-up issues; update or link an
  existing issue instead.

## Done and Closure

- `Done` means acceptance criteria are satisfied and the change is integrated
  into `develop`.
- `Done` does not mean promoted or released to `main`; promotion is
  human-controlled and not part of ordinary task completion.
- Link the PR to its parent issue and report any Forgejo update that could
  not be performed.
