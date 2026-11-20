export interface ReviewDiffLine {
  kind: "meta" | "context" | "added" | "removed";
  text: string;
  oldLine: number | null;
  newLine: number | null;
}

export interface ReviewDiffFile {
  path: string;
  lines: ReviewDiffLine[];
}

/** The controller emits one diff --kh block per measured path. */
export function parseReviewDiff(text: string): ReviewDiffFile[] {
  const files: ReviewDiffFile[] = [];
  let file: ReviewDiffFile | undefined;
  let oldLine = 0;
  let newLine = 0;
  let oldRemaining = 0;
  let newRemaining = 0;

  for (const raw of text.replace(/\r\n|\r/g, "\n").split("\n")) {
    const block = raw.match(/^diff --kh a\/(.+) b\/\1$/u);
    if (raw.startsWith("diff --kh ") && !block) return [];
    if (block?.[1]) {
      file = { path: block[1], lines: [] };
      files.push(file);
      oldRemaining = newRemaining = 0;
      continue;
    }
    if (!file || !raw) continue;
    const hunk = raw.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*|$)/);
    if (hunk) {
      oldLine = Number(hunk[1]);
      oldRemaining = Number(hunk[2] ?? 1);
      newLine = Number(hunk[3]);
      newRemaining = Number(hunk[4] ?? 1);
    }

    const prefix = raw[0];
    if (prefix === "+" && newRemaining > 0) {
      file.lines.push({ kind: "added", text: raw.slice(1), oldLine: null, newLine: newLine++ });
      newRemaining--;
    } else if (prefix === "-" && oldRemaining > 0) {
      file.lines.push({ kind: "removed", text: raw.slice(1), oldLine: oldLine++, newLine: null });
      oldRemaining--;
    } else if (prefix === " " && oldRemaining > 0 && newRemaining > 0) {
      file.lines.push({ kind: "context", text: raw.slice(1), oldLine: oldLine++, newLine: newLine++ });
      oldRemaining--;
      newRemaining--;
    } else {
      file.lines.push({ kind: "meta", text: raw, oldLine: null, newLine: null });
    }
  }
  return files;
}
