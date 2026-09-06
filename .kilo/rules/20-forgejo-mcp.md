# Forgejo MCP Rules

`forgejo-mcp` is the canonical Forgejo operation identity for
`forgejo.creatidy.com/BioMedical-IT/scarcity-router`. It performs issue,
PR, review, comment, label and branch/PR operations.

Use local files, `git`, and search tools for repository inspection. Do not
read repository contents through Forgejo MCP when the repository is available
locally.

## Use Boundaries

- Use `forgejo-mcp` narrowly for the exact issue, PR, comment, review or
  label operation the task requires.
- Do not list all repositories, issues, PRs, branches, organization
  resources, Projects, or milestones unless Adrian explicitly asks.
- When working on one issue or PR, read only that object and directly linked
  objects.
- Report only Forgejo writes that actually succeeded; never claim an issue,
  PR, review or label update that did not complete.

## Coder and Reviewer Intent

One MCP identity performs operations, but the Coder/Reviewer intent
separation is preserved conceptually:

- A session that implements a task must not review its own result or approve
  its own PR.
- Never auto-approve or auto-merge. Workers, reviewers and orchestrators
  never merge; a human is the merge gate.
- Do not post reviews or create follow-up issues unless the review workflow
  or the task explicitly authorizes the write.

## Failure Handling

- If a Forgejo MCP operation fails, name the blocked operation and provide
  the manual URL or command when useful.
- Forgejo MCP failure must NOT cause an automatic fallback to GitHub writes
  or any broader write mechanism. Proceed with local work only when the
  Forgejo-backed state change is optional for the current task.
- Before reporting a Forgejo operation as blocked, search available tools for
  the exact operation needed (for example `update_issue` for an issue body
  correction; do not add workaround comments when the body itself can be
  edited).

## GitHub Is a Mirror Only

GitHub (`github.com/creatidy/scarcity-router`) is an automatic secondary
mirror. Agent-side issues, PRs, reviews, comments, branch management and
normal repository mutations belong to Forgejo. Do not create or mutate GitHub
Issues/PRs/branches/tags or push to GitHub unless Adrian explicitly requests
a GitHub-specific operation.

GitHub may still be used for:

- read-only mirror verification;
- public/discovery links;
- tools that can only read GitHub;
- explicit owner-requested GitHub checks.

It is never the operational task source of truth.

## Branching Policy

- Branch from `develop`; open PRs that target `develop`.
- Never merge or promote to `main` automatically; `main` promotion is a
  human-controlled, explicitly authorized operation.
