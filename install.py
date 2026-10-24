#!/usr/bin/env python3
"""Install the KH Software Factory package with conflict detection, backups, and rollback.

No dependencies beyond Python's standard library. Does not install packages,
start models, change Git state, or touch live secrets. Run from the unpacked ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
if os.name == "nt":
    raise SystemExit("Native Windows is not supported. Use WSL2 (not WSL1) with Ubuntu; run KH and install.py inside Ubuntu, with the project under ~/ (not /mnt/c).")
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

PACKAGE = Path(__file__).resolve().parent
VISUALIZER = Path(".claude/skills/kh/apps/visualizer")
# Build inputs and outputs are never package payload. Kept identical to
# scripts/package.py EXCLUDED_PARTS so a checkout install and an archive
# install produce the same files; a divergence here silently shipped a
# developer's built `dist/` into target projects.
EXCLUDED = {".git", "__pycache__", "node_modules", ".pytest_cache", ".DS_Store", "dist"}
# Files the package stamps once and the project owns from then on. They carry
# the owner's protected paths, env allowlist, quality checks, per-role timeouts
# and budgets. An upgrade must preserve those project safeguards. Role prompts
# carry project instructions and must not be silently replaced or merged.
# Keep differing project files and save package versions for comparison.
# `--reset-config` selects the config template only; it never resets prompts.
PROJECT_OWNED = ("adws/adw_kh_config/kh.config.yaml",)
PROJECT_PROMPTS = Path("adws/adw_data/prompt_engineering")
# Exact pristine payload hashes from shipped versions, populated when files retire.
# Unknown versions and owner edits are preserved rather than inferred from backups.
RETIRED_PATHS: dict[str, set[str]] = {
    '.claude/skills/kh/templates/adws/adw_modules/legacy.py': {'4989684df30b755a04f098e04f46093a38916e8aa4c0ccb12afc4072a69a0ef5'},
    'adws/adw_modules/legacy.py': {'4989684df30b755a04f098e04f46093a38916e8aa4c0ccb12afc4072a69a0ef5'},
    '.claude/skills/kh/apps/visualizer/src/components/ExecutionBoard.vue': {'8850c62196add0fb40a19284505203a549d3e25ce3b1425cbe2f1a8e430bd2e6'},
    '.claude/skills/kh/apps/visualizer/src/components/KanbanCard.vue': {'56a56a83e10878e90717f43c35038a4f2a06aea97abe7e562400e025129b0323'},
    '.claude/skills/kh/apps/visualizer/src/components/KanbanDetail.vue': {'2665e5b9d8776dd7a3617a3bfd6e1ac7b5a40bd5633d6321787c85d95fcd9313'},
    '.claude/skills/kh/apps/visualizer/src/lib/kanban.ts': {'aaf5318cdcd1f25c3bc1e6493a16386e9d55f5e964b67b0929aa363b09087f8e'},
    '.claude/skills/kh/apps/visualizer/src/lib/board-follow.ts': {'5ea61a7d6acf3a764ba40587549eec99369af604dba9bc1113a8635b98ca9dd7'},
    '.claude/skills/kh/apps/visualizer/shared/kanban.ts': {'5b680b4d839192a5fe00dfd6db1296eef86a2694907725a4ad5f9c552f0b43b9'},
    '.claude/skills/kh/apps/visualizer/package-lock.json': {'7377e977076b4a5a2228c291c303e1e26a0b523d8dd13c602d0c6724d8b13e3f'},
    '.claude/skills/kh/apps/visualizer/public/favicon.png': {'2d5a3e3e8363b7b03e1f19bdc9e764e464d6086018605070f3a821914b8f1345'},
    '.claude/skills/kh/apps/visualizer/public/logo.png': {'6310c113eae8bc37760f257dd2b3092b2ecf3f90063cfeedbfcaa80be9564db4'},
    '.claude/skills/kh/apps/visualizer/public/models/claude.png': {'d79841af3b40259b85233330beb498e260468aaf5d21c4572de7c223db53ae92'},
    '.claude/skills/kh/apps/visualizer/public/models/gemini.png': {'e14286c3cb66092321f8c79de6b217aa70e150d574e0ae7f4e6b54daa58a11f4'},
    '.claude/skills/kh/apps/visualizer/public/models/kimi.png': {'cc1548fe6ba1a3682a95818eda9f141d9b68e22d9d4b696eb6f2f5d02d4e051d'},
    '.claude/skills/kh/apps/visualizer/public/models/openai.png': {'aeea76111c753dd681d9ce68e666bfd7eebd06b396213cc1ae989d706429b0ff'},
    '.claude/skills/kh/apps/visualizer/public/models/zai.png': {'8f54fb7a803ea6b6fd2d0d2076acbb2bb13af1375ae1b84d56bdf176617fbd49'},
    '.claude/skills/kh/templates/adws/adw_modules/kanban.py': {'b7e8e04cba4ac7489cec8e0fe16f641df1c05e94c9c6f336ad47d9d980a4e300'},
    'adws/adw_modules/kanban.py': {'b7e8e04cba4ac7489cec8e0fe16f641df1c05e94c9c6f336ad47d9d980a4e300'},
    '.claude/skills/kh/cookbooks/kh_overview.md': {'7ca7bedab4d697531aea1d62f238c059b5868b1738d76d08236b1dc7fecfb557'},
    '.claude/skills/kh/cookbooks/create_config.md': {'97eb7e1c1766aea42750f5570a5fdfe0393ec03b8d88c90d949f29f31b499f01'},
    '.claude/skills/kh/cookbooks/update_config.md': {'97eb7e1c1766aea42750f5570a5fdfe0393ec03b8d88c90d949f29f31b499f01'},
    '.claude/skills/kh/cookbooks/update_modules.md': {'1890d5a48864145e10663a76c43a966a5ab85cbb13c967aeb7f68bf6c576229b', 'c9f09e5e1cb07bec5c922b7a15130f049a4de10975c2a92814dca7bbd2c273c7'},
    '.claude/skills/kh/cookbooks/update_adw.md': {'1890d5a48864145e10663a76c43a966a5ab85cbb13c967aeb7f68bf6c576229b', 'c9f09e5e1cb07bec5c922b7a15130f049a4de10975c2a92814dca7bbd2c273c7'},
    '.claude/skills/kh/cookbooks/how_to_prompt_for_the_eng.md': {'3540f826e9c8644b0b541e29c2e8735591d83d42d49212aeb8c658d6250ecc7d', '8b8626c2f49155c6b29eae1b6e5eb15db51e8c2711b705ead57df3e00eb8d485'},
    '.claude/skills/kh-model-router/SKILL.md': {'d8946ed7ff080291d1a823a1a5db136366137dda17da4aefb6140be4ff4f7536'},
    '.claude/skills/kh-model-router/references/startup.md': {'b912aad369da0a1f182e4d28a67d298df805415ef41da24f7cdce10905dd7d15'},
    '.claude/skills/kh-model-router/agents/openai.yaml': {'51b9b399410b153e16320d665afd210ca8219db5556b22af362fa56bc3b6c934'},
    '.claude/skills/kh/templates/adws/adw_modules/review_panel.py': {'5dea8975bf8f8120be812ec97de9d3272d4d3b4d515873e21533bea75b852bbe', '67766643321c0c72703e11ebe649b8eda7e7d9cf573617a58bda12d55fd8a47a'},
    'adws/adw_modules/review_panel.py': {'5dea8975bf8f8120be812ec97de9d3272d4d3b4d515873e21533bea75b852bbe', '67766643321c0c72703e11ebe649b8eda7e7d9cf573617a58bda12d55fd8a47a'},
    '.claude/skills/task-observer/scripts/migrate-log.py': {'3f816fb85a714e7eef07701a7ca1098bb23301fc097ea390ec25fbf1fd2e2bac'},
    '.claude/skills/task-observer/scripts/validate-skill-bundle.py': {'68626b36ff592b10ae253fec234b72ebd30483da33aba369358b15423b690a4f'},
    '.claude/skills/task-observer/references/migration.md': {'5d66ed1e1226757f9f2f84212e10bda268a39fa51245f455f75d96ee390301db'},
    '.claude/skills/task-observer/references/skill-authoring.md': {'01a9c090c52303268a3114697ababc6c86de44d47b245fc65aab5b40e2611b75'},
    '.claude/skills/task-observer/references/weekly-review.md': {'39a5b8a5ad103c772c6c4bd4c6742f3787ea81cf62a9ee6142815715f4a267ec'},
    '.claude/commands/kh-build-review.md': {'8ac8b2a20309f16c8bb7112e9d06a6b0ca6107c7e5d57636506478d49ece35f9'},
    '.claude/commands/kh-build-test.md': {'5292f89703726dff37d6f8bdc7d26564c7a4d80e60a3264016e8fbcc06a0c203'},
    '.claude/commands/kh-build.md': {'e8f8049fd24fdd82e03531deb28a00b888228016db921ce292ac975ac33290fc'},
    '.claude/commands/kh-demo.md': {'90e4bc4ea3991e7b1ac20bc8f73765df261b2fba8fceaa13f12fe25d6d50df37'},
    '.claude/commands/kh-install.md': {'f88f767aadffc492147f005ed9d46873a239c2e5095c4e31ecced81180848f30'},
    '.claude/commands/kh-new-workflow.md': {'6823b4a55a55aa001fadcf7837810be9679271e044bd793beac33425dc16eac7'},
    '.claude/commands/kh-plan-build-quality.md': {'31325984c9bb90c14dd32e54fb57baf978909baae90c8c5171c5e1496815f61d'},
    '.claude/commands/kh-plan-build.md': {'0b6d85805533fbe29f8ccb9a01f45bd8243273f27c077252f003d90c52d59973'},
    '.claude/commands/kh-plan.md': {'5260f58d602530e9327bbe976b71c404b79af03965a0eef0d23b7ae2032a524f'},
    '.claude/commands/kh-sdlc.md': {'1bbc5ef77032845192a9235ef16ffc5e8fa177d77832b90ed7f2334e3a88262d'},
    '.claude/skills/kh/cookbooks/create_adw.md': {'7413fa98b7f7c02798a8a92422ea7a51a275701650acf9a0927e274c1e4df119'},
    '.claude/skills/kh/scripts/make_adw.py': {'e68db686c9ef3d01120324d66a4df326874eacf0eb677f0ffef2f3a5245f22c4'},
    '.claude/skills/kh/scripts/make_config.py': {'d451432c2b64cf7127f07401c76f613b23c1ef9132172e5919e7a2cf1c5f15c0'},
    '.claude/skills/kh/templates/adws/adw_build.py': {'0d17d735d0fa5e9e7d2a9270255824324fb3f436c0fc2f58fb3787d0691f555d'},
    '.claude/skills/kh/templates/adws/adw_build_review.py': {'f7211ccfaaeca3d27858ed4fad866e905ef3b50190f5bc1e4b0abf4a6334fc3a'},
    '.claude/skills/kh/templates/adws/adw_build_test.py': {'c9836ddd23fd1d4e9762c5e5e2c3c86ab3015791b086ad4bf602e8762be3337f'},
    '.claude/skills/kh/references/engineering-guidance/README.md': {'3da39d111b95cd5e077d68f1564b5bb4eef11dbae5bfa632823df8c373564f09'},
    '.claude/skills/kh/templates/adws/adw_modules/engineering_guidance.py': {'60bffa1e80ff73810cd00827c4c792fd5ed86f7a7402199805c24e4272c2d3ff'},
    '.claude/skills/kh/templates/adws/adw_modules/jev_scout.py': {'2cd774a3ff860f14c343ba152ef46abc703dd106d7439649ca95f6cfaa1f8a0a'},
    '.claude/skills/kh/templates/adws/adw_modules/live_models.py': {'c37a498073993863d34bb39ade8dd5da16ea166f66c5bd762ebff53ec346a684'},
    '.claude/skills/kh/templates/adws/adw_modules/planning_guidance.py': {'7b1c5e86eb1a73e58d439f048975063391f560f7843a2fb2c250c5810db404cb'},
    '.claude/skills/kh/templates/adws/adw_plan.py': {'7c5921f3b70e3dda8ef611b2c7122d1158ca9c5ebc3a88f89934ea91ce63eaae'},
    '.claude/skills/kh/templates/adws/adw_plan_build.py': {'58e792c9de2db30f095ddd7252a27ab5ed3bf1f719698602efc3e8f7d1cd2f62'},
    '.claude/skills/kh/templates/adws/adw_plan_build_test.py': {'4410f705476db731881844fd3981d4f98b9dcd046b250f19117122aa076fb05e'},
    '.claude/skills/kh/templates/adws/adw_plan_build_test_quality.py': {'a49acc2c9640ccfe879cc2bab3e98b34c8e1bf7803d63a3d8f1d74a99d5258a5'},
    '.claude/skills/kh/templates/adws/adw_simple_sdlc_legacy.py': {'86f24f233f62eedf137b0942aaef6e9374e4f5ea9c3d3ad2c2893ce4a27ce4e2'},
    'adws/adw_build.py': {'0d17d735d0fa5e9e7d2a9270255824324fb3f436c0fc2f58fb3787d0691f555d'},
    'adws/adw_build_review.py': {'f7211ccfaaeca3d27858ed4fad866e905ef3b50190f5bc1e4b0abf4a6334fc3a'},
    'adws/adw_build_test.py': {'c9836ddd23fd1d4e9762c5e5e2c3c86ab3015791b086ad4bf602e8762be3337f'},
    'adws/adw_modules/engineering_guidance.py': {'60bffa1e80ff73810cd00827c4c792fd5ed86f7a7402199805c24e4272c2d3ff'},
    'adws/adw_modules/jev_scout.py': {'2cd774a3ff860f14c343ba152ef46abc703dd106d7439649ca95f6cfaa1f8a0a'},
    'adws/adw_modules/live_models.py': {'c37a498073993863d34bb39ade8dd5da16ea166f66c5bd762ebff53ec346a684'},
    'adws/adw_modules/planning_guidance.py': {'7b1c5e86eb1a73e58d439f048975063391f560f7843a2fb2c250c5810db404cb'},
    'adws/adw_plan.py': {'7c5921f3b70e3dda8ef611b2c7122d1158ca9c5ebc3a88f89934ea91ce63eaae'},
    'adws/adw_plan_build.py': {'58e792c9de2db30f095ddd7252a27ab5ed3bf1f719698602efc3e8f7d1cd2f62'},
    'adws/adw_plan_build_test.py': {'4410f705476db731881844fd3981d4f98b9dcd046b250f19117122aa076fb05e'},
    'adws/adw_plan_build_test_quality.py': {'a49acc2c9640ccfe879cc2bab3e98b34c8e1bf7803d63a3d8f1d74a99d5258a5'},
    'adws/adw_simple_sdlc_legacy.py': {'86f24f233f62eedf137b0942aaef6e9374e4f5ea9c3d3ad2c2893ce4a27ce4e2'},
    '.claude/skills/kh/references/execution-decision.md': {'ed2310bb06ed15991cd7f451f611be27932133d3ef6e85f47c63140015471ec4'},
    '.claude/skills/kh/templates/adws/adw_modules/candidate_integration.py': {'4b3e5a96e0a205603790ba23603e6bf59b494812e3b8f74536564414f11928de', '4d80b0cbb4fb1771df1ac613408e485b2c8f24e4a47b68a2d171d6ba420586ed', 'b55e204b93da7cf5a4e54d9db8afee8b20250141fd87653dc91f437c90a20c13', 'c2b280763c9aa388348d61fad47c8d927bdc069f378227d7f618c75a95108043'},
    '.claude/skills/kh/templates/adws/adw_modules/execution_strategy.py': {'2945388c42e24789d8b899708a9297ccfeae6f1c45b382b47f2beeac071f2955', 'bcb874eb6faadc5bd2c3f67deb40e83be8a3f33c7edd1c8051efd0297f7b0b3a'},
    '.claude/skills/kh/templates/adws/adw_modules/native_replay.py': {'e15c5f72b05ad6a26f1c2136198812363ffc34d630cf44bade2eadd704d384b0'},
    '.claude/skills/kh/templates/adws/adw_modules/parallel_candidate.py': {'16bbaee99cc41b67c506f07932c19fd215d9b3c5e2d8e65d9334b9d566d6d97c', '758699c15ebe703882ddc3652aa49fc66323f9990f8ded6f28cd0e922cb99653'},
    '.claude/skills/kh/templates/adws/adw_modules/parallel_execution.py': {'27e3ea8ca3c0530bccc38f00f5f8c1512e187bce535ebca76dd9b2933b204d55', '4280db13e1af376b7d0ab759713db1fda73d28503dad2b843af691b1849ba4e2', '60e9f766830ea2603dea89646cc36000c23dc1d37ae981c1011dbfb31b4c5e6a', '722be9f241f0b58877c0766bdb221783ec27ec3dbd898a4a22b1346db4b51628', '8a1aa3b0ad27d7e74b62f1edca19c86643191c6908a74600abc45231c217593b', 'b4c6062f8cff49edbd33d2dfea4594e240e3f830cad5f78aba550374659c5447'},
    '.claude/skills/kh/templates/adws/adw_modules/shared_context.py': {'53e40170e331c6db25742e85c3b7ffadd91faa1be40c9479c7455d77595601bf', 'c66359e9d4ee7333b8c87f66593695898ca1946fea247dd3599b4f728647440d'},
    'adws/adw_modules/candidate_integration.py': {'4b3e5a96e0a205603790ba23603e6bf59b494812e3b8f74536564414f11928de', '4d80b0cbb4fb1771df1ac613408e485b2c8f24e4a47b68a2d171d6ba420586ed', 'b55e204b93da7cf5a4e54d9db8afee8b20250141fd87653dc91f437c90a20c13', 'c2b280763c9aa388348d61fad47c8d927bdc069f378227d7f618c75a95108043'},
    'adws/adw_modules/execution_strategy.py': {'2945388c42e24789d8b899708a9297ccfeae6f1c45b382b47f2beeac071f2955', 'bcb874eb6faadc5bd2c3f67deb40e83be8a3f33c7edd1c8051efd0297f7b0b3a'},
    'adws/adw_modules/native_replay.py': {'e15c5f72b05ad6a26f1c2136198812363ffc34d630cf44bade2eadd704d384b0'},
    'adws/adw_modules/parallel_candidate.py': {'16bbaee99cc41b67c506f07932c19fd215d9b3c5e2d8e65d9334b9d566d6d97c', '758699c15ebe703882ddc3652aa49fc66323f9990f8ded6f28cd0e922cb99653'},
    'adws/adw_modules/parallel_execution.py': {'27e3ea8ca3c0530bccc38f00f5f8c1512e187bce535ebca76dd9b2933b204d55', '4280db13e1af376b7d0ab759713db1fda73d28503dad2b843af691b1849ba4e2', '60e9f766830ea2603dea89646cc36000c23dc1d37ae981c1011dbfb31b4c5e6a', '722be9f241f0b58877c0766bdb221783ec27ec3dbd898a4a22b1346db4b51628', '8a1aa3b0ad27d7e74b62f1edca19c86643191c6908a74600abc45231c217593b', 'b4c6062f8cff49edbd33d2dfea4594e240e3f830cad5f78aba550374659c5447'},
    'adws/adw_modules/shared_context.py': {'53e40170e331c6db25742e85c3b7ffadd91faa1be40c9479c7455d77595601bf', 'c66359e9d4ee7333b8c87f66593695898ca1946fea247dd3599b4f728647440d'},
    '.claude/skills/kh/references/antigravity.md': {'44ce05a5675263f3b2561d438f04b8d3d687798cca541dcdd36aa854ba25ffde', '97a34c413d5b8612fd9bd6f49824309d52efb5def4b1f7db97efadeb1aed21ea'},
    '.claude/skills/kh/templates/adws/adw_modules/agent_antigravity.py': {'11d986ff8f3f092efa25e3da1164c0fbaf2ef58eb306eb84aae362cbc4394abc', '39870bed9bccd50d3c1abb7bca37dc000b99e4cb1bb05d2d2dc52dfaeb76cbd9', '403f0df931d8d8381e2d0fd4b2ea6f9a8f5513d244abbc0b82a4b4eaaf9376fe'},
    '.claude/skills/kh/templates/adws/adw_modules/antigravity_policy.py': {'5ffb10d180b36bde8238b38c1c22bdc9635b1743f72edc5ca6356189575f51b2', 'e262ca696330815981b4399b73bcfd7ac73b44025978e5175354152d4e426394'},
    'adws/adw_modules/agent_antigravity.py': {'11d986ff8f3f092efa25e3da1164c0fbaf2ef58eb306eb84aae362cbc4394abc', '39870bed9bccd50d3c1abb7bca37dc000b99e4cb1bb05d2d2dc52dfaeb76cbd9', '403f0df931d8d8381e2d0fd4b2ea6f9a8f5513d244abbc0b82a4b4eaaf9376fe'},
    'adws/adw_modules/antigravity_policy.py': {'5ffb10d180b36bde8238b38c1c22bdc9635b1743f72edc5ca6356189575f51b2', 'e262ca696330815981b4399b73bcfd7ac73b44025978e5175354152d4e426394'},
    '.claude/skills/kh/templates/adws/adw_modules/agent_gemini.py': {'10ced89e874d6e32ea9c7c61ab7958602e8657534056f62b86ed1be7c9923e2f', '4fa784e05057a18ca653d0c6f6798633daa2c58130286b5d5c75ed7f262b9b78', 'eebcf3b0653064298ed294ea23ccdfa66ff68082dbcc49a3df607ac95bafe20d'},
    '.claude/skills/kh/templates/adws/adw_modules/agent_pi.py': {'41557db5916b80b35237c5f44949b2d9c0af4c77ac652f3b20c44b904f8d8973', '9532241b7557abda6da873a0ee02a9e6b4635d1bf089a411111b657eacd9d1fc', '99cd2eb91a70f2465a4fcbcd248867133394c2e1e5a9c4f9988c7e467755b1d5', 'bc303aa4b3bb1f70b5ac94b1cc9dad3570882d17de3f178fc6be0b02484caa14'},
    'adws/adw_modules/agent_gemini.py': {'10ced89e874d6e32ea9c7c61ab7958602e8657534056f62b86ed1be7c9923e2f', '4fa784e05057a18ca653d0c6f6798633daa2c58130286b5d5c75ed7f262b9b78', 'eebcf3b0653064298ed294ea23ccdfa66ff68082dbcc49a3df607ac95bafe20d'},
    'adws/adw_modules/agent_pi.py': {'41557db5916b80b35237c5f44949b2d9c0af4c77ac652f3b20c44b904f8d8973', '9532241b7557abda6da873a0ee02a9e6b4635d1bf089a411111b657eacd9d1fc', '99cd2eb91a70f2465a4fcbcd248867133394c2e1e5a9c4f9988c7e467755b1d5', 'bc303aa4b3bb1f70b5ac94b1cc9dad3570882d17de3f178fc6be0b02484caa14'},
}


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Symlink paths are not supported: {part}")
    for parent in path.parents:
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Ancestor is not a directory: {parent}")
    if path.exists() and not (path.is_dir() or path.is_file()):
        raise ValueError(f"Unsupported filesystem object: {path}")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect(directory: Path, prefix: Path, desired: dict, *, excluded_prefixes=()) -> None:
    if not directory.is_dir():
        raise ValueError(f"Package directory missing: {directory}")
    safe_path(directory)
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory)
        if any(part in EXCLUDED for part in rel.parts) or path.suffix == ".pyc":
            continue
        destination = prefix / rel
        if any(destination == item or item in destination.parents for item in excluded_prefixes):
            continue
        safe_path(path)
        if path.is_file():
            key = (prefix / rel).as_posix()
            if key in desired:
                raise ValueError(f"Duplicate package destination: {key}")
            desired[key] = (path.read_bytes(), path.stat().st_mode & 0o777)


def desired_files(package: Path, user: bool, with_visualizer: bool = False) -> dict:
    desired = {}
    collect(package / ".claude/commands", Path(".claude/commands"), desired)
    if user:
        return desired
    if with_visualizer and not (package / VISUALIZER / "package.json").is_file():
        raise ValueError("Visualizer source missing; use the complete KH distribution")
    collect(package / ".claude/skills", Path(".claude/skills"), desired,
            excluded_prefixes=() if with_visualizer else (VISUALIZER,))
    catalog = package / "COMMANDS.md"
    safe_path(catalog)
    desired[".claude/skills/kh/references/commands.md"] = (
        catalog.read_bytes(), catalog.stat().st_mode & 0o777)
    collect(package / "vendor/delegate-skills", Path("vendor/delegate-skills"), desired)
    # Render runtime in an isolated staging project before touching the target.
    installer = package / ".claude/skills/kh/scripts/install.py"
    safe_path(installer)
    # macOS commonly exposes its temporary directory through /var -> /private/var.
    # Use the physical staging path while keeping target-path symlink checks strict.
    with tempfile.TemporaryDirectory(prefix="kh-stage-",
                                     dir=Path(tempfile.gettempdir()).resolve()) as temporary:
        stage = Path(temporary)
        result = subprocess.run([sys.executable, str(installer)], cwd=stage,
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if result.returncode:
            raise ValueError(f"Runtime staging failed: {result.stderr or result.stdout}")
        collect(stage, Path(), desired)
    return desired


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".kh-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        safe_path(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prompt_values(text: str) -> list[str]:
    """Use safe YAML when available; standalone installs accept literal paths."""
    try:
        import yaml
    except ImportError:
        yaml = None
    values = []
    if yaml is not None:
        try:
            pending = [yaml.safe_load(text)]
        except yaml.YAMLError as error:
            raise ValueError("Cannot safely read project prompt references: invalid YAML") from error
        visited = set()
        while pending:
            item = pending.pop()
            if not isinstance(item, (dict, list)) or id(item) in visited:
                continue
            visited.add(id(item))
            if isinstance(item, dict):
                values.extend(value for key, value in item.items()
                              if key in ("system", "user") and isinstance(value, str))
                pending.extend(item.values())
            else:
                pending.extend(item)
        return values

    quoted = r'''(?:"(?:\\.|[^"\\])*"|'(?:''|[^'])*')'''
    # The fallback supports single-line flow mappings only. Validate delimiters
    # before extracting any paths, so a multiline flow separator cannot become
    # part of a filename and silently exclude the owner's actual prompt.
    # Do not let an apostrophe in a plain scalar consume later lines and hide
    # a real multiline map. Multiline quoted values already require safe YAML.
    single_line_quoted = r'''(?:"(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:''|[^'\r\n])*')'''
    structural = re.sub(single_line_quoted, "''", text)
    structural = re.sub(r"(?m)(?:^[ \t]*|[ \t]+)#.*$", "", structural)
    for line in structural.splitlines():
        stack = []
        for char in line:
            if char in "{[":
                stack.append(char)
            elif char in "}]":
                if not stack or stack.pop() != {"}": "{", "]": "["}[char]:
                    raise ValueError("Unsupported flow YAML; use block form or install PyYAML")
        if stack:
            raise ValueError("Multiline flow YAML requires block form or PyYAML")
    key = f"(system|user|{quoted})"
    patterns = (
        rf"(?m)^[ \t]*{key}[ \t]*:[ \t]*({quoted}|[^\r\n]+)",
        rf"(?m)[{{,][ \t]*{key}[ \t]*:[ \t]*({quoted}|[^,}}\r\n]+)",
    )
    def literal(value):
        value = value.strip()
        if "\n" in value or "\r" in value:
            raise ValueError("Use literal system/user prompt paths or install PyYAML")
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        if value.startswith('"'):
            try:
                return json.loads(value)
            except ValueError as error:
                raise ValueError("Use literal system/user prompt paths or install PyYAML") from error
        value = re.sub(r"\s+#.*$", "", value).strip()
        if value.startswith(("*", "&", "!", "|", ">")):
            raise ValueError("Use literal system/user prompt paths or install PyYAML")
        return value
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            if literal(match.group(1)) in ("system", "user"):
                values.append(literal(match.group(2)))
    return values


def referenced_prompts(root: Path, data: bytes) -> set[str]:
    """Return project-local system/user paths; callers intersect with destinations."""
    paths = set()
    for value in prompt_values(data.decode("utf-8")):
        path = Path(value)
        if ".." in path.parts:
            continue
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                continue
        paths.add(path.as_posix())
    return paths


def keep_project_owned(root: Path, desired: dict, *, reset_config=False) -> list:
    """Drop project-owned files that already exist and differ from the package.

    Returns (relative path, package bytes) for each one kept, so the caller can
    report it and save the package's version for comparison. A file that is
    missing or identical stays in `desired` and is handled as usual.
    """
    owned = set() if reset_config else set(PROJECT_OWNED)
    owned.update(rel for rel in desired if PROJECT_PROMPTS in Path(rel).parents)
    # A config reset is not authorization to replace its custom prompt files.
    config = root / PROJECT_OWNED[0]
    safe_path(config)
    data = config.read_bytes() if config.is_file() else None
    if data is not None:
        owned.update(referenced_prompts(root, data))
    kept = []
    for rel in sorted(owned):
        target = root / rel
        if rel not in desired or not target.exists():
            continue
        safe_path(target)
        if not target.is_file():
            continue
        data = desired[rel][0]
        if target.read_bytes() != data:
            kept.append((rel, data))
            del desired[rel]
    return kept


def save_package_versions(root: Path, kept: list) -> list:
    """Write the package's copy of each kept file under .kh-backups/."""
    saved = []
    if not kept:
        return saved
    folder = root / ".kh-backups"
    safe_path(folder)
    folder.mkdir(mode=0o700, exist_ok=True)
    for rel, data in kept:
        name = f"{Path(rel).stem}.package-{digest(data)[:8]}{Path(rel).suffix}"
        # Keep the existing config-copy contract, but retain role/path identity
        # for prompts: identical builder/reviewer system.md must not collide.
        path = (folder / name if rel in PROJECT_OWNED else
                folder / "package-files" / Path(rel).parent / name)
        safe_path(path)
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data, 0o600)
        saved.append(path)
    return saved


