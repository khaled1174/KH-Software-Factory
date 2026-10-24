# Install

Namespace migration is retired. For a former legacy project, use a fresh current
KH installation when the owner requests one. Never reuse or convert old state.

Use the KH Software Factory package root installer:
python3 install.py --project /absolute/project/path

--dry-run previews the manifest. Existing conflicting files stop installation.
The explicit --upgrade flow backs up affected files and provides restoration.
Never overwrite credentials or invent quality commands.

python3 install.py --user installs slash prompts for all projects. Each project
still needs its own KH runtime and configuration.

The skill's own scripts/install.py remains a template installer for an existing
skill folder; run from the target project root. It skips existing files. Prefer
the root installer's backed-up upgrade flow over the legacy --force option.

Configure actual CLI access and quality commands, then start with /kh-help and
/kh-scout in a disposable project. No model CLI or account is installed here.
