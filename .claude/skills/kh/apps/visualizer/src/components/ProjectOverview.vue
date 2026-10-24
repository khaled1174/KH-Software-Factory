<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { ArrowRight, ClipboardCheck, Layers, Play, Search, TriangleAlert } from 'lucide-vue-next'
import { fetchProject } from '../lib/api'
import { hrefFor } from '../lib/router'
import { ACTIVE_TASK_STATUSES, ATTENTION_TASK_STATUSES, filterTasks, observedPercent } from '../lib/project'
import type { ProjectResponse, ProjectTask } from '../lib/types'
import ProjectMetrics from './ProjectMetrics.vue'

const response = shallowRef<ProjectResponse | null>(null)
const error = ref<string | null>(null)
const fetchedAt = ref('')
const query = ref('')
const status = ref('all')
const planOpen = ref(false)
const project = computed(() => response.value?.available ? response.value : null)
const nextTasks = computed(() => project.value?.next_tasks.map((id) =>
  project.value?.tasks.find((task) => task.id === id)).filter((task): task is ProjectTask => !!task) ?? [])
const attention = computed(() => project.value?.tasks.filter((task) => ATTENTION_TASK_STATUSES.has(task.status)) ?? [])
const activeTasks = computed(() => project.value?.tasks.filter((task) => ACTIVE_TASK_STATUSES.has(task.status)) ?? [])
const visibleTasks = computed(() => filterTasks(project.value?.tasks ?? [], query.value, status.value))
const statuses = computed(() => [...new Set(project.value?.tasks.map((task) => task.status) ?? [])])
let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function refresh() {
  if (inflight) return
  inflight = true
  try {
    response.value = await fetchProject()
    fetchedAt.value = new Date().toLocaleTimeString()
    error.value = null
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally { inflight = false }
}
onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), 2000)
})
onUnmounted(() => clearInterval(timer))

function label(value: string): string { return value.replaceAll('_', ' ') }
function bar(value: number): number { return Math.max(0, Math.min(100, value)) }
async function showTask(id: string) {
  query.value = ''
  status.value = 'all'
  planOpen.value = true
  await nextTick()
  const element = document.getElementById(`task-${id}`) as HTMLDetailsElement | null
  if (element) {
    element.open = true
    element.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' })
  }
}
</script>

