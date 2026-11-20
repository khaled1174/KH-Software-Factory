import { ref } from 'vue'

// The review route reserves its third segment so old one- and two-part run links keep working.
export interface Route {
  view: 'project' | 'run' | 'review'
  adwId: string | null
  phaseId: string | null
  taskId?: string
}

function parse(): Route {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map((part) => {
      try { return decodeURIComponent(part) } catch { return part }
    })
  if (parts[0] === 'review' && parts[1] && parts[2] === 'attempts') {
    return { view: 'review', taskId: parts[1], adwId: parts[3] ?? null, phaseId: null }
  }
  return { view: parts[0] ? 'run' : 'project', adwId: parts[0] ?? null, phaseId: parts[1] ?? null }
}

const route = ref<Route>(parse())

window.addEventListener('hashchange', () => {
  route.value = parse()
})

export function useRoute() {
  return route
}

// Display name for the phase crumb — set by the trace view once phases load,
// since the phase_id in the URL is not the display name.
export const phaseCrumb = ref<string | null>(null)

export function hrefFor(adwId?: string | null, phaseId?: string | null): string {
  let h = '#/'
  if (adwId) h += encodeURIComponent(adwId)
  if (adwId && phaseId) h += `/${encodeURIComponent(phaseId)}`
  return h
}

export function reviewHref(taskId: string, runId?: string | null): string {
  const page = `#/review/${encodeURIComponent(taskId)}/attempts`
  return runId ? `${page}/${encodeURIComponent(runId)}` : page
}

export function navigate(adwId?: string | null, phaseId?: string | null): void {
  window.location.hash = hrefFor(adwId, phaseId)
}
