<!-- Historical, unused legacy template. SQLite state plus `dispatcher resume`
and `dispatcher recover` are authoritative. Do not introduce new runtime use. -->
# Resume — {project_name}

Resuming run at step `{current_step}`.

## Current state

| Field | Value |
|---|---|
| Project | {project_name} |
| Step | {current_step} |
| Last decision hash | {last_decision_hash} |
| Last response hash | {last_response_hash} |

## Step statuses

{step_statuses}

Continue from where the plan left off.  Reply with a dispatch envelope.
