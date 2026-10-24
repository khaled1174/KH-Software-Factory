# Project verification examples

The executable formatter is installed with the runtime at
`adws/adw_modules/acceptance_recipe.py`. It consumes real project-harness results;
it does not run a dummy application or report a placeholder as passed.

Use `references/acceptance-recipes.md` in the KH skill for concrete UI-export and
backend-unavailable cases, owned-resource observations and configuration readiness.
Implement that harness inside the actual project's authorized test paths, then
configure its existing argv as a verification adapter. No optional recipe is
activated by installation or merely by appearing in this directory.
