# Report interface

`summarize(records)` accepts a list of dictionaries produced by the unchanged
core loader: nonempty service string, integer HTTP status 200..599, nonnegative
finite duration_ms and UTC-normalized ISO timestamp. Return a JSON-serializable
object. Do not mutate input, access external services or change the loader/CLI.
