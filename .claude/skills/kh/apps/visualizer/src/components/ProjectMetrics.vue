<script setup lang="ts">
import type { MethodologyMetrics } from '../lib/types'
import { observedCount, observedPercent } from '../lib/project'
import { fmtKnownCost } from '../lib/format'
import { BookOpenCheck, CircleDollarSign, GitPullRequest, ShieldCheck } from 'lucide-vue-next'

defineProps<{ metrics?: MethodologyMetrics }>()
</script>

<template>
  <section class="quality" aria-labelledby="quality-heading">
    <div class="section-heading">
      <div>
        <h2 id="quality-heading">Delivery quality</h2>
        <p>Recorded task history · observations only</p>
      </div>
    </div>
    <div class="quality-grid">
      <article>
        <ShieldCheck :size="20" aria-hidden="true" />
        <h3>First-attempt acceptance</h3>
        <strong>{{ observedPercent(metrics?.first_attempt_acceptance?.rate_percent) }}</strong>
        <p v-if="metrics?.first_attempt_acceptance">
          {{ metrics.first_attempt_acceptance.accepted }} of
          {{ metrics.first_attempt_acceptance.accepted_with_history }} approved tasks with attempt history
        </p>
        <p v-else>No recorded attempt history</p>
      </article>
      <article>
        <GitPullRequest :size="20" aria-hidden="true" />
        <h3>Code / evidence repairs</h3>
        <strong v-if="metrics?.repairs">{{ observedCount(metrics.repairs.code_observed) }} / {{ observedCount(metrics.repairs.evidence_waves_observed) }}</strong>
        <strong v-else>Not recorded</strong>
        <p v-if="metrics?.repairs">{{ metrics.repairs.unknown_attempts }} attempts with incomplete repair history</p>
        <p v-else>No recorded repair history</p>
      </article>
      <article>
        <BookOpenCheck :size="20" aria-hidden="true" />
        <h3>Documentation assessed</h3>
        <strong>{{ observedPercent(metrics?.documentation?.coverage_percent) }}</strong>
        <p v-if="metrics?.documentation">
          {{ metrics.documentation.assessed_accepted_tasks }} approved tasks assessed ·
          {{ metrics.documentation.unconfigured_accepted_tasks }} unconfigured ·
          {{ metrics.documentation.unrecorded_accepted_tasks }} unrecorded
        </p>
        <p v-else>No recorded documentation assessment</p>
      </article>
      <article>
        <CircleDollarSign :size="20" aria-hidden="true" />
        <h3>Recorded cost</h3>
        <strong>{{ fmtKnownCost(metrics?.cost?.recorded_usd, metrics?.cost?.complete) }}</strong>
        <p>Informational only · never stops work</p>
        <p v-if="metrics?.cost && !metrics.cost.complete">
          Incomplete billing · {{ metrics.cost.unknown_attempts == null ? 'attempt coverage not recorded' : `${metrics.cost.unknown_attempts} attempts with unknown cost` }}
        </p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.quality { margin-top: 28px; }
.section-heading { margin-bottom: 16px; }
h2 { margin: 0 0 6px; font-size: 20px; }
.section-heading p, article p { margin: 0; font-size: 14px; color: var(--dim); line-height: 1.5; }
.quality-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); background: var(--panel); border: 1px solid var(--border-soft); border-radius: 16px; }
article { padding: 22px; min-width: 0; border-inline-end: 1px solid var(--border-soft); }
article:last-child { border-inline-end: none; }
article > svg { color: var(--blue); margin-bottom: 12px; }
h3 { font-size: 14px; font-weight: 500; color: var(--dim); margin: 0 0 8px; }
strong { display: block; font-size: 24px; font-weight: 600; margin-bottom: 10px; overflow-wrap: anywhere; }
@media (max-width: 1000px) {
  .quality-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  article { border-bottom: 1px solid var(--border-soft); }
  article:nth-child(even) { border-inline-end: none; }
  article:nth-last-child(-n+2) { border-bottom: none; }
}
@media (max-width: 560px) {
  .quality-grid { grid-template-columns: 1fr; }
  article { border-inline-end: none; }
  article:nth-last-child(2) { border-bottom: 1px solid var(--border-soft); }
}
</style>
