<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { SessionSummary } from '../lib/types'
import { fetchSessions } from '../lib/api'
import { ts } from '../lib/format'
import SessionCard from './SessionCard.vue'
import { Search } from 'lucide-vue-next'

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())
const query = ref('')
const status = ref('all')

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    sessions.value = await fetchSessions()
    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => clearInterval(timer))

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

const ordered = computed(() =>
  sessions.value.filter((session) => {
    const term = query.value.trim().toLocaleLowerCase()
    return (status.value === 'all' || session.status === status.value) &&
      (!term || [session.adw_id, session.adw_name, session.request].filter(Boolean).join(' ').toLocaleLowerCase().includes(term))
  }).toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)
</script>

<template>
  <section id="runs" class="sessions" aria-labelledby="runs-heading">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div class="list-head">
      <div><h2 id="runs-heading">Run traces</h2><p class="dim">Execution history, role activity and recorded evidence</p></div>
      <div class="filters">
        <label class="run-search"><Search :size="18" aria-hidden="true" /><input v-model="query" aria-label="Search runs" type="search" placeholder="Search runs" /></label>
        <select v-model="status" aria-label="Filter runs by status"><option value="all">All statuses</option><option value="running">Running</option><option value="success">Success</option><option value="fail">Failed</option></select>
      </div>
    </div>
    <p v-if="loaded" class="result-count" role="status">{{ ordered.length }} of {{ sessions.length }} loaded runs · unarchived only</p>

    <div v-if="ordered.length" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="loaded" class="empty-state">{{ sessions.length ? 'No runs match these filters.' : 'No sessions yet — run an ADW to see it here.' }}</div>
    <div v-else-if="!apiError" class="empty-state">loading sessions…</div>
  </section>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
  max-width: 1600px;
  margin: 32px auto 0;
}

.list-head {
  padding: 0 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 20px;
}

h2 { margin: 0 0 6px; font-size: 20px; }
.list-head p { margin: 0; font-size: 14px; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; }
.run-search { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: 9px; color: var(--dim); }
.run-search input { min-width: 0; width: 170px; border: none; background: transparent; color: var(--text); font-size: 14px; }
.run-search input::placeholder { color: var(--faint); }
select { padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel); color: var(--text); font-size: 14px; }
.result-count { color: var(--dim); padding: 0 28px; margin: 16px 0 0; font-size: 13px; }

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 460px), 1fr));
  gap: 18px;
  padding: 16px 28px 28px;
}
@media (max-width: 700px) {
  .list-head, .result-count { padding-inline: 18px; }
  .cards { padding-inline: 18px; }
}

</style>
