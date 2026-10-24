# Task-kind planning support

`execution.task_kind` selects `generic`, `bug`, `feature` or `maintenance` before a
task starts. The controller supplies this value as data to the planner and
independent plan reviewer; canonical prompts state the advisory duties directly.
No recipe helper or extra model call is needed. The value does not select models
or change scope/checks. A changed task kind changes plan identity, so an old
approval is not silently reused under a different selection.

For a bug, record the expected and observed result, a safe reproduction and the
first incorrect boundary. Identify the cause in source and a regression that
fails on the broken behavior and passes on the correction. If reproduction is
unavailable, name the limitation rather than fabricate a failed run.

For a feature, describe the actual user journey and its measurable outcome.
Inspect callers, consumers, configuration and verification dependencies before
listing exact paths. Include negative and empty states. UI evidence should show
outcomes, such as actual downloaded content, rather than just an attractive view.

For maintenance, state the compatibility behavior that must survive. Separate
preservation from intentional change and add migration/rollback verification only
when the task actually needs it. Do not add libraries or source edits during
read-only planning. Documentation impact remains part of the existing plan.
