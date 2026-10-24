# Pinned delegate transport

Source: [amElnagdy/delegate-skills](https://github.com/amElnagdy/delegate-skills/tree/b781ee2e23089630e2fbee1cfd6174afe4edeb76)

Upstream commit: `b781ee2e23089630e2fbee1cfd6174afe4edeb76`.
License: MIT; original copyright notice is retained in `LICENSE`.

This distribution includes only the standalone Codex relay, which uses Node's
built-in modules and invokes the separately installed `codex` CLI. No npm install
is required for the relay. No delegation skills are auto-activated. Fleet lane
setup, alternative providers, hooks, background queue orchestration and optional
upstream setup scripts are intentionally outside this adapter's supported surface.

The included `skills/codex-delegate/scripts/relay.mjs` enforces:

1. Reject the `danger-full-access` sandbox option from accepted values and help.
2. Pass `approval_policy="never"` to Codex. An unavailable permission fails;
   unattended dispatch does not escalate or weaken the selected sandbox.
3. Disable implicit `/tmp` and `$TMPDIR` write roots, extra writable roots, and
   sandboxed-command network access. Writable scope stays with the working copy.
   These config keys were verified in OpenAI's Codex config schema; real CLI
   compatibility is still a required pilot check.

The KH Python adapter adds strict result/terminal-event validation, explicit
context and scope, a bounded supervisor, provider-token normalization, and a
receipt stating `accepted: false`. It always supplies an explicit sandbox and
ignores ambient Codex user config to avoid inheriting unrelated project tools or
permission settings. Authentication remains with the user's installed CLI.

Supported integration entry point: `adw_modules.agent_delegate.run`.
Do not invoke this relay directly to bypass KH's isolation and promotion gates.

See [.claude/skills/kh/references/delegation.md](../../.claude/skills/kh/references/delegation.md)
for integration requirements and environment verification limits.
