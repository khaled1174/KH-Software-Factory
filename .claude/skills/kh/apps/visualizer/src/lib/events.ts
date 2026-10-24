import type { AgentStartPayload, EventRow, ToolCallPayload } from './types'

// ── Event dot colors ─────────────────────────────────────────────────────────
// One color per event type, shared by the session-card timelines and the phase
// detail list. gate_fail reads as an error signal on purpose.

export const EVENT_DOT_COLORS: Record<string, string> = {
  agent_start: '#7146b2',
  tool_call: '#086f82',
  handoff: '#5659b9',
  agent_end: '#147546',
  error: '#ba3a35',
  gate_fail: '#ba3a35',
}

export function dotColor(type: string | null): string | null {
  if (!type) return null
  return EVENT_DOT_COLORS[type] ?? null
}

// ── Agent lane colors ────────────────────────────────────────────────────────
// Config color wins (agents[].color from the API, or the agent_start payload
// for in-flight agents); the palette below covers dbs written before the
// color column existed.

export const AGENT_FALLBACK_COLORS = ['#7146b2', '#086f82', '#5659b9', '#95610c', '#a13d72']

/** Keep recorded hues, but darken pastel hex colors for ink on light surfaces.
 * This affects display only: the stored role configuration stays untouched.
 */
export function readableAgentColor(color: string): string {
  if (!/^#[0-9a-f]{6}$/i.test(color)) return color
  let channels = [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16))
  const luminance = () => channels.reduce((sum, channel, index) => {
    const value = channel / 255
    return sum + (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4) * [0.2126, 0.7152, 0.0722][index]!
  }, 0)
  while (1.05 / (luminance() + 0.05) < 5) channels = channels.map((channel) => Math.floor(channel * 0.92))
  return `#${channels.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`
}

export function agentColor(
  configColor: string | null | undefined,
  payloadColor: string | null | undefined,
  index: number,
): string {
  return readableAgentColor(
    configColor ??
    payloadColor ??
    AGENT_FALLBACK_COLORS[index % AGENT_FALLBACK_COLORS.length] ??
    '#7146b2'
  )
}

/** "#7146b2" + alpha → rgba() usable in inline styles. Invalid input → transparent. */
export function hexAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m || !m[1]) return 'transparent'
  const n = Number.parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 0xff}, ${(n >> 8) & 0xff}, ${n & 0xff}, ${alpha})`
}

// ── Payload parsing ──────────────────────────────────────────────────────────

export function parsePayload(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch {
    /* legacy or truncated payloads render raw */
  }
  return null
}

export function parseToolCall(e: EventRow): ToolCallPayload | null {
  const payload = parsePayload(e.payload_json)
  if (!payload || typeof payload.tool !== 'string') return null
  return payload as ToolCallPayload
}

export function parseAgentStart(e: EventRow): AgentStartPayload | null {
  return parsePayload(e.payload_json) as AgentStartPayload | null
}

// Args keys most likely to BE the call, in priority order — a bash command, a
// file path, a search pattern. Used to build the one-line label.
const ARG_LABEL_KEYS = [
  'command',
  'cmd',
  'file_path',
  'path',
  'pattern',
  'query',
  'url',
  'prompt',
  'description',
]

const LABEL_MAX = 160

function oneLine(value: string): string {
  const flat = value.replaceAll(/\s+/g, ' ').trim()
  return flat.length > LABEL_MAX ? `${flat.slice(0, LABEL_MAX)}…` : flat
}

/** One-line summary of a tool's args: the call itself, not a JSON dump. */
export function argsSummary(args: Record<string, unknown> | undefined): string {
  if (!args) return ''
  for (const key of ARG_LABEL_KEYS) {
    const v = args[key]
    if (typeof v === 'string' && v.trim() !== '') return oneLine(v)
  }
  const parts: string[] = []
  for (const [key, v] of Object.entries(args)) {
    if (v == null) continue
    parts.push(`${key}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  }
  return oneLine(parts.join(' '))
}

/**
 * Exact-call row label for an event.
 * Rich tool_call → "bash: bun test src". Legacy payloads fall back to
 * the event name plus whatever hint the payload carries.
 */
export function eventLabel(e: EventRow): string {
  if (e.type === 'tool_call') {
    const call = parseToolCall(e)
    if (call?.tool) {
      // New-tracer rows already carry the human label in events.name
      // ("bash: ls -la src") — prefer it over re-deriving.
      if (e.name?.startsWith(call.tool)) return oneLine(e.name)
      const summary = argsSummary(call.args)
      return summary ? `${call.tool}: ${summary}` : call.tool
    }
    const legacy = parsePayload(e.payload_json)
    if (legacy && typeof legacy.pi_event === 'string') {
      return `${e.name ?? 'tool'} ${legacy.pi_event}`
    }
  }
  return e.name ?? e.type ?? ''
}
