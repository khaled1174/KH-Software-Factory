<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { ArrowLeft, ArrowRight } from 'lucide-vue-next'
import { parseReviewDiff, type ReviewDiffLine } from '@shared/review-diff'
import { environmentBlocked, requiresManualResolution } from '@shared/review-actions'
import { fetchTaskReview } from '../lib/api'
import { hrefFor, reviewHref } from '../lib/router'
import type { ReviewLineAnchor, TaskReviewResponse } from '../lib/types'

const props = defineProps<{ taskId: string; runId: string | null }>()
defineSlots<{ actions(props: { review: TaskReviewResponse; line: ReviewLineAnchor | null; stale: boolean }): unknown }>()

const review = shallowRef<TaskReviewResponse | null>(null)
const anchor = shallowRef<ReviewLineAnchor | null>(null)
const error = ref('')
const heading = ref<HTMLHeadingElement | null>(null)
const fileIndex = ref(0)
const page = ref(0)
const pageSize = 300
const files = shallowRef<ReturnType<typeof parseReviewDiff>>([])
const activeFile = computed(() => files.value[fileIndex.value])
const rows = computed(() => activeFile.value?.lines.slice(page.value * pageSize, (page.value + 1) * pageSize) ?? [])
const pages = computed(() => Math.ceil((activeFile.value?.lines.length ?? 0) / pageSize))
const diffText = computed(() => review.value?.diff?.text)
const blocker = computed(() => review.value?.selected_attempt?.blocker ??
  (!review.value?.selected_attempt ? review.value?.task.blocker : null))
const canAttach = computed(() => !error.value && review.value?.actions_available &&
  review.value.task.status === 'blocked' && review.value.task.run_id === review.value.selected_attempt?.run_id &&
  Boolean(blocker.value && !requiresManualResolution(blocker.value) && !environmentBlocked(blocker.value)))
const abort = new AbortController()
let timer: ReturnType<typeof setInterval> | undefined
let pending = false
let selectionVersion = 0

watch(() => [review.value?.selected_attempt?.run_id, review.value?.diff?.sha256].join('/'), () => {
  files.value = diffText.value === undefined ? [] : parseReviewDiff(diffText.value)
  fileIndex.value = 0
  anchor.value = null
  selectionVersion++
})
watch(fileIndex, () => { page.value = 0 })

async function refresh() {
  if (pending) return
  pending = true
  try {
    const first = review.value === null
    review.value = await fetchTaskReview(props.taskId, props.runId, abort.signal)
    error.value = ''
    if (first) {
      await nextTick()
      heading.value?.focus()
    }
  } catch (e) {
    if (!abort.signal.aborted) error.value = e instanceof Error ? e.message : String(e)
  } finally { pending = false }
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), 4000)
})
onUnmounted(() => {
  abort.abort()
  clearInterval(timer)
})

async function selectLine(row: ReviewDiffLine, side: 'old' | 'new') {
  const diff = review.value?.diff
  const path = activeFile.value?.path
  const line = side === 'old' ? row.oldLine : row.newLine
  if (!diff || !path || !line) return
  const selection = ++selectionVersion
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(row.text))
  if (selection !== selectionVersion || review.value?.diff?.sha256 !== diff.sha256) return
  anchor.value = {
    artifact: diff.artifact,
    patch_sha256: diff.sha256,
    path,
    side,
    line,
    line_sha256: Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join(''),
  }
}

