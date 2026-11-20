/**
 * KH visualizer server — JSON API over a target repo's kh.db, plus the
 * built UI when ./dist exists. The trace database is read-only except for
 * archive flags. An opt-in action launcher hands replies to the controller.
 *
 * There is no ingest endpoint and no websocket. The data path is
 * agents → sqlite → web ui, and the UI gets there by polling.
 *
 *   bun run server/index.ts
 *   bun run server/index.ts --db /path/to/repo/adws/adw_data/kh.db
 *   KH_DB=/path/to/kh.db PORT=4600 bun run server/index.ts
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomBytes } from "node:crypto";
import { KhDb, resolveDbPath, resolveProjectRoot } from "./db.ts";
import { DataPathError, isSafeSegment, safeFile } from "./data-file.ts";
import { readTaskReview, ReviewDataError } from "./task-review.ts";
import { reviewActions } from "./review-actions.ts";
import { readTaskDelivery } from "./delivery-export.ts";
import type { AgentPrompts, ApiError, HealthResponse, ProjectSnapshot } from "../shared/types.ts";

const PORT = Number(process.env.PORT ?? 4600);
const DIST_DIR = resolve(import.meta.dir, "..", "dist");

const dbPath = resolveDbPath();
const projectRoot = resolveProjectRoot(dbPath);
let db: KhDb | null = null;
try {
  if (existsSync(dbPath)) db = new KhDb(dbPath);
} catch (error) {
  console.error(`[kh] ${(error as Error).message}`);
  process.exit(1);
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function notFound(message: string): Response {
  return json({ error: message } satisfies ApiError, 404);
}

/**
 * DNS rebinding: a page on attacker.example that re-resolves its name to
 * 127.0.0.1 is same-origin with this server, so binding to loopback alone does
 * not keep traces private. Only loopback Host names on the bound port are
 * served; anything else is refused before a handler runs.
 */
let boundPort = PORT;
const LOOPBACK_NAMES = ["127.0.0.1", "localhost", "[::1]"];

function trustedHost(req: Request): boolean {
  const host = (req.headers.get("host") ?? "").toLowerCase();
  return LOOPBACK_NAMES.some((name) => host === `${name}:${boundPort}`);
}

function untrustedHost(): Response {
  return json({ error: "untrusted Host header" } satisfies ApiError, 403);
}

