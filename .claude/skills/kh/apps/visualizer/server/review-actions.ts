import { createHash, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { closeSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { requiresManualResolution } from '../shared/review-actions'
import type { ReviewRequest, ReviewRequestState } from '../shared/review-actions'

const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/
const SHA = /^[0-9a-f]{64}$/
const LIMIT = 32 * 1024

interface Target {
  task: { id: string; status: string; run_id?: string | null; blocker?: string | null }
  task_definition_sha256?: string | null
  selected_attempt: { run_id: string; status: string | null; blocker?: string | null } | null
  run_mode: 'full' | 'light' | null
  diff: { artifact: string; sha256: string } | null
}

interface Options {
  root: string | null
  executable: string | null
  lookup: (taskId: string, runId?: string) => Target | null
}

interface Reservation {
  run_id: string
  request_sha256: string
}

interface Outcome {
  exit_code: number | null
  reason?: string
}

interface Setup {
  expires: number
  mode: 'full' | 'light'
  digest: string
}

class ActionError extends Error {
  constructor(readonly status: number, message: string) { super(message) }
}

function denied(message: string): never { throw new ActionError(409, message) }

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status, headers: { 'cache-control': 'no-store' } })
}

function regular(path: string, kind: 'file' | 'directory'): boolean {
  const stat = lstatSync(path, { throwIfNoEntry: false })
  if (!stat) return false
  if ((kind === 'file' && !stat.isFile()) || (kind === 'directory' && !stat.isDirectory())) {
    return denied('Review state contains an unsupported file.')
  }
  return true
}

function record<T>(path: string): T | null {
  if (!regular(path, 'file')) return null
  if (lstatSync(path).size > LIMIT) return denied('Review state exceeds its size limit.')
  return JSON.parse(readFileSync(path, 'utf8')) as T
}

function writeOnce(directory: string, target: string, value: unknown): boolean {
  if (regular(target, 'file')) return false
  try { closeSync(openSync(`${target}.claim`, 'wx', 0o600)) }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return false
    throw error
  }
  const tmp = join(directory, `.${randomUUID()}.tmp`)
  const fd = openSync(tmp, 'wx', 0o600)
  try {
    try {
      writeFileSync(fd, JSON.stringify(value))
      fsyncSync(fd)
    } finally {
      closeSync(fd)
    }
    renameSync(tmp, target)
    const parent = openSync(directory, 'r')
    try { fsyncSync(parent) } finally { closeSync(parent) }
    return true
  } finally {
    if (regular(tmp, 'file')) unlinkSync(tmp)
  }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ActionError(400, 'Expected a JSON object.')
  return value as Record<string, unknown>
}

function fields(value: Record<string, unknown>, allowed: string[]) {
  if (Object.keys(value).some((key) => !allowed.includes(key))) {
    throw new ActionError(400, 'Unexpected review request fields.')
  }
}

function string(value: unknown, pattern: RegExp, label: string): string {
  if (typeof value !== 'string' || value.includes('\0') || !pattern.test(value)) {
    throw new ActionError(400, `Invalid ${label}.`)
  }
  return value
}