<template>
  <section class="project-overview" aria-labelledby="project-heading">
    <div v-if="error" class="snapshot-error" role="status">
      Project status unavailable — retrying. {{ project ? 'Showing the last received snapshot.' : '' }} {{ error }}
    </div>
    <template v-if="project">
      <div class="project-head">
        <div>
          <p class="eyebrow">Project overview</p>
          <h1 id="project-heading">{{ project.project.name }}</h1>
          <p class="subtitle">Approved work, current tasks and the evidence behind each delivery.</p>
        </div>
        <div class="snapshot-meta">
          <span :class="{ stale: error }">{{ error ? 'Snapshot may be stale' : 'Snapshot fetched' }} · {{ fetchedAt }}</span>
          <span>Controller-owned progress</span>
        </div>
      </div>

      <div class="summary-grid" aria-label="Project task counts">
        <article>
          <span class="metric-icon success"><ClipboardCheck :size="20" aria-hidden="true" /></span>
          <div><h2>Approved tasks</h2><strong>{{ project.totals.completed }} <small>/ {{ project.totals.tasks }}</small></strong></div>
        </article>
        <article>
          <span class="metric-icon accent"><Play :size="20" aria-hidden="true" /></span>
          <div><h2>In progress</h2><strong>{{ project.totals.active }}</strong></div>
        </article>
        <article>
          <span class="metric-icon warning"><TriangleAlert :size="20" aria-hidden="true" /></span>
          <div><h2>Needs attention</h2><strong>{{ attention.length }}</strong></div>
        </article>
        <article>
          <span class="metric-icon accent"><Layers :size="20" aria-hidden="true" /></span>
          <div><h2>Ready next</h2><strong>{{ nextTasks.length }}</strong></div>
        </article>
      </div>

      <div class="overview-grid">
        <section class="delivery-panel" aria-labelledby="delivery-heading">
          <div class="section-head">
            <h2 id="delivery-heading">Delivery progress</h2>
            <strong class="completion">{{ observedPercent(project.totals.completion_percent) }}</strong>
          </div>
          <progress :value="bar(project.totals.completion_percent)" max="100" aria-label="Project completion" />
          <p class="muted">Weighted by the approved plan · {{ project.totals.tasks - project.totals.completed }} tasks remaining</p>
          <div class="milestones">
            <div v-for="milestone in project.milestones" :key="milestone.id" class="milestone">
              <span>{{ milestone.title }}</span>
              <span class="muted">{{ milestone.completed }} / {{ milestone.total }} approved</span>
            </div>
          </div>
          <div class="current-work" v-if="activeTasks.length">
            <h3>Current work</h3>
            <button v-for="task in activeTasks.slice(0, 3)" :key="task.id" type="button" @click="showTask(task.id)">
              <span>{{ task.id }} · {{ task.title }}</span><span class="task-state" :class="task.status">{{ label(task.status) }}</span>
            </button>
          </div>
          <p v-else class="muted">No tasks are currently in progress.</p>
        </section>
        <section class="attention-panel" aria-labelledby="attention-heading">
          <div class="section-head"><h2 id="attention-heading">Needs attention</h2><span class="attention-count">{{ attention.length }}</span></div>
          <div v-if="attention.length" class="attention-list">
            <button v-for="task in attention.slice(0, 3)" :key="task.id" type="button" @click="showTask(task.id)">
              <span class="attention-title"><TriangleAlert :size="17" aria-hidden="true" />{{ task.id }} · {{ task.title }}</span>
              <span class="muted">{{ task.blocker || (task.status === 'needs_repair' ? 'Repair required. Open the task and linked run for recorded findings.' : 'Blocker details not recorded.') }}</span>
            </button>
            <button v-if="attention.length > 3" class="text-button" type="button" @click="planOpen = true; status = 'attention'; query = ''">View all {{ attention.length }} tasks</button>
          </div>
          <p v-else class="muted">No tasks are marked blocked or in need of repair.</p>
          <div class="ready-next">
            <h3>Ready next</h3>
            <button v-for="task in nextTasks.slice(0, 2)" :key="task.id" type="button" @click="showTask(task.id)">
              <span>{{ task.id }} · {{ task.title }}</span><ArrowRight :size="17" aria-hidden="true" />
            </button>
            <p v-if="!nextTasks.length" class="muted">{{ project.totals.completed === project.totals.tasks ? 'All planned tasks are approved.' : 'No tasks are ready. Check active work and dependencies.' }}</p>
          </div>
        </section>
      </div>

      <ProjectMetrics :metrics="project.methodology_metrics" />

      <details class="task-plan" :open="planOpen" @toggle="planOpen = ($event.target as HTMLDetailsElement).open">
        <summary><span>Task plan &amp; evidence</span><span class="muted">{{ project.totals.tasks }} tasks</span></summary>
        <div class="task-controls">
          <label class="search"><Search :size="18" aria-hidden="true" /><input v-model="query" type="search" aria-label="Search project tasks" placeholder="Search tasks, blockers or results" /></label>
          <label class="status-filter">Status<select v-model="status" aria-label="Filter project tasks by status">
            <option value="all">All tasks</option><option value="attention">Needs attention</option><option value="active">In progress</option>
            <option v-for="value in statuses" :key="value" :value="value">{{ label(value) }}</option>
          </select></label>
        </div>
        <p class="results-count" role="status">{{ visibleTasks.length }} of {{ project.totals.tasks }} tasks</p>
        <div class="task-list">
          <details v-for="task in visibleTasks" :id="`task-${task.id}`" :key="task.id" class="task">
            <summary><span class="task-id">{{ task.id }}</span><span class="task-title">{{ task.title }}</span><span class="task-state" :class="task.status">{{ label(task.status) }}</span></summary>
            <div class="task-detail">
              <p class="muted">{{ project.milestones.find((item) => item.id === task.milestone_id)?.title }}</p>
              <p v-if="task.request">{{ task.request }}</p>
              <p v-if="task.summary"><strong>Result:</strong> {{ task.summary }}</p>
              <p v-if="task.blocker" class="blocked"><strong>Blocker:</strong> {{ task.blocker }}</p>
              <p v-if="task.dependencies.length"><strong>Depends on:</strong> {{ task.dependencies.join(', ') }}</p>
              <p v-if="task.allowed_paths.length"><strong>Allowed paths:</strong> {{ task.allowed_paths.join(', ') }}</p>
              <p v-if="task.changed_files?.length"><strong>Changed files:</strong> {{ task.changed_files.join(', ') }}</p>
              <div v-if="task.evidence?.length"><strong>Recorded evidence:</strong><ul><li v-for="path in task.evidence" :key="path">{{ path }}</li></ul></div>
              <p v-else class="muted">No evidence paths recorded for this task.</p>
              <a v-if="task.run_id" :href="hrefFor(task.run_id)">Open run trace · {{ task.run_id }} <ArrowRight :size="15" aria-hidden="true" /></a>
              <span v-else class="muted">No run linked yet.</span>
            </div>
          </details>
        </div>
        <p v-if="!visibleTasks.length" class="no-results">No tasks match these filters.</p>
      </details>
      <p class="source-note">Only controller-approved tasks count as complete. Preparation, plan approval and a successful run phase alone do not approve delivery.</p>
    </template>
    <div v-else-if="response && !response.available" class="project-empty">
      <p class="eyebrow">Project overview</p><h1 id="project-heading">No project plan yet</h1>
      <p>{{ response.reason }}</p><p class="muted">Session traces remain available below. No project progress is inferred from them.</p>
    </div>
    <p v-else-if="!error" class="muted">Loading project plan…</p>
  </section>
