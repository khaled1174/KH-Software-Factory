<script setup lang="ts">
import { useRoute, hrefFor, phaseCrumb, reviewHref } from './lib/router'
import SessionsList from './components/SessionsList.vue'
import SessionTrace from './components/SessionTrace.vue'
import ProjectOverview from './components/ProjectOverview.vue'
import TaskReview from './components/TaskReview.vue'
import ReviewActions from './components/ReviewActions.vue'
import TaskDelivery from './components/TaskDelivery.vue'

const route = useRoute()
</script>

<template>
  <div class="app">
    <header class="topbar">
      <nav class="crumbs" aria-label="Breadcrumb">
        <span class="brand">KH Software Factory</span>
        <span class="sep">›</span>
        <a :href="hrefFor()" :class="{ current: route.view === 'project' }">project &amp; runs</a>
        <template v-if="route.view === 'review' && route.taskId">
          <span class="sep">›</span>
          <a :href="reviewHref(route.taskId)" class="current">Review {{ route.taskId }}</a>
        </template>
        <template v-else-if="route.adwId">
          <span class="sep">›</span>
          <a :href="hrefFor(route.adwId)" :class="{ current: !route.phaseId }">{{
            route.adwId
          }}</a>
        </template>
        <template v-if="route.view === 'run' && route.adwId && route.phaseId">
          <span class="sep">›</span>
          <span class="current">{{ phaseCrumb ?? route.phaseId }}</span>
        </template>
      </nav>
      <span class="live-hint">Local project monitor</span>
    </header>
    <main>
      <template v-if="route.view === 'project'">
        <ProjectOverview />
        <SessionsList />
      </template>
      <TaskReview v-else-if="route.view === 'review' && route.taskId"
        :key="`${route.taskId}/${route.adwId ?? ''}`" :task-id="route.taskId" :run-id="route.adwId">
        <template #actions="{ review, line, stale }">
          <ReviewActions :review="review" :line="line" :stale="stale" />
          <TaskDelivery v-if="review.selected_attempt" :key="review.selected_attempt.run_id"
            :task-id="review.task.id" :run-id="review.selected_attempt.run_id" />
        </template>
      </TaskReview>
      <SessionTrace v-else-if="route.adwId" :key="route.adwId" :adw-id="route.adwId" :phase-id="route.phaseId" />
    </main>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 15px 28px;
  background: var(--panel);
  border-bottom: 1px solid var(--border-soft);
  flex-wrap: wrap;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 10;
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  min-width: 0;
  flex-wrap: wrap;
  overflow-wrap: anywhere;
}


.brand {
  color: var(--text);
  font-weight: 700;
  white-space: nowrap;
}

.sep {
  color: var(--faint);
}

.crumbs a {
  color: var(--dim);
}

.crumbs a:hover {
  color: var(--text);
}

.crumbs .current {
  color: var(--text);
}

.live-hint {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--dim);
  font-size: 14px;
  white-space: nowrap;
}

@media (max-width: 700px) {
  .topbar { padding: 14px 18px; }
  .crumbs { gap: 8px; font-size: 14px; }
  .brand { font-size: 16px; }
  .live-hint { display: none; }
}
</style>