function parseDraft(data: unknown): ReviewRequest {
  const value = object(data)
  fields(value, ['request_id', 'task_id', 'stopped_run_id', 'task_definition_sha256', 'blocker', 'action', 'text', 'anchor'])
  const action = value.action
  if (action !== 'request_changes' && action !== 'retry') throw new ActionError(400, 'Invalid review action.')
  const text = string(value.text, /^[\s\S]{0,4000}$/, 'reply').trim()
  if (action === 'request_changes' ? !text : Boolean(text || value.anchor)) {
    throw new ActionError(400, 'Request changes requires a reply. Retry uses an empty reply.')
  }
  const request: ReviewRequest = {
    schema_version: 1,
    request_id: string(value.request_id, UUID, 'request ID'),
    task_id: string(value.task_id, ID, 'task ID'),
    stopped_run_id: string(value.stopped_run_id, ID, 'run ID'),
    task_definition_sha256: string(value.task_definition_sha256, SHA, 'task definition'),
    blocker: string(value.blocker, /^[\s\S]{1,16000}$/, 'blocker'),
    action,
    text,
    created_at: new Date().toISOString(),
  }
  if (value.anchor == null) return request
  const anchor = object(value.anchor)
  fields(anchor, ['artifact', 'patch_sha256', 'path', 'side', 'line', 'line_sha256'])
  const side = anchor.side
  const line = anchor.line
  if ((side !== 'old' && side !== 'new') || typeof line !== 'number' || !Number.isSafeInteger(line) || line < 1) {
    throw new ActionError(400, 'Invalid source line.')
  }
  request.anchor = {
    artifact: string(anchor.artifact, /^controller_diff_[1-9][0-9]*\.patch$/, 'patch artifact'),
    patch_sha256: string(anchor.patch_sha256, SHA, 'patch digest'),
    path: string(anchor.path, /^[^\r\n]{1,1024}$/, 'source path'),
    side, line,
    line_sha256: string(anchor.line_sha256, SHA, 'source line digest'),
  }
  return request
}

async function body(req: Request): Promise<unknown> {
  if (req.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    throw new ActionError(415, 'An application/json request is required.')
  }
  const reader = req.body?.getReader()
  if (!reader) throw new ActionError(400, 'A request body is required.')
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let bytes = 0
  let text = ''
  try {
    for (;;) {
      // Stream chunks must be read in order to enforce the body limit.
      // oxlint-disable-next-line eslint/no-await-in-loop
      const { done, value } = await reader.read()
      if (done) break
      bytes += value.byteLength
      if (bytes > LIMIT) throw new ActionError(413, 'The request is too large.')
      text += decoder.decode(value, { stream: true })
    }
    return JSON.parse(text + decoder.decode())
  } catch (error) {
    await reader.cancel().catch(() => {})
    if (error instanceof ActionError) throw error
    throw new ActionError(400, 'The request body is not valid JSON.')
  } finally {
    reader.releaseLock()
  }
}

async function processOutput(stream: ReadableStream<Uint8Array>, limit: number, tail = false): Promise<string> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let output = ''
  try {
    for (;;) {
      // Stream chunks must be decoded in order.
      // oxlint-disable-next-line eslint/no-await-in-loop
      const { done, value } = await reader.read()
      if (done) return output + decoder.decode()
      output += decoder.decode(value, { stream: true })
      if (tail) output = output.slice(-limit)
      else if (output.length > limit) break
    }
    await reader.cancel()
    return denied('The controller preview is too large.')
  } finally {
    reader.releaseLock()
  }
}

