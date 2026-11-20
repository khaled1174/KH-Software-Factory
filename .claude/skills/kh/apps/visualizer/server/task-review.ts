import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, lstatSync, opendirSync, openSync, readFileSync, readSync } from "node:fs";
import { resolve } from "node:path";
import type { Phase, ProjectTask, ReviewAttempt, TaskReviewResponse } from "../shared/types.ts";
import { DataPathError, dataFile, safeFile } from "./data-file.ts";
import type { KhDb } from "./db.ts";

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;
const ROUND = /^(?:build|checks|review)_(\d+)(?:_|$)/;
const PATCH_LIMIT = 1024 * 1024;
const HISTORY_LIMIT = 100;
const TEXT_LIMIT = 4096;

export class ReviewDataError extends Error {}

interface TaskRow {
  definition: string;
  status: string;
  run_id: string | null;
  summary: string;
  blocker: string | null;
  updated_at: string;
  changed_files: string;
}

interface AttemptRow {
  run_id: string;
  event: string | null;
  detail: string | null;
  started_at: string | null;
  event_at: string | null;
}

function parse(raw: string | null | undefined): unknown {
  try {
    return raw == null ? null : JSON.parse(raw);
  } catch {
    return null;
  }
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function bounded(value: unknown): string {
  return typeof value === "string" ? value.slice(0, TEXT_LIMIT) : "";
}

function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function asAttempt(row: AttemptRow): ReviewAttempt {
  const blocked = row.event === "blocked" || row.event === "recovered";
  const detail = blocked ? parse(row.detail) : null;
  return {
    run_id: row.run_id,
    status: blocked ? "blocked" : row.event,
    blocker: typeof detail === "string" ? bounded(detail) : null,
    started_at: row.started_at,
    ended_at: blocked || row.event === "completed" ? row.event_at : null,
  };
}

function ledgerReview(path: string, taskId: string, runId: string | null) {
  const db = new Database(path, { readonly: true });
  try {
    db.exec("PRAGMA busy_timeout=1500; BEGIN");
    const version = db.query<{ user_version: number }, []>("PRAGMA user_version").get();
    if (version?.user_version !== 1) throw new ReviewDataError("Unsupported project ledger schema");
    const row = db.query<TaskRow, [string]>(
      "SELECT definition,status,run_id,summary,blocker,updated_at,changed_files FROM tasks WHERE id=?",
    ).get(taskId);
    if (!row) return null;
    const definition = record(parse(row.definition));
    if (definition.id !== taskId) throw new ReviewDataError("Invalid task definition in the project ledger");
    const task = {
      ...definition,
      status: row.status,
      run_id: row.run_id,
      summary: row.summary,
      blocker: row.blocker,
      updated_at: row.updated_at,
      changed_files: parse(row.changed_files),
    } as ProjectTask;

    const attempts = db.query<AttemptRow, [string, string, string | null, number]>(
      `WITH history AS (
          SELECT run_id,event,detail,created_at AS event_at,
                 MIN(created_at) OVER (PARTITION BY run_id) AS started_at,
                 ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY sequence DESC) AS latest,
                 sequence
            FROM events
           WHERE task_id=? AND event IN ('running','verifying','reviewing','needs_repair','blocked','recovered','completed')
        )
        SELECT a.run_id,h.event,h.detail,h.started_at,h.event_at
          FROM attempts a LEFT JOIN history h ON a.run_id=h.run_id AND h.latest=1
         WHERE a.task_id=?
         ORDER BY CASE WHEN a.run_id=? THEN 1 ELSE 0 END DESC, h.sequence DESC
         LIMIT ?`,
    ).all(taskId, taskId, runId, HISTORY_LIMIT).filter((attempt) => ID.test(attempt.run_id)).map(asAttempt);
    if (row.run_id) {
      const current = attempts.find((attempt) => attempt.run_id === row.run_id);
      if (current) {
        current.status = row.status;
        current.blocker = row.blocker;
      }
    }
    return { task, attempts, task_definition_sha256: sha256(row.definition) };
  } finally {
    db.close();
  }
}

function projectionReview(root: string, taskId: string) {
  const file = safeFile(root, ".kh", "project-status.json");
  if (!file) return null;
  const snapshot = record(parse(readFileSync(file, "utf8")));
  if (snapshot.schema_version !== 1 || !Array.isArray(snapshot.tasks)) {
    throw new ReviewDataError("Invalid project status projection");
  }
  const task = snapshot.tasks.find((candidate: ProjectTask) => candidate?.id === taskId) as ProjectTask | undefined;
  if (!task) return null;
  const attempts: ReviewAttempt[] = task.run_id && ID.test(task.run_id) ? [{
    run_id: task.run_id,
    status: task.status,
    blocker: task.blocker ?? null,
    started_at: null,
    ended_at: null,
  }] : [];
  return { task, attempts, task_definition_sha256: null };
}

function phaseRound(phase: Phase): number {
  return Number(phase.name?.match(ROUND)?.[1] ?? 0);
}

/** Read the exact bounded bytes, including when a file changes between the path check and open. */
export function publishedText(root: string, directories: string[], artifact: string, limit: number) {
  const file = dataFile(root, [...directories, ...artifact.split("/")], limit);
  if (!file) return null;
  const fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > limit) return null;
    const data = Buffer.alloc(stat.size + 1);
    let length = 0;
    while (length < data.length) {
      const read = readSync(fd, data, length, data.length - length, null);
      if (!read) break;
      length += read;
    }
    if (length !== stat.size) return null;
    const bytes = data.subarray(0, length);
    if (bytes.includes(0)) return null;
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    return { text, sha256: sha256(bytes) };
  } finally {
    closeSync(fd);
  }
}

