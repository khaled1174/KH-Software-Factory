#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the KH factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force] [--with-visualizer]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(7 starter agents), adws/adw_kh_config/kh.config.yaml, .env.sample,
.gitignore entries.
Existing files are skipped unless --force.
"""

import argparse
import os
if os.name == "nt":
    raise SystemExit("Native Windows is not supported. Use WSL2 (not WSL1) with Ubuntu; run KH and install.py inside Ubuntu, with the project under ~/ (not /mnt/c).")
import shutil
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Directory names stamp() never copies: dependency trees and build output are
# not package payload. The preflight symlink check reads the same set, so it
# only ever inspects paths that would actually be written.
NEVER_COPIED = {"__pycache__", "node_modules", ".git", "dist"}

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/kh.db*",
    ".env",
    ".kh-observer/",
    ".kh/project.db*",
    ".kh/project-status.json",
    ".kh/PROJECT_STATUS.md",
    ".kh/task-reports/",
    ".kh/task-receipts/",
    ".kh/work/",
    ".kh/parallel/",
    ".kh/execution-decision.json",
    ".kh/execution-metadata.json",
    ".kh/controller.lock",
    ".kh/controller-control.lock",
    ".kh/controller-state.json",
    ".kh/controller-request.json",
    ".kh/intent-digests.json",
    ".kh/intent-digests.lock",
    ".kh/model-switch/",
    ".kh/timing-history.json",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # Chains that end in a commit phase call `git add -A`, so without this a
    # stamped repo commits its own .pyc files — 15 of them showed up in the
    # first repo that was ever installed into from scratch.
    "__pycache__/",
    "*.pyc",
    ".kh/context-publication.json",
]


def stamp(src: Path, dest: Path, force: bool, stamped: list, skipped: list) -> None:
    if not src.exists():
        # An optional template folder may be absent (archives drop empty
        # directories). Create the destination so the runtime layout is complete.
        dest.mkdir(parents=True, exist_ok=True)
        skipped.append(f"{dest} (template absent: {src.name})")
        return
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name in NEVER_COPIED or child.suffix == ".pyc":
                continue
            stamp(child, dest / child.name, force, stamped, skipped)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stamped.append(str(dest))


def links_copied_content(location: Path) -> bool:
    """True if anything stamp() would copy under `location` is a symlink.

    Scoped to the copied set on purpose. `node_modules/.bin/*` is symlinks on
    every machine that has installed the UI dependencies, and `dist/` can hold
    linked assets; scanning those refused the documented `--with-visualizer`
    upgrade on any project whose dashboard had ever been built.
    """
    for parent, dirnames, filenames in os.walk(location):
        keep = []
        for name in dirnames:
            if name in NEVER_COPIED:
                continue
            if Path(parent, name).is_symlink():
                return True
            keep.append(name)
        dirnames[:] = keep          # prune; never descend into skipped trees
        if any(Path(parent, name).is_symlink() for name in filenames):
            return True
    return False


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# kh runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--with-visualizer", action="store_true",
                        help="Copy optional visualizer source from the full distribution; no dependency install")
    args = parser.parse_args()

    root = Path.cwd()
    stamped, skipped = [], []
    visualizer_source = TEMPLATES.parent / "apps/visualizer"
    visualizer_target = root / ".claude/skills/kh/apps/visualizer"
    if args.with_visualizer:
        if not (visualizer_source / "package.json").is_file():
            parser.error("Visualizer source missing. From the complete KH distribution run: "
                         'python3 install.py --project "' + str(root) + '" --with-visualizer')
        # Refuse linked paths before writing any runtime files.
        for location in (visualizer_source, visualizer_target):
            if any(p.is_symlink() for p in (location, *location.parents)):
                parser.error("Visualizer symlink paths are not supported")
            if location.exists() and links_copied_content(location):
                parser.error("Visualizer symlink paths are not supported")


    stamp(TEMPLATES / "adws", root / "adws", args.force, stamped, skipped)
    stamp(TEMPLATES / "prompt_engineering",
          root / "adws" / "adw_data" / "prompt_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "harness_engineering",
          root / "adws" / "adw_data" / "harness_engineering", args.force, stamped, skipped)
    stamp(TEMPLATES / "kh.config.yaml",
          root / "adws" / "adw_kh_config" / "kh.config.yaml",
          args.force, stamped, skipped)
    stamp(TEMPLATES / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Skipped like any other file if the repo already has a justfile.
    stamp(TEMPLATES / "justfile", root / "justfile", args.force, stamped, skipped)
    stamp(TEMPLATES / "project-plan.example.json", root / "project-plan.example.json",
          args.force, stamped, skipped)
    ensure_gitignore(root, stamped)
    if args.with_visualizer and visualizer_source.resolve() != visualizer_target.resolve():
        stamp(visualizer_source, visualizer_target, args.force, stamped, skipped)


    print(f"kh installed into {root}")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    print("  2. uv run adws/adw_setup_models.py --interactive --apply  # explicit role choices")
    print("  3. uv run adws/adw_bootstrap.py  # reviewed project documentation")
    print("  4. just demo             # optional read-only demonstrations")
    print("  5. just sessions         # text traces; no UI dependencies needed")
    print("  6. just obs              # optional visualizer, requires --with-visualizer and bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
