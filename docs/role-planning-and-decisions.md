# Role Planning and Discussion Decisions

Agent Hub does not treat sub-agent roles as fixed personas. The main agent creates temporary
run-scoped roles from the current task, execution mode, risk level, requested skills, and model
availability.

## Role Planner

`RolePlanner` is framework-neutral and can be used before Crew-style dispatch, Discussion Runtime,
or Hybrid execution.

- Dispatch mode creates execution roles such as planner, implementer, tester, security reviewer,
  installer, doctor agent, and rollback planner.
- Discuss mode creates judgment roles such as moderator, domain expert, skeptic, risk officer, and
  decision recorder.
- Daily work roles are supplied by `RoleCatalog`, including director, copywriter, video editor,
  economic analyst, marketing strategist, product manager, designer, sales advisor, finance
  analyst, legal/compliance reviewer, project manager, operations coordinator, and quality reviewer.
- Each role includes mission, required questions, allowed tools, forbidden actions, skill hints,
  model binding, and output schema.
- The role system is extensible: custom `RoleDefinition` entries can be added to a `RoleCatalog`
  without changing `RolePlanner` code.
- If the task is high-risk and too ambiguous, the planner returns `requires_user=True` instead of
  guessing a role layout.

This keeps the same API/model reusable across different roles while preventing role context,
permissions, and output contracts from being mixed.

## Decision Resolver

`DecisionResolver` consumes structured positions from discussion participants and returns one of:

- `selected`: a decision was made with an audit memo.
- `needs_verification`: factual disagreement must be checked before selecting a result.
- `needs_user`: authority, high-risk, low-confidence, or near-tie decisions require user input.

The resolver does not rely on majority vote. Strategy disagreements are scored by:

| Criterion | Default weight |
| --- | ---: |
| Goal fit | 30% |
| Safety | 25% |
| Verifiability | 20% |
| Implementation cost | 15% |
| Maintainability | 10% |

Fact conflicts require verification unless a verified option is supplied. Authority conflicts and
high-risk choices are escalated to the user. Close scores and low-confidence winners are also
escalated, which avoids silent wrong-mode or wrong-plan decisions.

## Runtime integration point

The intended sequence is:

```text
Mode Router
  -> Role Planner
  -> Dispatch / Discussion / Hybrid Runtime
  -> Decision Recorder structured positions
  -> Decision Resolver
  -> Main Agent final response or user question
```

The modules are pure Python and do not depend on CrewAI, AG2, MAF, or AutoGen directly. Runtime
adapters should call them at the planning and synthesis boundaries.