function safeText(root: string, directories: string[], file: string, limit: number) {
  try {
    return publishedText(root, directories, file, limit);
  } catch (error) {
    if (error instanceof DataPathError || error instanceof TypeError ||
        ["ENOENT", "ELOOP"].includes((error as NodeJS.ErrnoException).code ?? "")) return null;
    throw error;
  }
}

export function publishedHandoff(root: string, sessions: string, runId: string): Map<string, string | null> {
  const map = safeText(sessions, [runId], "artifact-map.json", PATCH_LIMIT);
  const manifest = record(parse(map?.text));
  const files = new Map<string, string | null>();
  if (manifest.version !== 1 || !Array.isArray(manifest.mappings)) return files;
  for (const value of manifest.mappings) {
    const entry = record(value);
    if (entry.owner_root !== root || !Array.isArray(entry.published_handoff_files)) continue;
    const hashes = record(entry.published_handoff_sha256);
    for (const name of entry.published_handoff_files) {
      if (typeof name !== "string") continue;
      const digest = hashes[name];
      const valid = typeof digest === "string" && /^[a-f0-9]{64}$/i.test(digest);
      files.set(name, valid ? digest.toLowerCase() : files.get(name) ?? null);
    }
  }
  return files;
}

function checkedText(sessions: string, paths: string[], names: Map<string, string | null>, artifact: string, limit: number) {
  const expected = names.get(artifact);
  if (expected === undefined) return null;
  const file = safeText(sessions, paths, artifact, limit);
  return file && (!expected || file.sha256 === expected) ? file : null;
}

