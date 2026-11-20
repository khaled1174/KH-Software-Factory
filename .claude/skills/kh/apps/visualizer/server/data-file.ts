import { lstatSync } from "node:fs";
import { resolve, sep } from "node:path";

export class DataPathError extends Error {}

export function isSafeSegment(value: string): boolean {
  return /^[A-Za-z0-9._-]+$/.test(value) && value !== "." && value !== "..";
}

/** Refuse symlinks at every data path component, including dangling links. */
export function safeFile(root: string, ...segments: string[]): string | null {
  return dataFile(root, segments, 8 * 1024 * 1024);
}

export function dataFile(root: string, segments: string[], maxBytes: number): string | null {
  let path = root;
  let info;
  for (const segment of ["", ...segments]) {
    path = resolve(path, segment);
    if (path !== root && !path.startsWith(root + sep)) throw new DataPathError("invalid data path");
    try {
      info = lstatSync(path);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
    if (info.isSymbolicLink()) throw new DataPathError("symlink data paths are not served");
  }
  if (!info?.isFile()) return null;
  if (info.size > maxBytes) throw new DataPathError("data file exceeds size limit");
  return path;
}