</template>

<style scoped>
.project-overview { margin: 32px auto 0; padding: 0 28px; max-width: 1600px; }
.project-head, .section-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.project-head { align-items: flex-start; margin-bottom: 24px; }
.eyebrow { color: var(--blue); font-size: 13px; letter-spacing: .1em; text-transform: uppercase; margin: 0 0 8px; font-weight: 600; }
h1 { margin: 0 0 10px; font-size: clamp(24px, 2.5vw, 32px); font-weight: 600; letter-spacing: -.025em; }
h2 { font-size: 20px; margin: 0; font-weight: 600; }
h3 { font-size: 14px; color: var(--dim); margin: 0 0 12px; font-weight: 600; }
.subtitle, .muted { color: var(--dim); font-size: 14px; line-height: 1.6; }
.subtitle { margin: 0; }
.snapshot-meta { display: flex; flex-direction: column; gap: 6px; text-align: right; flex: none; color: var(--dim); font-size: 13px; }
.stale, .blocked { color: var(--red); }
.snapshot-error { padding: 14px; margin-bottom: 20px; background: var(--danger-bg); color: var(--red); border: 1px solid #edc5c2; border-radius: 10px; overflow-wrap: anywhere; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 24px; }
.summary-grid article { display: flex; align-items: center; gap: 16px; padding: 22px; border: 1px solid var(--border-soft); border-radius: 16px; background: var(--panel); box-shadow: var(--shadow); }
.summary-grid h2 { font-size: 14px; color: var(--dim); font-weight: 400; margin-bottom: 8px; }
.summary-grid strong { font-size: 30px; line-height: 1; font-weight: 600; }
.summary-grid small { font-size: 16px; color: var(--dim); font-weight: 400; }
.metric-icon { display: flex; align-items: center; justify-content: center; width: 42px; height: 42px; border-radius: 50%; flex: none; }
.success { background: var(--success-bg); color: var(--green); }
.accent { background: var(--accent-bg); color: var(--blue); }
.warning { background: var(--warning-bg); color: var(--amber); }
.overview-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 24px; }
.delivery-panel, .attention-panel { padding: 24px; border: 1px solid var(--border-soft); border-radius: 16px; background: var(--panel); }
.completion { font-size: 24px; color: var(--blue); }
progress { display: block; width: 100%; height: 8px; margin: 18px 0 10px; border: 0; border-radius: 99px; overflow: hidden; accent-color: var(--blue); background: var(--panel-2); }
progress::-webkit-progress-bar { background: var(--panel-2); border-radius: 99px; }
progress::-webkit-progress-value { background: var(--blue); border-radius: 99px; }
progress::-moz-progress-bar { background: var(--blue); border-radius: 99px; }
.milestones { margin-top: 20px; }
.milestone { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-soft); font-size: 14px; }
.current-work, .ready-next { margin-top: 22px; }
button { cursor: pointer; }
.current-work button, .ready-next button { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; padding: 10px 0; text-align: left; border: 0; background: transparent; color: var(--text); font-size: 14px; }
.current-work button:hover, .ready-next button:hover, .text-button:hover { color: var(--blue); }
.attention-count { border-radius: 99px; background: var(--warning-bg); color: var(--amber); padding: 3px 10px; font-size: 14px; }
.attention-list { margin-top: 10px; }
.attention-list > button { width: 100%; border: 0; border-bottom: 1px solid var(--border-soft); background: transparent; padding: 14px 0; display: flex; flex-direction: column; gap: 8px; text-align: left; }
.attention-list > button:hover { background: var(--panel-3); }
.attention-title { display: flex; align-items: start; gap: 9px; font-size: 14px; font-weight: 600; color: var(--text); }
.attention-title svg { flex: none; color: var(--amber); }
.text-button { color: var(--blue); font-size: 14px; }
.task-plan { background: var(--panel); border: 1px solid var(--border-soft); border-radius: 16px; margin-top: 24px; }
.task-plan > summary { cursor: pointer; padding: 20px 24px; font-size: 17px; font-weight: 600; }
.task-plan > summary > .muted { margin-inline-start: 14px; font-weight: 400; }
.task-controls { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 16px; padding: 0 24px; }
.search { display: flex; align-items: center; gap: 10px; border: 1px solid var(--border); border-radius: 9px; padding: 10px 12px; flex: 1; min-width: 200px; color: var(--dim); }
.search input { width: 100%; min-width: 0; border: 0; background: transparent; color: var(--text); font-size: 14px; }
.search input::placeholder { color: var(--faint); }
.status-filter { display: flex; gap: 10px; align-items: center; color: var(--dim); font-size: 14px; }
select { padding: 10px; color: var(--text); border: 1px solid var(--border); border-radius: 9px; background: var(--panel); }
.results-count { color: var(--dim); font-size: 13px; padding: 0 24px; }
.task-list { padding: 0 24px; }
.task { border-top: 1px solid var(--border-soft); scroll-margin-top: 140px; }
.task summary { cursor: pointer; display: flex; align-items: center; gap: 12px; padding: 16px 0; font-size: 14px; }
.task summary::before { content: '▸'; color: var(--dim); }
.task[open] summary::before { content: '▾'; }
.task-id { font-family: var(--mono); color: var(--dim); }
.task-title { flex: 1; }
.task-state { padding: 4px 9px; border-radius: 6px; background: var(--panel-2); font-size: 13px; white-space: nowrap; }
.task-state.completed { background: var(--success-bg); color: var(--green); }
.task-state.running, .task-state.verifying, .task-state.reviewing { background: var(--accent-bg); color: var(--blue); }
.task-state.blocked, .task-state.needs_repair { background: var(--danger-bg); color: var(--red); }
.task-detail { padding: 0 20px 22px; overflow-wrap: anywhere; font-size: 14px; line-height: 1.65; }
.task-detail a { display: inline-flex; align-items: center; gap: 8px; }
.no-results { padding: 20px 24px; color: var(--dim); }
.source-note { margin: 16px 0 0; font-size: 13px; line-height: 1.6; color: var(--dim); }
.project-empty { padding: 24px; border: 1px solid var(--border-soft); border-radius: 16px; background: var(--panel); }
@media (max-width: 1000px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .overview-grid { grid-template-columns: 1fr; }
}
@media (max-width: 700px) {
  .project-overview { padding: 0 18px; margin-top: 24px; }
  .project-head { flex-direction: column; gap: 12px; }
  .snapshot-meta { text-align: left; }
  .summary-grid { gap: 10px; }
  .summary-grid article { padding: 16px 12px; gap: 10px; }
  .metric-icon { width: 32px; height: 32px; }
  .summary-grid strong { font-size: 26px; }
  .delivery-panel, .attention-panel { padding: 20px; }
  .milestone { flex-direction: column; gap: 4px; }
  .current-work button, .task summary { flex-wrap: wrap; }
  .task-title { min-width: 55%; }
  .task-controls, .task-list { padding-inline: 16px; }
  .task-plan > summary { padding-inline: 16px; }
}
</style>