function currentIntent(root: string, request: string | null): string | null {
  if (!request) return null;
  const location = resolve(root, "intents");
  if (!lstatSync(location, { throwIfNoEntry: false })?.isDirectory()) return null;
  const dir = opendirSync(location);
  try {
    let matches: string | null = null;
    for (let examined = 0; examined < 1000; examined++) {
      const entry = dir.readSync();
      if (!entry) return matches;
      if (!entry.isFile() || !entry.name.endsWith(".md")) continue;
      const candidate = safeText(root, ["intents"], entry.name, 16 * 1024);
      if (!candidate) continue;
      const lines = candidate.text.split(/\r?\n/);
      const start = lines.findIndex((line) => /^## Task request[ \t]*$/.test(line));
      if (start === -1) continue;
      const end = lines.findIndex((line, i) => i > start && line.startsWith("## "));
      if (lines.slice(start + 1, end < 0 ? undefined : end).join("\n").trim() !== request.trim()) continue;
      if (matches) return null;
      matches = "intents/" + entry.name;
    }
    return null;
  } finally {
    dir.closeSync();
  }
}

function decisionDraft(root: string, sessions: string, names: Map<string, string | null>, attempt: ReviewAttempt, task: ProjectTask) {
  if (!attempt.blocker?.startsWith("OWNER_DECISION_REQUIRED:")) return null;
  const draftName = attempt.blocker.match(/a decision draft is at (decision_draft-[0-9]+\.json) in the controller inputs\./)?.[1];
  if (!draftName) return null;
  const artifact = "controller_inputs/" + draftName;
  const paths = [attempt.run_id, "context_handoff"];
  const file = checkedText(sessions, paths, names, artifact, 128 * 1024);
  if (!file) return null;
  const payload = record(parse(file.text));
  if (payload.schema_version !== 1 || payload.in_force !== false || !Array.isArray(payload.draft_decisions)) return null;

  const receiptName = [...names.keys()].filter((name) => /^controller_inputs\/receipts\/\d{20}-[0-9a-f]{12}\.json$/.test(name))
    .toSorted().at(-1);
  if (!receiptName) return null;
  const receiptFile = checkedText(sessions, paths, names, receiptName, PATCH_LIMIT);
  if (!receiptFile || !receiptName.endsWith(`-${receiptFile.sha256.slice(0, 12)}.json`)) return null;
  const receipt = record(parse(receiptFile.text));
  const registered = record(record(receipt.files)[artifact]);
  if (receipt.schema_version !== 1 || receipt.run_id !== attempt.run_id || registered.sha256 !== file.sha256 ||
      registered.producer !== "controller" || registered.kind !== "decision_draft") return null;

  const findings = Array.isArray(payload.findings) ? payload.findings.slice(0, 40).flatMap((raw) => {
    const item = record(raw);
    return typeof item.text === "string" && typeof item.category === "string"
      ? [{ text: bounded(item.text), category: bounded(item.category) }] : [];
  }) : [];
  const invocations = payload.draft_decisions.slice(0, 40).flatMap((raw) => {
    const item = record(raw);
    return item.task === task.id && item.kind === "historical_exception" && Array.isArray(item.invocations)
      ? item.invocations.slice(0, 10).filter((id): id is string => typeof id === "string").map(bounded)
      : [];
  });
  const candidate = record(payload.identity).candidate_sha256;
  const current = task.run_id === attempt.run_id && task.status === "blocked"
    ? safeText(root, [".kh", "work", task.id, "current"], "meta.json", PATCH_LIMIT) : null;
  const meta = record(parse(current?.text));
  const checked = record(meta.checked);
  return {
    artifact, sha256: file.sha256, findings, invocations: [...new Set(invocations)],
    checked_candidate_recorded: (meta.schema === 1 || meta.schema === 2) &&
      meta.task === task.id && meta.run_id === attempt.run_id && typeof candidate === "string" &&
      /^[0-9a-f]{64}$/.test(candidate) && checked.candidate_sha256 === candidate &&
      record(checked.verification_result).passed === true,
  };
}

function attemptEvidence(root: string, trace: KhDb, attempt: ReviewAttempt, task: ProjectTask) {
  const runId = attempt.run_id;
  const budget = record(parse(safeText(trace.sessionsDir, [runId], "execution_budget-task.json", 64 * 1024)?.text));
  const mode = budget.stage === "task" && budget.schema_version === 1 ? record(budget.policy).mode : null;
  const runMode: TaskReviewResponse["run_mode"] = mode === "full" || mode === "light" ? mode : null;
  const phases = trace.phases(runId);
  const round = phases.reduce((latest, phase) => Math.max(latest, phaseRound(phase)), 0);
  const roundPhases = phases.filter((phase) => round && phaseRound(phase) === round);
  const selected = roundPhases.at(-1);
  const quality = roundPhases.findLast((phase) => phase.owner === "quality" && phase.name === `checks_${round}`);
  const reviewer = roundPhases.findLast((phase) => phase.owner === "reviewer");
  const builder = roundPhases.findLast((phase) => phase.owner === "builder");
  const result = quality && trace.phaseEvidence(runId, quality.phase_id).map((raw) => record(parse(raw)))
    .find((log) => Array.isArray(log.checks) && typeof log.passed === "boolean");
  const verdict = reviewer ? record(parse(trace.phaseEvidence(runId, reviewer.phase_id, "ReviewOutput")[0])) : {};
  const handoff = builder ? record(parse(trace.phaseEvidence(runId, builder.phase_id, "BuildOutput")[0])) : {};
  const checks = Array.isArray(result?.checks) ? result.checks.slice(0, 50).flatMap((raw) => {
    const check = record(raw);
    return typeof check.passed === "boolean" && typeof check.name === "string" ? [{
      name: bounded(check.name),
      passed: check.passed,
      returncode: typeof check.returncode === "number" ? check.returncode : null,
      output_tail: bounded(check.output_tail),
    }] : [];
  }) : [];
  const review = typeof verdict.approved === "boolean" ? {
    approved: verdict.approved,
    summary: bounded(verdict.summary),
    blocking: Array.isArray(verdict.blocking) ? verdict.blocking.slice(0, 50).map(bounded) : [],
    findings: Array.isArray(verdict.findings) ? verdict.findings.slice(0, 50).flatMap((raw) => {
      const finding = record(raw);
      return typeof finding.met === "boolean" && typeof finding.requirement === "string" ? [{
        requirement: bounded(finding.requirement),
        met: finding.met,
        evidence: bounded(finding.evidence),
      }] : [];
    }) : [],
  } : null;
  const names = publishedHandoff(root, trace.sessionsDir, runId);
  const paths = [runId, "context_handoff"];
  const artifact = `controller_diff_${round}.patch`;
  const file = quality && result?.passed !== false
    ? checkedText(trace.sessionsDir, paths, names, artifact, PATCH_LIMIT) : null;
  let ownerHold = null;
  if (attempt.blocker?.startsWith("OWNER_REVIEW_REQUIRED:")) {
    const name = `owner_review_${round}.json`;
    const published = checkedText(trace.sessionsDir, paths, names, name, 64 * 1024);
    const flags = parse(published?.text);
    const categories = Array.isArray(flags) ? flags.slice(0, 50).flatMap((raw) => {
      const flag = record(raw);
      return typeof flag.category === "string" && typeof flag.why === "string"
        ? [{ category: bounded(flag.category), why: bounded(flag.why) }] : [];
    }) : [];
    ownerHold = { categories, intent_path: currentIntent(root, task.run_id === runId ? task.request : null) };
  }
  return {
    run_mode: runMode,
    phase: selected ? { phase_id: selected.phase_id, name: selected.name ?? "", round } : null,
    checks,
    review,
    build_summary: bounded(handoff.summary) || null,
    diff: file ? { artifact, round, ...file } : null,
    owner_hold: ownerHold,
    decision_draft: decisionDraft(root, trace.sessionsDir, names, attempt, task),
  };
}

export function readTaskReview(projectRoot: string, trace: KhDb | null, taskId: string,
                               runId: string | null = null): TaskReviewResponse | null {
  if (!ID.test(taskId) || (runId !== null && !ID.test(runId))) throw new DataPathError("invalid task or run ID");
  const root = resolve(projectRoot);
  const ledger = dataFile(root, [".kh", "project.db"], Infinity);
  if (ledger) {
    if (lstatSync(ledger).nlink !== 1) throw new ReviewDataError("Invalid project ledger file");
    for (const suffix of ["-journal", "-wal", "-shm"]) {
      const sidecar = dataFile(root, [".kh", "project.db" + suffix], Infinity);
      if (sidecar && lstatSync(sidecar).nlink !== 1) throw new ReviewDataError("Invalid project ledger sidecar");
    }
  } else if (lstatSync(resolve(root, ".kh", "project.db"), { throwIfNoEntry: false })) {
    throw new ReviewDataError("Invalid project ledger file");
  }
  const source = ledger ? ledgerReview(ledger, taskId, runId) : projectionReview(root, taskId);
  if (!source) return null;
  const { task, attempts } = source;
  const current = attempts.find((attempt) => attempt.run_id === task.run_id);
  const selected = runId === null
    ? (task.status === "blocked" ? current : attempts.find((attempt) => attempt.status === "blocked") ?? current ?? attempts[0])
    : attempts.find((attempt) => attempt.run_id === runId);
  if (runId !== null && !selected) return null;
  const evidence = ledger && selected && trace ? attemptEvidence(root, trace, selected, task) : null;
  return {
    ...source,
    selected_attempt: selected ?? null,
    run_mode: evidence?.run_mode ?? null,
    history_available: ledger !== null,
    fetched_at: new Date().toISOString(),
    phase: evidence?.phase ?? null,
    checks: evidence?.checks ?? [],
    review: evidence?.review ?? null,
    build_summary: evidence?.build_summary ?? null,
    diff: evidence?.diff ?? null,
    diff_unavailable: evidence?.diff ? null : "This attempt has no available published diff.",
    owner_hold: evidence?.owner_hold ?? null,
    decision_draft: evidence?.decision_draft ?? null,
  };
}
