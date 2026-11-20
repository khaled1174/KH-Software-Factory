import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import { relative, resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";
import { parseReviewDiff } from "../shared/review-diff.ts";
import type { DeliveryPacket } from "../shared/types.ts";
import { DataPathError, dataFile } from "./data-file.ts";
import type { KhDb } from "./db.ts";
import { publishedText, readTaskReview } from "./task-review.ts";

const RECORD_LIMIT = 1024 * 1024;
const ATTACHMENT_LIMIT = 64 * 1024;
const MAX_ATTACHMENTS = 6;
const DIGEST = /^[a-f0-9]{64}$/i;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function jsonRecord(text?: string): Record<string, unknown> {
  try {
    return record(text ? JSON.parse(text) : null);
  } catch {
    return {};
  }
}

function textArtifact(root: string, location: string[], name: string, limit = RECORD_LIMIT) {
  try {
    return publishedText(root, location, name, limit);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (error instanceof DataPathError || error instanceof TypeError || code === "ENOENT" || code === "ELOOP") {
      return null;
    }
    throw error;
  }
}

function committedReceipt(root: string, taskId: string, runId: string) {
  const ledger = dataFile(root, [".kh", "project.db"], Infinity);
  if (!ledger) return null;
  const db = new Database(ledger, { readonly: true });
  try {
    db.exec("PRAGMA busy_timeout=1500");
    const completed = db.query<{ detail: string }, [string, string]>(
      "SELECT detail FROM events WHERE task_id=? AND run_id=? AND event='completed' ORDER BY sequence DESC LIMIT 1",
    ).get(taskId, runId);
    const receipt = jsonRecord(completed?.detail);
    if ((receipt.schema_version !== 1 && receipt.schema_version !== 2) ||
        receipt.task_id !== taskId || receipt.run_id !== runId || receipt.promoted !== true ||
        receipt.checks_passed !== true || receipt.review_approved !== true ||
        typeof receipt.source_sha256 !== "string" || !DIGEST.test(receipt.source_sha256)) return null;
    const file = textArtifact(root, [".kh", "task-receipts", taskId], `${runId}.json`);
    return file && isDeepStrictEqual(jsonRecord(file.text), receipt) ? { receipt, sha256: file.sha256 } : null;
  } finally {
    db.close();
  }
}

function paths(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function relativeArtifact(path: string): boolean {
  return path.length <= 1024 && path.split("/").every((part) =>
    part && part !== "." && part !== ".." && !part.includes("\\") &&
      !Array.from(part).some((character) => character.charCodeAt(0) < 32));
}

function eligibleAttachment(path: string): boolean {
  return relativeArtifact(path) && /^(quality|verification)\//.test(path) &&
    /(?:\.(?:txt|md|log|json|csv|xml)|\/(?:stdout|stderr))$/i.test(path);
}

function handoffIndex(root: string, sessions: string, runId: string) {
  const manifest = jsonRecord(textArtifact(sessions, [runId], "artifact-map.json")?.text);
  const entries = new Map<string, string | null>();
  if (manifest.version !== 1 || !Array.isArray(manifest.mappings)) return entries;
  for (const raw of manifest.mappings) {
    const mapping = record(raw);
    if (mapping.owner_root !== root) continue;
    const hashes = record(mapping.published_handoff_sha256);
    for (const name of paths(mapping.published_handoff_files)) {
      const hash = hashes[name];
      entries.set(name, typeof hash === "string" && DIGEST.test(hash) ? hash.toLowerCase() : null);
    }
  }
  return entries;
}

function inline(value: unknown): string {
  return typeof value === "string" ? value.slice(0, 4096).replace(/[\r\n]+/g, " ")
    .replace(/[\\`*_{}[\]()<>#+.!|]/g, "\\$&") : "";
}

function fenced(text: string, language = ""): string {
  let length = 3;
  for (const match of text.matchAll(/`+/g)) length = Math.max(length, match[0].length + 1);
  const fence = "`".repeat(length);
  return `${fence}${language}\n${text}${text.endsWith("\n") ? "" : "\n"}${fence}`;
}

export function readTaskDelivery(projectRoot: string, trace: KhDb | null, taskId: string,
                                 runId: string | null, selected: string[]): DeliveryPacket | null {
  if (!runId) throw new DataPathError("a run ID is required");
  if (selected.length > MAX_ATTACHMENTS || selected.some((id) => !DIGEST.test(id))) {
    throw new DataPathError("invalid artifact selection");
  }
  const root = resolve(projectRoot);
  const review = readTaskReview(root, trace, taskId, runId);
  if (!review?.selected_attempt) return null;
  const attempt = review.selected_attempt;
  const accepted = attempt.status === "completed" ? committedReceipt(root, taskId, runId) : null;
  const receipt = accepted?.receipt;
  const sessions = trace?.sessionsDir;
  const index = sessions ? handoffIndex(root, sessions, runId) : new Map<string, string | null>();
  const prefix = sessions ? relative(root, resolve(sessions, runId, "context_handoff")).replaceAll("\\", "/") : "";
  const verifiedNames = new Set(paths(receipt?.evidence).flatMap((path) =>
    prefix && path.startsWith(prefix + "/") ? [path.slice(prefix.length + 1)] : []));
  const recordedPatch = [...verifiedNames].find((name) => /^controller_diff_\d+\.patch$/.test(name));
  const patchName = recordedPatch ?? review.diff?.artifact ?? null;
  const changed = receipt ? paths(receipt.changed_files) : parseReviewDiff(review.diff?.text ?? "").map((file) => file.path);
  const checks = receipt && Array.isArray(receipt.checks) ? receipt.checks.map(record) : review.checks;
  const verdict: Record<string, unknown> = receipt ? { ...record(receipt.review), approved: true } : record(review.review);
  const choices = [...(receipt ? verifiedNames : index.keys())].filter(eligibleAttachment).toSorted().slice(0, 60)
    .map((name) => ({ id: createHash("sha256").update(`${runId}\0${name}`).digest("hex"), name }));
  if (selected.some((id) => !choices.some((option) => option.id === id))) {
    throw new DataPathError("selected artifact is outside this attempt's published evidence");
  }

  const lines = ["# Task delivery packet", "", `Task **${inline(taskId)}** · ${inline(review.task.title)}`, "",
    `Run **${inline(runId)}**`, "", accepted ? "**Accepted by the KH controller.**" :
      attempt.status === "completed" ? "**The completion receipt is missing or differs from the project ledger.**" :
        "**Incomplete. The controller has not recorded acceptance for this attempt.**",
    "", "## Current task request", "", fenced(review.task.request.slice(0, 16 * 1024))];
  if (attempt.blocker) lines.push("", "## Recorded blocker", "", fenced(attempt.blocker.slice(0, 16 * 1024)));
  if (accepted && receipt) lines.push("", "## Acceptance receipt", "",
    `Stored receipt \`.kh/task-receipts/${taskId}/${runId}.json\``,
    `Stored receipt SHA-256 \`${accepted.sha256}\``,
    `Verified source SHA-256 \`${receipt.source_sha256}\``);
  lines.push("", "## Changed files", "", ...(changed.length
    ? changed.slice(0, 100).map((path) => `- ${inline(path)}`)
    : ["No changed files are recorded for this attempt."]));
  lines.push("", "## Reported checks", "", ...(checks.length ? checks.slice(0, 50).map((check) =>
    `- ${inline(check.name)} · ${check.passed === true ? "Passed" : check.passed === false ? "Failed" : "Unknown"}` +
      (typeof check.returncode === "number" ? ` · Exit ${check.returncode}` : "")) : ["No checks are recorded."]));
  lines.push("", "## Independent review", "", verdict?.approved === true ? "Approved." :
    verdict?.approved === false ? "Changes requested." : "No verdict is recorded.");
  if (typeof verdict?.summary === "string" && verdict.summary) lines.push("", fenced(verdict.summary.slice(0, 16 * 1024)));
  for (const blocker of paths(verdict?.blocking).slice(0, 30)) lines.push("", `- ${inline(blocker)}`);

  const appendArtifact = (name: string, limit: number, language = "") => {
    const file = sessions && index.has(name) ? textArtifact(sessions, [runId, "context_handoff"], name, limit) : null;
    const expected = index.get(name);
    if (!file) lines.push("Published evidence is missing, unavailable, or exceeds the export size limit.");
    else if (expected && file.sha256 !== expected) lines.push("Published evidence differs from its recorded digest.");
    else {
      lines.push(expected ? `Artifact SHA-256 \`${expected}\`` : "Content integrity is unrecorded for this artifact.",
        "", fenced(file.text, language));
    }
  };

  lines.push("", "## Controller diff", "");
  if (patchName) appendArtifact(patchName, RECORD_LIMIT, "diff");
  else lines.push("This attempt has no available published diff.");
  const included = choices.filter(({ id }) => selected.includes(id));
  if (included.length) {
    lines.push("", "## Selected test artifacts");
    for (const { name } of included) {
      lines.push("", `### ${inline(name)}`, "");
      appendArtifact(name, ATTACHMENT_LIMIT);
    }
  }
  return { filename: `kh-${taskId}-${runId}-delivery.md`, accepted: accepted !== null, artifacts: choices,
    max_attachments: MAX_ATTACHMENTS, markdown: lines.join("\n") + "\n" };
}
