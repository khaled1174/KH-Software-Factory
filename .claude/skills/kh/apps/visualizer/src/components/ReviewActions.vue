<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { environmentBlocked, requiresManualResolution } from '../../shared/review-actions'
import type { ReviewAnchor, ReviewRequestDraft, ReviewRequestState } from '../../shared/review-actions'
import { reviewHref } from '../lib/router'

const props = defineProps<{
  review: {
    task: { id: string; status: string; run_id?: string | null; blocker?: string | null }
    selected_attempt: { run_id: string } | null
    task_definition_sha256?: string | null
  }
  line?: ReviewAnchor | null
  stale?: boolean
}>()

interface Preview {
  mode: string
  profile: string
  setup_sha256: string
  roles: Array<{ role: string; selected: { coding_agent: string; model: string; thinking?: string }; fallback: boolean }>
}

const token = ref('')
const keyInput = ref('')
const keyStorage = 'kh-review-action-key'
const projectKey = ref('')
const capabilityLoaded = ref(false)
const actionsEnabled = ref(false)
const note = ref('')
const attachLine = ref(true)
const progress = ref<ReviewRequestState | null>(null)
const preview = ref<Preview | null>(null)
const confirmed = ref(false)
const busy = ref(false)
const error = ref('')
const pendingId = ref<string | null>(null)
const lifecycle = new AbortController()
let timer: ReturnType<typeof setInterval> | undefined

const taskId = computed(() => props.review.task.id)
const storageKey = computed(() => `kh-review-request:${projectKey.value}:${taskId.value}`)
const selectedRun = computed(() => props.review.selected_attempt?.run_id)
const blocker = computed(() => props.review.task.blocker ?? '')
const latest = computed(() => props.review.task.status === 'blocked' && Boolean(blocker.value) &&
  Boolean(selectedRun.value) && selectedRun.value === props.review.task.run_id)
const ownerDecision = computed(() => latest.value && requiresManualResolution(blocker.value))
const retry = computed(() => environmentBlocked(blocker.value))
const canReply = computed(() => Boolean(token.value && latest.value && props.review.task_definition_sha256 && !ownerDecision.value))
const readOnly = computed(() => capabilityLoaded.value && !actionsEnabled.value && latest.value && !ownerDecision.value)
const locked = computed(() => actionsEnabled.value && !token.value && latest.value && !ownerDecision.value)
const currentDraft = computed(() => canReply.value && progress.value?.request.stopped_run_id === selectedRun.value)
const anchor = computed(() => attachLine.value ? props.line : null)
const canSave = computed(() => canReply.value && !props.stale && !busy.value && (retry.value || Boolean(note.value.trim())))
const stateLabels: Record<ReviewRequestState['state'], string> = {
  saved: 'Reply saved',
  starting: 'Starting a guarded attempt',
  running: 'Attempt running',
  stopped: 'Attempt stopped',
  accepted: 'Accepted by KH',
  unknown: 'Launch result needs inspection',
}

function remember(id: string | null) {
  try {
    if (id) localStorage.setItem(storageKey.value, id)
    else localStorage.removeItem(storageKey.value)
  } catch { /* The controller keeps the saved record. */ }
}

function reset() {
  note.value = progress.value?.request.text ?? note.value
  progress.value = null
  preview.value = null
  pendingId.value = null
  confirmed.value = false
  error.value = ''
  remember(null)
}

async function request<T>(path = '', data?: object): Promise<T> {
  const headers: Record<string, string> = { 'x-kh-review-capability': token.value }
  if (data) headers['content-type'] = 'application/json'
  const response = await fetch(`/api/review/requests${path}`, {
    method: data ? 'POST' : 'GET', headers,
    body: data ? JSON.stringify(data) : undefined,
    signal: data ? undefined : lifecycle.signal,
  })
  const body = await response.json() as T & { error?: string }
  if (!response.ok) throw new Error(body.error ?? `Review request failed (${response.status}).`)
  return body
}

