Review brief:
{{prompt}}

Previous envelope, if supplied:
{{previous_envelope}}

Evidence directory:
{{context_handoff_dir}}

Write a substantive review.md inside this evidence directory. Return only the controller-requested JSON envelope, including that artifact path. For PlanReviewOutput, copy the exact plan_digest and version from the brief; decision is approve, revise or needs_owner. Include findings with stable id, severity, requirement_id or step_id where applicable, evidence and correction. resolved_finding_ids identifies only prior findings independently verified as corrected. Retain every unresolved prior finding. For ContextReviewOutput, use its provided schema and bind the proposal digest instead.