/** Guard every handler so a malformed query can't take the server down mid-run. */
function safely(
  handler: (req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return async (req) => {
    if (!trustedHost(req)) return untrustedHost();
    try {
      // Project plans exist before the first run. Do not create an empty DB;
      // attach readonly as soon as the controller creates the real database.
      if (!db && existsSync(dbPath)) db = new KhDb(dbPath);
      return await handler(req);
    } catch (error) {
      console.error(`[kh] ${req.method} ${new URL(req.url).pathname}:`, error);
      return json({ error: (error as Error).message } satisfies ApiError,
        error instanceof DataPathError ? 400 : error instanceof ReviewDataError ? 422 : 500);
    }
  };
}

function param(req: Request, key: string): string {
  return decodeURIComponent(
    (req as Request & { params: Record<string, string> }).params[key] ?? "",
  );
}

function intQuery(req: Request, key: string, fallback: number): number {
  const raw = new URL(req.url).searchParams.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function projectStatus(): Response {
  if (!projectRoot) return json({ available: false,
    reason: "Set --project-root or KH_PROJECT_ROOT when using a custom database path." });
  const file = safeFile(projectRoot, ".kh", "project-status.json");
  if (!file) return json({ available: false,
    reason: "No project plan yet. Initialize with /kh-project or uv run adws/adw_project.py init --plan project-plan.json to track approved tasks and milestones." });
  let snapshot: ProjectSnapshot;
  try { snapshot = JSON.parse(readFileSync(file, "utf8")) as ProjectSnapshot; }
  catch { return json({ error: "project-status.json is not valid JSON" }, 422); }
  if (!snapshot || snapshot.schema_version !== 1 || !snapshot.project?.name ||
      !snapshot.totals || !Array.isArray(snapshot.milestones) ||
      !Array.isArray(snapshot.tasks) || !Array.isArray(snapshot.next_tasks)) {
    return json({ error: "unsupported or incomplete project snapshot" }, 422);
  }
  return json({ ...snapshot, available: true });
}

/** Serve the built SPA, or explain how to build it for this same listener. */
async function serveStatic(req: Request): Promise<Response> {
  const { pathname } = new URL(req.url);

  if (!existsSync(DIST_DIR)) {
    return new Response(
      `KH visualizer API is running on :${PORT}.\n\n` +
        `No ./dist build found. Run "bun run build", then refresh this page.\n`,
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  // Reject traversal before touching the filesystem.
  const candidate = resolve(join(DIST_DIR, pathname));
  if (candidate === DIST_DIR || candidate.startsWith(DIST_DIR + "/")) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return new Response(Bun.file(candidate));
    }
  }

  // SPA fallback: breadcrumb routes are client-side.
  const indexHtml = join(DIST_DIR, "index.html");
  if (existsSync(indexHtml)) {
    return new Response(Bun.file(indexHtml), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  return notFound("not found");
}

function reviewExecutable(): string | null {
  if (Bun.env.KH_REVIEW_ACTIONS !== "1" || !projectRoot) return null;
  try {
    const entry = safeFile(projectRoot, "adws", "adw_start.py");
    if (entry && /parser\.add_argument\(['"]--review-request['"]/.test(readFileSync(entry, "utf8"))) {
      return Bun.which("uv");
    }
  } catch (error) {
    console.error("[kh] Review launcher is unavailable:", (error as Error).message);
  }
  return null;
}

const executable = reviewExecutable();
const configuredKey = Bun.env.KH_REVIEW_ACTION_KEY;
const terminalKey = executable && !configuredKey && process.stdout.isTTY
  ? randomBytes(32).toString("hex") : "";
const actions = reviewActions({
  root: projectRoot,
  executable,
  key: configuredKey || terminalKey,
  lookup: (taskId, runId) => projectRoot ? readTaskReview(projectRoot, db, taskId, runId) : null,
});

const server = Bun.serve({
  hostname: "127.0.0.1",
  port: PORT,
  routes: {
    "/api/project": { GET: safely(() => projectStatus()) },
    "/api/review/capability": { GET: safely(actions.capability) },
    "/api/review/requests": { GET: safely(actions.read), POST: safely(actions.save) },
    "/api/review/requests/preview": { POST: safely(actions.preview) },
    "/api/review/requests/start": { POST: safely(actions.start) },
    "/api/project/tasks/:task_id/review": {
      GET: safely((req) => {
        if (!projectRoot) return notFound("no project root is configured");
        const taskId = param(req, "task_id");
        const runId = new URL(req.url).searchParams.get("run_id");
        const review = readTaskReview(projectRoot, db, taskId, runId);
        return review ? json({ ...review, actions_available: actions.enabled }) : notFound("task or attempt is missing from this project");
      }),
    },
    "/api/project/tasks/:task_id/delivery": {
      GET: safely((req) => {
        if (!projectRoot) return notFound("no project root is configured");
        const query = new URL(req.url).searchParams;
        const packet = readTaskDelivery(projectRoot, db, param(req, "task_id"), query.get("run_id"), query.getAll("artifact"));
        return packet ? json(packet) : notFound("task or attempt is missing from this project");
      }),
    },
    "/api/health": safely(
      () =>
        json({
          ok: true,
          db: dbPath,
          db_ready: db !== null,
          journal_mode: db?.journalMode ?? "unavailable",
          sessions: db?.sessionCount() ?? 0,
        } satisfies HealthResponse),
    ),

    "/api/sessions": safely((req) => json(db?.sessions(intQuery(req, "limit", 200)) ?? [])),

    "/api/sessions/:adw_id": safely((req) => {
      const detail = db?.sessionDetail(param(req, "adw_id"));
      return detail ? json(detail) : notFound(`no session ${param(req, "adw_id")}`);
    }),

    // Archiving belongs to the reader and is independent of the controller.
    "/api/sessions/:adw_id/archive": {
      POST: safely(async (req) => {
        const adwId = param(req, "adw_id");
        if (!isSafeSegment(adwId)) {
          return json({ error: "invalid adw_id" } satisfies ApiError, 400);
        }
        // A JSON content type makes a cross-site write a preflighted request,
        // which fails here because the server sends no CORS headers.
        if (!(req.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
          return json({ error: "archive requires application/json" } satisfies ApiError, 415);
        }
        const body = (await req.json().catch(() => ({}))) as { archived?: unknown };
        const archived = body.archived === undefined ? true : Boolean(body.archived);
        return db?.setArchived(adwId, archived)
          ? json({ adw_id: adwId, archived })
          : notFound(`no session ${adwId}`);
      }),
    },

    "/api/sessions/:adw_id/events": safely((req) =>
      json(
        db?.events(
          param(req, "adw_id"),
          intQuery(req, "after", 0),
          intQuery(req, "limit", 500),
        ) ?? { events: [], cursor: 0, has_more: false },
      ),
    ),

    "/api/sessions/:adw_id/envelopes": safely((req) =>
      json(db?.envelopes(param(req, "adw_id")) ?? []),
    ),

    "/api/sessions/:adw_id/gates": safely((req) => json(db?.gates(param(req, "adw_id")) ?? [])),

    // The exact prompts an agent was sent, read from the session dir. Files are
    // the raw record; the db has no copy of them.
    "/api/sessions/:adw_id/agents/:agent/prompts": safely(async (req) => {
      const adwId = param(req, "adw_id");
      const agent = param(req, "agent");
      if (!isSafeSegment(adwId) || !isSafeSegment(agent)) {
        return json({ error: "invalid adw_id or agent" } satisfies ApiError, 400);
      }
      const connection = db;
      if (!connection?.session(adwId)) return notFound(`no session ${adwId}`);

      const requestedPhase = new URL(req.url).searchParams.get("phase_id");
      if (requestedPhase !== null && !isSafeSegment(requestedPhase)) {
        return json({ error: "invalid phase_id" } satisfies ApiError, 400);
      }
      const phases = connection.phases(adwId).filter((p) => p.owner === agent && p.kind === "agent");
      if (!requestedPhase && phases.length > 1) {
        return json({ error: "phase_id is required for an agent with multiple phases" }, 400);
      }
      const phase = requestedPhase ? phases.find((p) => p.phase_id === requestedPhase) : phases[0];
      if (!phase) return notFound("phase does not belong to this session and agent");

      const read = (name: string): string | null => {
        const modern = safeFile(connection.sessionsDir, adwId, agent, "phases", phase.phase_id, "prompts", `${name}.md`);
        if (modern) return readFileSync(modern, "utf8");
        // Legacy files predate per-phase directories. Never use one to fill a
        // missing modern phase: that would display another invocation's ask.
        const phasesDir = join(connection.sessionsDir, adwId, agent, "phases");
        if (existsSync(phasesDir) || phases.length !== 1) return null;
        const legacy = safeFile(connection.sessionsDir, adwId, agent, "prompts", `${name}.md`);
        return legacy ? readFileSync(legacy, "utf8") : null;
      };
      return json({
        system: read("system"),
        user: read("user"),
        phase_id: phase.phase_id,
      } satisfies AgentPrompts);
    }),
  },

  fetch(req) {
    if (!trustedHost(req)) return untrustedHost();
    const { pathname } = new URL(req.url);
    if (pathname.startsWith("/api/")) return notFound(`no route ${pathname}`);
    return serveStatic(req);
  },
});

boundPort = server.port ?? PORT;
console.log(`[kh] visualizer api  http://localhost:${server.port}`);
if (terminalKey) console.log(`[kh] Local review key  ${terminalKey}`);
if (executable && !actions.enabled) {
  console.error("[kh] Review actions require a terminal or a valid private KH_REVIEW_ACTION_KEY.");
}
console.log(`[kh] db              ${dbPath}  [journal_mode=${db?.journalMode ?? "waiting for first run"}]`);
console.log(
  existsSync(DIST_DIR)
    ? `[kh] serving ui from  ${DIST_DIR}`
    : `[kh] no ./dist — run "bun run build" before starting the UI`,
);

process.on("SIGINT", () => {
  db?.close();
  process.exit(0);
});
