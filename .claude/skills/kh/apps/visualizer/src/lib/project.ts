import type { ProjectTask } from './types'

export const ACTIVE_TASK_STATUSES = new Set(['running', 'verifying', 'reviewing', 'needs_repair'])
export const ATTENTION_TASK_STATUSES = new Set(['blocked', 'needs_repair'])

export function filterTasks(tasks: ProjectTask[], query: string, status: string): ProjectTask[] {
  const term = query.trim().toLocaleLowerCase()
  return tasks.filter((task) => {
    const matchesStatus = status === 'all' ||
      (status === 'attention' ? ATTENTION_TASK_STATUSES.has(task.status) :
        status === 'active' ? ACTIVE_TASK_STATUSES.has(task.status) : task.status === status)
    const text = [task.id, task.title, task.summary, task.blocker].filter(Boolean).join(' ').toLocaleLowerCase()
    return matchesStatus && (!term || text.includes(term))
  })
}

/** Missing historical measurements must never become a measured zero. */
export function observedCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value.toLocaleString('en-US') : 'Not recorded'
}

export function observedPercent(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
    ? `${Number(value.toFixed(1))}%` : 'Not recorded'
}
