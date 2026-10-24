# Log reporting fixture

Run `python3 -m logreports input.jsonl` or
`python3 -B -m unittest discover -s tests`.

This isolated fixture needs no network service, API key or external application.

## Execution responsibilities

All model roles use file tools only; the builder has no native shell permission.
The controller executes configured test and build checks on changed source.
Keep those checks and meaningful report tests. Native required_test_commands
need explicit owner permission; a plan cannot create a permission grant.
