export function ts(iso: string | null | undefined): number {
  if (!iso) return NaN
  return new Date(iso).getTime()
}

export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${(ms / 1000).toFixed(2)}s`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  if (m < 60) return `${m}m ${String(rem).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${String(m % 60).padStart(2, '0')}m`
}

export function fmtClock(iso: string | null | undefined): string {
  const t = ts(iso)
  if (!Number.isFinite(t)) return '—'
  return new Date(t).toLocaleTimeString([], { hour12: false })
}

export function fmtDate(iso: string | null | undefined): string {
  const t = ts(iso)
  if (!Number.isFinite(t)) return '—'
  const d = new Date(t)
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${fmtClock(iso)}`
}

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}

export function fmtCost(n: number | null | undefined): string {
  if (n == null) return '—'
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`
}

/** An absent billing flag (old recordings) is uncertainty, never free usage. */
export function fmtKnownCost(n: number | null | undefined, known?: boolean | number | null): string {
  if ((known === true || known === 1) && n != null) return fmtCost(n)
  return n != null && n > 0 ? `Partial ${fmtCost(n)}` : 'Unknown'
}

// Compact offset label for time axes: 0s, 30s, 1m, 1m30s, 2m, 1h05m.
export function fmtOffset(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) {
    const rem = s % 60
    return rem ? `${m}m${String(rem).padStart(2, '0')}s` : `${m}m`
  }
  const h = Math.floor(m / 60)
  const mrem = m % 60
  return mrem ? `${h}h${String(mrem).padStart(2, '0')}m` : `${h}h`
}

const TICK_STEPS_MS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600].map(
  (s) => s * 1000,
)

/** Evenly-stepped time-axis ticks over a span, ≤ maxTicks of them. */
export function axisTicks(spanMs: number, maxTicks = 8): { pct: number; label: string }[] {
  const span = Math.max(spanMs, 1)
  const step = TICK_STEPS_MS.find((s) => span / s <= maxTicks) ?? 3_600_000
  const out: { pct: number; label: string }[] = []
  for (let t = 0; t <= span; t += step) {
    out.push({ pct: (t / span) * 100, label: fmtOffset(t) })
  }
  return out
}

// New-tracer tool_call payloads carry ok:false on tool errors; older payloads
// have no ok key and count as ok.
export function payloadOk(raw: string | null | undefined): boolean {
  if (!raw) return true
  try {
    const p: unknown = JSON.parse(raw)
    if (p && typeof p === 'object' && 'ok' in p) return (p as { ok?: unknown }).ok !== false
  } catch {
    /* not JSON — treat as ok */
  }
  return true
}