async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { await action() }
  catch (failure) {
    if (!lifecycle.signal.aborted) error.value = failure instanceof Error ? failure.message : String(failure)
  }
  finally { busy.value = false }
}

async function refresh(id: string) {
  const result = await request<ReviewRequestState>(`?id=${encodeURIComponent(id)}`)
  if (result.request.task_id !== taskId.value || id !== pendingId.value) return
  progress.value = result
}

function save() {
  if (!canSave.value || !selectedRun.value || !props.review.task_definition_sha256) return
  const draft: ReviewRequestDraft = {
    request_id: pendingId.value ?? crypto.randomUUID(),
    task_id: taskId.value,
    stopped_run_id: selectedRun.value,
    task_definition_sha256: props.review.task_definition_sha256,
    blocker: blocker.value,
    action: retry.value ? 'retry' : 'request_changes',
    text: retry.value ? '' : note.value,
    anchor: retry.value ? undefined : anchor.value ?? undefined,
  }
  pendingId.value = draft.request_id
  void run(async () => {
    progress.value = await request<ReviewRequestState>('', draft)
    remember(draft.request_id)
    preview.value = null
    confirmed.value = false
  })
}

function inspectSetup() {
  const draft = progress.value?.request
  if (!draft || props.stale) return
  void run(async () => {
    preview.value = await request<Preview>('/preview', { request_id: draft.request_id })
    confirmed.value = false
  })
}

function start() {
  const draft = progress.value?.request
  const digest = preview.value?.setup_sha256
  if (!draft || !digest || !confirmed.value || props.stale) return
  void run(async () => {
    preview.value = null
    confirmed.value = false
    const result = await request<ReviewRequestState>('/start', {
      request_id: draft.request_id, confirm_saved_setup: true, setup_sha256: digest,
    })
    progress.value = result
    note.value = ''
  })
}

function attemptLink(runId: string) {
  return reviewHref(taskId.value, runId)
}

async function checkAccess(key: string) {
  const response = await fetch('/api/review/capability', {
    cache: 'no-store',
    headers: { 'x-kh-review-capability': key },
    signal: lifecycle.signal,
  })
  if (!response.ok) return false
  const capability = await response.json() as { enabled: boolean; unlocked?: boolean; project_key?: string }
  capabilityLoaded.value = true
  actionsEnabled.value = capability.enabled
  projectKey.value = capability.project_key ?? ''
  token.value = capability.unlocked ? key : ''
  try {
    if (token.value) sessionStorage.setItem(keyStorage, key)
    else sessionStorage.removeItem(keyStorage)
  } catch { /* Access remains available for this page. */ }
  if (!token.value) return false
  try { pendingId.value = localStorage.getItem(storageKey.value) }
  catch { /* Storage can be unavailable in the browser. */ }
  if (pendingId.value) await refresh(pendingId.value)
  return true
}

function unlock() {
  void run(async () => {
    if (!await checkAccess(keyInput.value.trim())) throw new Error('Enter the local review key from the terminal.')
    keyInput.value = ''
  })
}

watch(() => props.line, () => { attachLine.value = true })
onMounted(() => {
  timer = setInterval(() => {
    const id = pendingId.value
    if (id && !busy.value && progress.value && ['starting', 'running', 'unknown'].includes(progress.value.state)) {
      void run(() => refresh(id))
    }
  }, 1500)
  let key = ''
  try { key = sessionStorage.getItem(keyStorage) ?? '' }
  catch { /* The owner can unlock the page directly. */ }
  void run(async () => { await checkAccess(key) })
})
onUnmounted(() => {
  lifecycle.abort()
  clearInterval(timer)
})
</script>