function time(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function label(value: string): string { return value.replaceAll('_', ' ') }
</script>

<template>
  <section class="task-review" aria-labelledby="review-heading">
    <a class="back-link" :href="hrefFor()"><ArrowLeft :size="16" aria-hidden="true" /> Project &amp; runs</a>
    <p v-if="error" class="review-error" role="status">Task review unavailable. {{ error }}</p>
    <p v-else-if="!review" role="status">Loading task review…</p>
    <template v-if="review">
      <header class="review-heading">
        <div>
          <p class="task-id">{{ review.task.id }} · {{ label(review.task.status) }}</p>
          <h1 id="review-heading" ref="heading" tabindex="-1">{{ review.task.title }}</h1>
          <p class="request">{{ review.task.request }}</p>
        </div>
        <span class="snapshot-time">{{ error ? 'Last received' : 'Fetched' }} · {{ time(review.fetched_at) }}</span>
      </header>

      <div class="review-layout">
        <aside class="review-card" aria-labelledby="attempts-heading">
          <h2 id="attempts-heading">Recent attempts</h2>
          <p v-if="!review.history_available" class="muted">Only the last projected attempt is available.</p>
          <nav v-if="review.attempts.length" class="attempts" aria-label="Review attempts">
            <a v-for="attempt in review.attempts" :key="attempt.run_id"
              :href="reviewHref(taskId, attempt.run_id)"
              :aria-current="attempt.run_id === review.selected_attempt?.run_id ? 'page' : undefined">
              <strong>{{ attempt.run_id }}</strong>
              <span>{{ attempt.status ? label(attempt.status) : 'Status unrecorded' }}</span>
              <span v-if="attempt.started_at">{{ time(attempt.started_at) }}</span>
            </a>
          </nav>
          <p v-else class="muted">No attempt is linked to this task.</p>
        </aside>

        <div class="review-content">
          <section class="review-card" aria-labelledby="attempt-heading">
            <div class="card-head">
              <h2 id="attempt-heading">{{ review.selected_attempt?.run_id || 'Task status' }}</h2>
              <a v-if="review.selected_attempt" :href="hrefFor(review.selected_attempt.run_id)">Open run trace <ArrowRight :size="15" aria-hidden="true" /></a>
            </div>
            <p v-if="review.phase" class="muted">{{ label(review.phase.name) }} · round {{ review.phase.round }}</p>
            <p v-if="review.selected_attempt?.ended_at" class="muted">Ended {{ time(review.selected_attempt.ended_at) }}</p>
            <p v-if="blocker" class="blocker">{{ blocker }}</p>
            <section v-if="blocker?.startsWith('OWNER_REVIEW_REQUIRED:')" class="decision" aria-labelledby="owner-hold-heading">
              <h3 id="owner-hold-heading">Sensitive change awaiting owner decision</h3>
              <ul v-if="review.owner_hold?.categories.length">
                <li v-for="flag in review.owner_hold.categories" :key="flag.category"><strong>{{ flag.category }}</strong> · {{ flag.why }}</li>
              </ul>
              <p>
                Review the controller diff and use the task's accepted intent to record any approved categories.
                Update <code>Approved sensitive changes</code>, re-accept the intent, and start a normal guarded attempt.
              </p>
              <p>Intent location <code>{{ review.owner_hold?.intent_path ?? 'intents/' }}</code></p>
            </section>
            <section v-else-if="blocker?.startsWith('OWNER_DECISION_REQUIRED:')" class="decision" aria-labelledby="owner-evidence-heading">
              <h3 id="owner-evidence-heading">Owner evidence decision</h3>
              <template v-if="review.decision_draft">
                <p>Review the draft for this attempt. Record an applicable decision in <code>.kh/dispositions.json</code> through the existing owner workflow.</p>
                <details>
                  <summary>Draft evidence and invocation IDs</summary>
                  <ul v-if="review.decision_draft.findings.length">
                    <li v-for="(finding,index) in review.decision_draft.findings" :key="index">{{ finding.text }} · {{ label(finding.category) }}</li>
                  </ul>
                  <ul v-if="review.decision_draft.invocations.length">
                    <li v-for="invocation in review.decision_draft.invocations" :key="invocation"><code>{{ invocation }}</code></li>
                  </ul>
                  <p v-else class="muted">The draft has no recorded invocation IDs for this task.</p>
                </details>
                <template v-if="review.decision_draft.checked_candidate_recorded">
                  <p>Once the decision is recorded, the controller can revalidate the kept candidate. This command may promote if the independent review approves.</p>
                  <pre>/kh-start --task {{ taskId }} --review-existing</pre>
                  <p>For an advisory review, add <code>--advisory</code>.</p>
                </template>
              </template>
              <p v-else class="muted">The decision draft is unavailable. Open the run trace for the recorded blocker.</p>
            </section>
            <p v-else-if="blocker && environmentBlocked(blocker)" class="muted">
              Resolve the recorded environment issue before starting another guarded attempt.
            </p>
            <template v-if="review.build_summary">
              <h3>Builder handoff</h3>
              <p>{{ review.build_summary }}</p>
            </template>

            <template v-if="review.checks.length">
              <h3>Recorded checks</h3>
              <details v-for="(check, index) in review.checks" :key="index" class="check" :open="!check.passed">
                <summary><strong>{{ check.name }}</strong> · {{ check.passed ? 'Passed' : 'Failed' }}<template v-if="check.returncode !== null"> · exit {{ check.returncode }}</template></summary>
                <pre v-if="check.output_tail">{{ check.output_tail }}</pre>
                <p v-else class="muted">No output tail was recorded.</p>
              </details>
            </template>
            <template v-if="review.review">
              <h3>Independent review · {{ review.review.approved ? 'Approved' : 'Changes requested' }}</h3>
              <p>{{ review.review.summary }}</p>
              <ul v-if="review.review.blocking.length">
                <li v-for="(finding, index) in review.review.blocking" :key="index">{{ finding }}</li>
              </ul>
              <details v-if="review.review.findings.length">
                <summary>Review findings</summary>
                <ul>
                  <li v-for="(finding, index) in review.review.findings" :key="index">
                    <strong>{{ finding.requirement }} · {{ finding.met ? 'Met' : 'Unmet' }}</strong>
                    <p v-if="finding.evidence">{{ finding.evidence }}</p>
                  </li>
                </ul>
              </details>
            </template>
          </section>

          <section class="review-card" aria-labelledby="diff-heading">
            <div class="card-head">
              <h2 id="diff-heading">Controller diff</h2>
              <label v-if="files.length">
                File
                <select v-model="fileIndex">
                  <option v-for="(file, index) in files" :key="index" :value="index">{{ file.path }}</option>
                </select>
              </label>
            </div>
            <p v-if="!review.diff" class="muted">{{ review.diff_unavailable }}</p>
            <pre v-else-if="!files.length" tabindex="0">{{ review.diff.text }}</pre>
            <template v-else>
              <div class="diff-scroll" tabindex="0" aria-label="Controller diff">
                <div v-for="(row, index) in rows" :key="index" class="diff-row" :class="row.kind">
                  <template v-for="side in (['old', 'new'] as const)" :key="side">
                    <button v-if="$slots.actions && canAttach && review.run_mode && (side === 'old' ? row.oldLine : row.newLine)"
                      class="gutter" type="button"
                      :aria-label="`Comment on ${side} line ${side === 'old' ? row.oldLine : row.newLine}`"
                      :aria-pressed="anchor?.path === activeFile?.path && anchor?.side === side && anchor?.line === (side === 'old' ? row.oldLine : row.newLine)"
                      @click="selectLine(row, side)">{{ side === 'old' ? row.oldLine : row.newLine }}</button>
                    <span v-else class="gutter">{{ side === 'old' ? row.oldLine : row.newLine }}</span>
                  </template>
                  <span aria-hidden="true" class="sign">{{ row.kind === 'added' ? '+' : row.kind === 'removed' ? '−' : ' ' }}</span>
                  <code :aria-label="row.kind === 'added' || row.kind === 'removed' ? `${row.kind}: ${row.text}` : undefined">{{ row.text }}</code>
                </div>
              </div>
              <div v-if="pages > 1" class="pagination">
                <button type="button" :disabled="page === 0" @click="page--">Previous lines</button>
                <span aria-live="polite">{{ page + 1 }} / {{ pages }}</span>
                <button type="button" :disabled="page + 1 >= pages" @click="page++">Next lines</button>
              </div>
            </template>
          </section>
          <p v-if="anchor && canAttach" class="anchor-summary" role="status">
            Replying to {{ anchor.path }} · {{ anchor.side }} line {{ anchor.line }}
            <button type="button" @click="anchor = null; selectionVersion++">Use task instead</button>
          </p>
          <slot name="actions" :review="review" :line="anchor" :stale="Boolean(error)" />
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.task-review { max-width: 1500px; margin: 30px auto; padding: 0 28px; }
.back-link, .card-head a { display: inline-flex; align-items: center; gap: 8px; }
.review-heading, .card-head { display: flex; align-items: start; flex-wrap: wrap; justify-content: space-between; gap: 12px 22px; }
.review-heading { margin: 20px 0 24px; }
h1 { margin: 6px 0; font-size: clamp(25px, 4vw, 36px); font-weight: 650; line-height: 1.2; overflow-wrap: anywhere; }
h2 { margin: 0 0 12px; font-size: 18px; }
h3 { margin: 18px 0 8px; font-size: 14px; }
.task-id, .snapshot-time, .muted { color: var(--dim); font-size: 13px; }
.task-id, .request { margin: 0; }
.task-id { font-family: var(--mono); }
.review-layout { display: grid; grid-template-columns: minmax(170px, 235px) minmax(0, 1fr); align-items: start; gap: 20px; }
.review-content { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.review-card { padding: 20px; background: var(--panel); border: 1px solid var(--border-soft); border-radius: 14px; }
.review-card p, .review-card li { overflow-wrap: anywhere; }
.attempts { display: grid; gap: 4px; }
.attempts a { display: grid; gap: 3px; padding: 9px; border-radius: 6px; text-decoration: none; overflow-wrap: anywhere; }
.attempts a[aria-current='page'] { background: var(--accent-bg); }
.attempts a span { color: var(--dim); font-size: 12px; }
.blocker, .review-error { background: var(--danger-bg); color: var(--red); border-radius: 7px; padding: 10px 12px; white-space: pre-wrap; }
.decision { border: 1px solid var(--border-soft); border-radius: 8px; padding: 0 12px; margin: 12px 0; }
.check { border-top: 1px solid var(--border-soft); padding: 8px 0; }
summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--panel-2); padding: 10px; border-radius: 6px; font-family: var(--mono); font-size: 13px; }
.card-head label { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 13px; }
select { max-width: min(45vw, 360px); background: var(--panel); border: 1px solid var(--border); border-radius: 6px; color: var(--text); padding: 6px; }
.diff-scroll { overflow: auto; border: 1px solid var(--border-soft); border-radius: 6px; font: 13px / 1.6 var(--mono); }
.diff-row { display: grid; grid-template-columns: 4.5ch 4.5ch 2ch minmax(20ch, 1fr); min-width: max-content; }
.diff-row code { white-space: pre; font: inherit; padding-inline-end: 12px; }
.meta { background: var(--panel-2); color: var(--dim); }
.added { background: var(--success-bg); }
.removed { background: var(--danger-bg); }
.sign, .gutter { text-align: right; padding-inline-end: 5px; color: var(--dim); }
button { cursor: pointer; color: var(--text); background: var(--panel); border: 1px solid var(--border); border-radius: 5px; padding: 4px 8px; }
button.gutter { background: transparent; border: 0; padding: 0 5px 0 0; border-radius: 0; font: inherit; }
button.gutter:hover, button.gutter[aria-pressed='true'] { color: var(--blue); background: var(--accent-bg); }
.pagination { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.pagination button:disabled { opacity: .5; cursor: default; }
.anchor-summary { display: flex; align-items: center; gap: 10px; font-size: 13px; }
@media (max-width: 720px) {
  .task-review { padding: 0 16px; }
  .review-layout { grid-template-columns: 1fr; }
}
</style>