export function reviewActions({ root, executable, lookup }: Options) {
  const token = root && executable ? randomBytes(32).toString('hex') : ''
  const projectKey = root ? createHash('sha256').update(root).digest('hex') : ''
  const active = new Set<string>()
  const previewed = new Map<string, Setup>()

  function authorized(req: Request, mutating: boolean) {
    if (!token) throw new ActionError(403, 'Review actions are disabled.')
    const provided = req.headers.get('x-kh-review-capability') ?? ''
    if (!SHA.test(provided) || !timingSafeEqual(Buffer.from(provided), Buffer.from(token))) {
      throw new ActionError(403, 'Review capability is unavailable. Refresh the page.')
    }
    if (mutating && req.headers.get('origin') !== `http://${req.headers.get('host')}`) {
      throw new ActionError(403, 'Review actions require the local visualizer origin.')
    }
  }

  function directory(create = false) {
    if (!root || !regular(join(root, '.kh'), 'directory')) return denied('The project ledger is unavailable.')
    const path = join(root, '.kh', 'review-requests')
    if (create && !regular(path, 'directory')) {
      try { mkdirSync(path, { mode: 0o700 }) }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error }
    }
    regular(path, 'directory')
    return path
  }

  function requireCurrent(request: ReviewRequest) {
    const target = lookup(request.task_id, request.stopped_run_id)
    if (!target || target.task.status !== 'blocked' || target.task.run_id !== request.stopped_run_id ||
        target.task.blocker !== request.blocker || target.task_definition_sha256 !== request.task_definition_sha256) {
      return denied('The task or stopped attempt changed. Reopen the latest review and keep your reply as a draft.')
    }
    if (requiresManualResolution(request.blocker)) {
      return denied('This stop needs the existing owner decision or reconciliation path.')
    }
    if (request.anchor && (!target.run_mode || target.diff?.artifact !== request.anchor.artifact ||
        target.diff?.sha256 !== request.anchor.patch_sha256)) {
      return denied('The selected patch or its run mode is unavailable. Reopen the latest evidence or use a task reply.')
    }
    return target.run_mode ?? 'full'
  }

  function state(directoryPath: string, request: ReviewRequest): ReviewRequestState {
    const id = request.request_id
    const reservation = record<Reservation>(join(directoryPath, `${id}.start.json`))
    if (!reservation) {
      if (regular(join(directoryPath, `${id}.start.json.claim`), 'file')) {
        return { request, state: 'unknown', run_id: null, recorded_run: false,
          reason: 'A launch reservation was interrupted. Inspect the controller before retrying.' }
      }
      return { request, state: 'saved', run_id: null, recorded_run: false }
    }
    const base = { request, run_id: reservation.run_id, recorded_run: false }
    const outcome = record<Outcome>(join(directoryPath, `${id}.outcome.json`))
    const attempt = lookup(request.task_id, reservation.run_id)?.selected_attempt
    if (attempt?.run_id === reservation.run_id) {
      const known = { ...base, recorded_run: true }
      if (active.has(id)) return { ...known, state: 'running' }
      if (attempt.status === 'completed') return { ...known, state: 'accepted' }
      if (attempt.status === 'blocked') return { ...known, state: 'stopped', reason: attempt.blocker ?? outcome?.reason }
      return { ...known, state: 'unknown', reason: 'Inspect the recorded run before retrying.' }
    }
    if (active.has(id)) return { ...base, state: 'starting' }
    if (outcome?.exit_code) return { ...base, state: 'stopped', reason: outcome.reason ?? 'The controller refused to start.' }
    return { ...base, state: 'unknown', reason: 'A launch was reserved. Inspect the controller before retrying.' }
  }

  async function launch(directoryPath: string, request: ReviewRequest, runId: string, setup: Setup) {
    const id = request.request_id
    const outcomePath = join(directoryPath, `${id}.outcome.json`)
    let outcome: Outcome = { exit_code: null }
    try {
      const child = Bun.spawn([
        executable!, 'run', 'adws/adw_start.py', '--task', request.task_id,
        '--mode', setup.mode, '--adw-id', runId, '--review-request', id,
      ], { cwd: root!, env: { ...process.env, KH_REVIEW_SETUP_SHA256: setup.digest }, stdout: 'ignore', stderr: 'pipe' })
      const [exitCode, stderr] = await Promise.all([child.exited, processOutput(child.stderr, 8000, true)])
      const lastError = stderr.split(/\r?\n/).findLast((line) => line.startsWith('kh-start: '))
      outcome = { exit_code: exitCode, reason: lastError?.slice(10, 2010) }
    } catch {
      outcome.reason = 'The launcher could not confirm whether the controller started.'
    } finally {
      try { writeOnce(directoryPath, outcomePath, outcome) }
      catch (error) { console.error('[kh] Review outcome could not be recorded:', (error as Error).message) }
      active.delete(id)
    }
  }

  function wrap(handler: (req: Request) => Response | Promise<Response>, mutating = false) {
    return async (req: Request) => {
      try {
        authorized(req, mutating)
        return await handler(req)
      } catch (error) {
        if (error instanceof ActionError) return json({ error: error.message }, error.status)
        console.error('[kh] Review action failed:', (error as Error).message)
        return json({ error: 'Review state could not be read or written.' }, 500)
      }
    }
  }

  return {
    capability: () => json(token ? { enabled: true, token, project_key: projectKey } : { enabled: false }),
    save: wrap(async (req) => {
      const request = parseDraft(await body(req))
      const path = directory(true)
      const requestPath = join(path, `${request.request_id}.json`)
      function existing(saved: ReviewRequest | null) {
        if (!saved) return null
        if (JSON.stringify(saved) !== JSON.stringify({ ...request, created_at: saved.created_at })) {
          return denied('This request ID already belongs to another reply.')
        }
        return json(state(path, saved))
      }
      const previous = existing(record<ReviewRequest>(requestPath))
      if (previous) return previous
      requireCurrent(request)
      if (!writeOnce(path, requestPath, request)) {
        return existing(record<ReviewRequest>(requestPath)) ?? denied('An earlier save is incomplete. Keep the reply and try a new request.')
      }
      return json(state(path, request), 201)
    }, true),
    read: wrap((req) => {
      const id = string(new URL(req.url).searchParams.get('id'), UUID, 'request ID')
      const path = directory()
      const request = record<ReviewRequest>(join(path, `${id}.json`))
      if (!request) throw new ActionError(404, 'Review request unavailable.')
      return json(state(path, request))
    }),
    preview: wrap(async (req) => {
      const payload = object(await body(req))
      fields(payload, ['request_id'])
      const id = string(payload.request_id, UUID, 'request ID')
      const request = record<ReviewRequest>(join(directory(), `${id}.json`))
      if (!request) throw new ActionError(404, 'Review request unavailable.')
      const mode = requireCurrent(request)
      const process = Bun.spawn([
        executable!, 'run', 'adws/adw_start.py', '--task', request.task_id, '--mode', mode, '--dry-run',
      ], { cwd: root!, stdout: 'pipe', stderr: 'ignore', timeout: 15_000 })
      const [exitCode, output] = await Promise.all([process.exited, processOutput(process.stdout, 256 * 1024)])
      if (exitCode !== 0) return denied('The controller preview is unavailable. Check the project setup.')
      const preview = object(JSON.parse(output))
      if (Array.isArray(preview.missing_checks) && preview.missing_checks.length) {
        return denied('Configure the required quality checks before starting another attempt.')
      }
      if (typeof preview.review_setup_sha256 !== 'string' || !SHA.test(preview.review_setup_sha256) ||
          !Array.isArray(preview.roles) || typeof preview.profile !== 'string') {
        return denied('The controller did not provide a supported setup preview.')
      }
      const now = Date.now()
      for (const [key, setup] of previewed) if (setup.expires <= now) previewed.delete(key)
      previewed.set(id, { expires: now + 5 * 60_000, mode, digest: preview.review_setup_sha256 })
      return json({ mode, profile: preview.profile, roles: preview.roles, setup_sha256: preview.review_setup_sha256 })
    }, true),
    start: wrap(async (req) => {
      const payload = object(await body(req))
      fields(payload, ['request_id', 'confirm_saved_setup', 'setup_sha256'])
      const id = string(payload.request_id, UUID, 'request ID')
      const digest = string(payload.setup_sha256, SHA, 'preview digest')
      if (payload.confirm_saved_setup !== true) throw new ActionError(400, 'Confirm the saved project setup first.')
      const path = directory()
      const file = join(path, `${id}.json`)
      const request = record<ReviewRequest>(file)
      if (!request) throw new ActionError(404, 'Review request unavailable.')
      const current = state(path, request)
      if (current.state !== 'saved') return json(current)
      const setup = previewed.get(id)
      if (!setup || setup.digest !== digest || setup.expires <= Date.now() || requireCurrent(request) !== setup.mode) {
        return denied('Review the saved run setup again before starting.')
      }
      const runId = `browser-${id}`
      const reservation = { run_id: runId, request_sha256: createHash('sha256').update(readFileSync(file)).digest('hex') }
      if (!writeOnce(path, join(path, `${id}.start.json`), reservation)) return json(state(path, request))
      previewed.delete(id)
      active.add(id)
      void launch(path, request, runId, setup)
      return json(state(path, request), 202)
    }, true),
  }
}
