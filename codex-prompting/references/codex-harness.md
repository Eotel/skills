# Codex Harness Reference

Verify version-sensitive behavior with current OpenAI documentation and the
installed Codex CLI.

## Durable mechanics

- Codex loads applicable `AGENTS.md` instructions by directory. Keep repository
  rules durable, contextual, and closer to the paths they govern.
- Sandbox and approval policy are runtime configuration. A prompt can describe
  required access but cannot grant permissions the harness does not provide.
- Use repository-native commands and editing conventions. Do not restate generic
  coding-agent behavior unless the project needs a deliberate exception.
- Long tasks may be compacted or resumed. Persist progress in repository
  artifacts, commits, or the harness's goal/task mechanisms when the workflow
  needs continuity.

## Prompting implications

- Put standing rules in `AGENTS.md`; put task-specific intent in the task prompt.
- Point to architecture, schema, deployment, or runbook documents only under the
  condition that makes them relevant.
- Describe the completion evidence and any protected surfaces. Let Codex choose
  the implementation path.
- Keep progress-reporting instructions minimal unless the default communication
  style is unsuitable for automation or a long-running workflow.

## Current-source checks

Before naming a CLI command, config key, model, reasoning effort, subagent field,
or approval behavior, check the installed CLI help or current official docs. Do
not preserve a point-in-time snapshot inside every prompt.
