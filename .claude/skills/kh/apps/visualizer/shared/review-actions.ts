export interface ReviewAnchor {
  artifact: string
  patch_sha256: string
  path: string
  side: 'old' | 'new'
  line: number
  line_sha256: string
}

export interface ReviewRequest {
  schema_version: 1
  request_id: string
  task_id: string
  stopped_run_id: string
  task_definition_sha256: string
  blocker: string
  action: 'request_changes' | 'retry'
  text: string
  anchor?: ReviewAnchor
  created_at: string
}

export type ReviewRequestDraft = Omit<ReviewRequest, 'schema_version' | 'created_at'>

export interface ReviewRequestState {
  request: ReviewRequest
  state: 'saved' | 'starting' | 'running' | 'stopped' | 'accepted' | 'unknown'
  run_id: string | null
  recorded_run: boolean
  reason?: string
}

export function ownerDecisionRequired(blocker: string): boolean {
  return /^(OWNER_REVIEW_REQUIRED|OWNER_DECISION_REQUIRED):/.test(blocker)
}

export function requiresManualResolution(blocker: string): boolean {
  return ownerDecisionRequired(blocker) || /^(ROLE_CLEANUP_PENDING|Source was promoted; completion recording needs reconciliation):/.test(blocker)
}

export function environmentBlocked(blocker: string): boolean {
  return /^(INFRA_OR_PROVIDER_BLOCKER|PROVIDER_OUTAGE|RUNTIME_INCOMPATIBLE|NOT_CONFIGURED):/.test(blocker)
}
