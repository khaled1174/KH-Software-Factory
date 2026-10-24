# Context preparation
{{prompt}}

Previous context:
{{previous_envelope}}

Evidence directory:
{{context_handoff_dir}}

Return only JSON matching the exact controller-supplied schema. The baseline digest
must match the supplied inventory. Include complete document content, applicability,
retained canonical paths and unresolved decisions. Preserve owner documents verbatim. Only baseline replaceable_paths identify unchanged controller-generated documents eligible for reviewed refresh.
You may create evidence artifacts only within the evidence directory.
