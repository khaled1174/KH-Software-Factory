# Adapted engineering guidance

Read this reference when maintaining role guidance or reviewing its authority and
upstream provenance. It is maintainer documentation, not an executable skill.

The runtime source is `templates/adws/adw_modules/engineering_guidance.py` relative
to the KH skill root. Its `for_role` function returns reviewed instructions and a
receipt. The controller supplies canonical role and configured task kind; source
files, user prose and provider labels do not activate instructions by substring.
Unknown roles receive no guidance. The receipt identifies the delivered text; it
does not prove model comprehension or compliance.

Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) at
`49dd158d1076134a641b33efb035946536778336`. These are locally written summaries with
KH-specific restrictions. No upstream execution workflow is installed. The
upstream MIT notice is preserved in `LICENSE.mattpocock`.

| Source skill | Adoption and differences |
|---|---|
| engineering/to-tickets | Outcome-oriented tickets, blocking dependencies and staged migrations. Preserve KH `allowed_paths` and required checks. Propose to the controller; no issue-tracker publication or second approval loop. |
| engineering/diagnosing-bugs | Distinguishing reproduction, falsifiable hypotheses, regression and cleanup proof. Existing permissions and deadlines remain binding. Inaccessible reproduction is disclosed, not worked around. |
| engineering/tdd | Behavioral tests and small verified changes. Test scope comes from the reviewed plan; repeated owner approvals are removed. Harmless documentation changes stay proportionate. |
| engineering/code-review | Requirements and standards both matter. Retain the full KH review baseline and schema. Missing requirements cannot waive review. No nested reviewer scheduler. |
| productivity/writing-for-agents | Concise navigation, explicit reading conditions and completion criteria. Keep mandatory rules unconditional and preserve the existing documentation publication contract. |
| engineering/domain-modeling | Consistent terms, observed behavior and recorded tradeoffs. Reuse approved targets in the two existing documentation directories. No extra root glossary or ADR tree. |
| engineering/codebase-design | Conditional interface, compatibility and testability guidance. Preserve project vocabulary and scope; no forced renaming or architecture redesign. |
| engineering/retro | Evidence-backed proposals to improve navigation, rules or deterministic checks. No rule installation, execution or task acceptance. |

All source paths above end in `/SKILL.md` under upstream `skills/`. Complete source
paths and the upstream revision are attached to each selected guidance receipt.
`implement` and `implement-spec` are not adopted: their commit, scheduling and merge
responsibilities belong to KH and owner-authorized workflows. Copying reviewed
upstream files into a repository does not activate them.

There are no new tools, credentials, network calls, model choices or publication
permissions. Delivery is at most 8,000 characters per invocation and adds only
role-relevant notes. Bug diagnosis is limited to explicit `bug` builder tasks.
Design guidance is limited to `feature` or `maintenance` tasks for design-capable
roles and still applies only if the approved work changes interfaces or architecture.

Tests establish deterministic selection, size, provenance, side-effect freedom
and explicit authority boundaries. They do not measure quality improvements on
live model runs. The controller's permissions, evidence gates, independent review
and checked promotion remain the enforcement mechanisms.