def install(root: Path, *, user=False, upgrade=False, dry_run=False,
            package: Path = PACKAGE, with_visualizer=False, reset_config=False) -> dict:
    root = absolute(root)
    safe_path(root)
    if not root.is_dir():
        raise ValueError(f"Target directory does not exist: {root}")
    if user and with_visualizer:
        raise ValueError("--with-visualizer requires --project")
    desired = desired_files(package, user, with_visualizer)
    current_paths = set(desired)
    kept = [] if user else keep_project_owned(root, desired, reset_config=reset_config)
    if not user:
        path = root / ".gitignore"
        safe_path(path)
        if path.exists() and not path.is_file():
            raise ValueError(f"Not a file: {path}")
        original = path.read_bytes() if path.exists() else b""
        lines = original.decode("utf-8").splitlines()
        requested = desired.get(".gitignore", (b"", 0o644))[0].decode().splitlines()
        additions = [line for line in requested if line and not line.startswith("#") and line not in lines]
        if ".kh-backups/" not in lines:
            additions.append(".kh-backups/")
        data = original
        if additions:
            data += (b"\n" if data and not data.endswith(b"\n") else b"")
            data += ("\n# KH runtime\n" + "\n".join(dict.fromkeys(additions)) + "\n").encode()
        desired[".gitignore"] = (data, path.stat().st_mode & 0o777 if path.exists() else 0o644)
    changes, conflicts, unchanged = [], [], 0
    for rel, (data, mode) in sorted(desired.items()):
        target = root / rel
        safe_path(target)
        if target.exists() and not target.is_file():
            raise ValueError(f"Target is not a regular file: {target}")
        old = target.read_bytes() if target.exists() else None
        if old == data:
            unchanged += 1
            continue
        if old is not None and rel != ".gitignore":
            conflicts.append(rel)
        changes.append((rel, data, mode, old,
                        target.stat().st_mode & 0o777 if old is not None else None))
    kept_retired = []
    if upgrade:
        for rel, hashes in sorted(RETIRED_PATHS.items()):
            path = Path(rel)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.as_posix() != rel:
                raise ValueError(f"Unsafe retired path: {rel}")
            if (rel in current_paths or (user and Path('.claude/commands') not in path.parents)
                    or (not with_visualizer and VISUALIZER in path.parents)):
                continue
            target = root / path
            safe_path(target)
            if not target.exists():
                continue
            if not target.is_file():
                raise ValueError(f"Retired target is not a regular file: {target}")
            old = target.read_bytes()
            if digest(old) not in hashes:
                kept_retired.append({"path": rel, "sha256": digest(old),
                                     "note": "modified or unknown retired version; preserved"})
                continue
            changes.append((rel, None, 0o644, old, target.stat().st_mode & 0o777))
    report = {"root": str(root), "mode": "user commands" if user else "project",
              "components": ["commands"] if user else ["core"] + (["visualizer"] if with_visualizer else []),
              "unchanged": unchanged, "changes": [
                  {"path": rel, "action": "remove" if data is None else "replace" if old is not None else "create",
                   "old_sha256": digest(old) if old is not None else None,
                   "new_sha256": digest(data) if data is not None else None,
                   **({"message": f"removed (retired): {rel}"} if data is None else {})}
                  for rel, data, mode, old, old_mode in changes],
              "conflicts": conflicts, "upgrade_required": bool(conflicts and not upgrade)}
    if kept_retired:
        report["kept_retired_files"] = kept_retired
    if kept:
        report["kept_project_files"] = [
            {"path": rel, "package_sha256": digest(data),
             "note": "existing project file kept; the package version differs"}
            for rel, data in kept]
    if dry_run:
        return report
    if conflicts and not upgrade:
        raise ValueError("Different existing files preserved; inspect --dry-run, then use --upgrade if intended:\n" + "\n".join(conflicts))
    saved = save_package_versions(root, kept)
    if saved:
        for entry, path in zip(report["kept_project_files"], saved):
            entry["package_version_saved_as"] = str(path)
            entry["compare"] = f'diff "{root / entry["path"]}" "{path}"'
    if not changes:
        return report
    backup = root / ".kh-backups" / uuid.uuid4().hex
    safe_path(backup)
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"version": 1, "root": str(root), "components": report["components"], "files": []}
    for rel, data, mode, old, old_mode in changes:
        if old is not None:
            saved = backup / "originals" / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(old)
            saved.chmod(0o600)
        manifest["files"].append({"path": rel, "existed": old is not None,
                                  "original_mode": old_mode,
                                  "original_sha256": digest(old) if old is not None else None,
                                  "installed_sha256": digest(data) if data is not None else None})
    (backup / "restore.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (backup / "restore.json").chmod(0o600)
    applied = []
    try:
        for rel, data, mode, old, old_mode in changes:
            target = root / rel
            safe_path(target)
            current = target.read_bytes() if target.exists() else None
            if current != old:
                raise ValueError(f"Target changed during installation: {target}")
            if data is None:
                target.unlink()
            else:
                atomic_write(target, data, old_mode if old_mode is not None else mode)
            applied.append((rel, old, old_mode))
    except Exception:
        for rel, old, old_mode in reversed(applied):
            target = root / rel
            safe_path(target)
            if old is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, old, old_mode)
        raise
    report["backup"] = str(backup)
    report["restore_command"] = f'python3 install.py --restore "{backup}"'
    return report