<template>
  <section v-if="readOnly || locked || canReply || progress" class="review-actions" aria-labelledby="review-actions-title">
    <h2 id="review-actions-title">Next action</h2>
    <p v-if="readOnly">To send a reply, launch the current visualizer with <code>KH_REVIEW_ACTIONS=1 just obs</code>.</p>

    <form v-if="locked" @submit.prevent="unlock">
      <label for="review-key">Local review key</label>
      <input id="review-key" v-model="keyInput" type="password" autocomplete="off" maxlength="64">
      <button type="submit" :disabled="busy || !keyInput.trim()">Unlock review actions</button>
    </form>

    <div v-else-if="progress" class="request-state">
      <p role="status"><strong>{{ stateLabels[progress.state] }}</strong></p>
      <p v-if="progress.request.text" class="reply-text">{{ progress.request.text }}</p>
      <a :href="attemptLink(progress.request.stopped_run_id)">Original attempt</a>
      <a v-if="progress.recorded_run && progress.run_id" :href="attemptLink(progress.run_id)">Open resulting attempt</a>
      <p v-if="progress.reason">{{ progress.reason }}</p>
      <div v-if="progress.state === 'saved'" class="request-buttons">
        <button v-if="currentDraft && !preview" type="button" :disabled="busy || stale" @click="inspectSetup">Review run setup</button>
        <button v-if="canReply" type="button" :disabled="busy" @click="reset">New reply</button>
      </div>
      <div v-if="currentDraft && preview && progress.state === 'saved'" class="run-preview">
        <p>Profile <strong>{{ preview.profile }}</strong> · {{ preview.mode }} mode</p>
        <ul>
          <li v-for="role in preview.roles" :key="role.role">
            {{ role.role }} · {{ role.selected.model }} · {{ role.selected.coding_agent }}
            <template v-if="role.selected.thinking"> · {{ role.selected.thinking }}</template>
            <template v-if="role.fallback"> · configured fallback</template>
          </li>
        </ul>
        <label><input v-model="confirmed" type="checkbox"> Use this project's saved role setup</label>
        <button type="button" :disabled="busy || !confirmed || stale" @click="start">
          {{ progress.request.action === 'retry' ? 'Retry task' : 'Request changes' }}
        </button>
      </div>
      <button v-if="canReply && progress.state === 'stopped'" type="button" :disabled="busy" @click="reset">Write another reply</button>
      <p v-if="progress.state === 'unknown'">Inspect the run trace or use <code>just project controller-status</code> before scheduling another attempt.</p>
    </div>

    <form v-else-if="canReply" @submit.prevent="save">
      <template v-if="retry">
        <p>Resolve the recorded environment or provider issue before retrying.</p>
      </template>
      <template v-else>
        <label for="review-reply">Reply to this attempt</label>
        <p v-if="anchor" class="anchor">
          <code>{{ anchor.path }}:{{ anchor.line }}</code> ({{ anchor.side }})
          <button type="button" @click="attachLine = false">Reply to task instead</button>
        </p>
        <textarea id="review-reply" v-model="note" rows="4" maxlength="4000" />
      </template>
      <button type="submit" :disabled="!canSave">{{ retry ? 'Prepare retry' : 'Save reply' }}</button>
    </form>
    <p v-if="error" class="action-error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.review-actions { border: 1px solid var(--border-soft); border-radius: 10px; padding: 16px; margin-block: 16px; }
h2 { font-size: 1.1rem; margin: 0 0 12px; }
form, .run-preview { display: grid; gap: 10px; }
textarea { width: 100%; resize: vertical; color: var(--text); background: var(--panel); border: 1px solid var(--border-soft); border-radius: 6px; padding: 8px; font: inherit; }
button { justify-self: start; width: fit-content; padding: 6px 10px; border: 1px solid var(--border-soft); border-radius: 6px; color: var(--text); background: var(--panel); cursor: pointer; }
button:disabled { cursor: default; opacity: .6; }
.request-state a { margin-inline-end: 12px; }
.request-buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.reply-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.anchor, .action-error { overflow-wrap: anywhere; }
.action-error { color: var(--red); }
</style>