def restore(backup: Path, dry_run=False) -> dict:
    backup = absolute(backup)
    safe_path(backup)
    manifest_path = backup / "restore.json"
    safe_path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    root = absolute(Path(manifest["root"]))
    safe_path(root)
    if backup.parent != root / ".kh-backups" or manifest.get("version") != 1:
        raise ValueError("Backup is not a supported KH restore plan for this target")
    prepared = []
    seen = set()
    for entry in manifest["files"]:
        rel = Path(entry["path"])
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.as_posix() in seen:
            raise ValueError("Unsafe or duplicate restore path")
        seen.add(rel.as_posix())
        target = root / rel
        safe_path(target)
        if target.exists() and not target.is_file():
            raise ValueError(f"Changed after installation; restore preserved current work: {target}")
        installed = target.read_bytes() if target.exists() else None
        if (digest(installed) if installed is not None else None) != entry["installed_sha256"]:
            raise ValueError(f"Changed after installation; restore preserved current work: {target}")
        data = None
        if entry["existed"]:
            saved = backup / "originals" / rel
            safe_path(saved)
            data = saved.read_bytes()
            if digest(data) != entry["original_sha256"]:
                raise ValueError(f"Backup checksum mismatch: {saved}")
        prepared.append((target, data, entry["original_mode"], installed))
    if not dry_run:
        restored = []
        try:
            for target, data, mode, installed in reversed(prepared):
                safe_path(target)
                current = target.read_bytes() if target.exists() else None
                if current != installed:
                    raise ValueError(f"Changed during restore; preserved current work: {target}")
                installed_mode = target.stat().st_mode & 0o777 if installed is not None else None
                if data is None:
                    target.unlink()
                else:
                    atomic_write(target, data, mode)
                restored.append((target, installed, installed_mode))
        except Exception:
            for target, installed, installed_mode in reversed(restored):
                safe_path(target)
                if installed is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, installed, installed_mode)
            raise
    return {"restored_files": len(prepared), "dry_run": dry_run, "root": str(root)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--project", type=Path, help="Install skills, commands, delegate vendor, and runtime into ROOT")
    mode.add_argument("--user", action="store_true", help="Install only the 18 commands into ~/.claude/commands")
    mode.add_argument("--restore", type=Path, help="Restore a recorded installation backup; rejects subsequent edits")
    parser.add_argument("--dry-run", action="store_true", help="Print changes and conflicts without writing target files")
    parser.add_argument("--upgrade", action="store_true",
                        help="Replace differing framework files after backing up originals; "
                             "keep existing project config and prompts")
    parser.add_argument("--with-visualizer", action="store_true",
                        help="Include optional visualizer source; installs no Node/Bun packages")
    parser.add_argument("--reset-config", action="store_true",
                        help="Replace the project's kh.config.yaml with the package template "
                             "(with --upgrade; the old file is still backed up). By default an "
                             "existing project configuration is kept. Project prompts are always kept")
    args = parser.parse_args()
    if args.reset_config and not args.project:
        parser.error("--reset-config requires --project")
    if args.with_visualizer and not args.project:
        parser.error("--with-visualizer requires --project")
    try:
        if args.restore:
            if args.upgrade:
                parser.error("--upgrade is not valid with --restore")
            report = restore(args.restore, args.dry_run)
        else:
            report = install(Path.home() if args.user else args.project,
                             user=args.user, upgrade=args.upgrade, dry_run=args.dry_run,
                             with_visualizer=args.with_visualizer,
                             reset_config=args.reset_config)
        print(json.dumps(report, indent=2))
        return 2 if report.get("upgrade_required") else 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Installation stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
