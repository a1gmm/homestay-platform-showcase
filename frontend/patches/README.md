# braces 3.0.3 depth mitigation

The development toolchain resolves `eslint-config-next -> fast-glob -> micromatch -> braces`.
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) has no upstream patched version as checked on 2026-10-04.

The pnpm patch bounds parsed nesting to 100 and checks caller-supplied AST depth iteratively before compile, expand and stringify recurse. Ordinary glob behavior is unchanged; unusually nested patterns fail with an explicit `SyntaxError`. `lib/__tests__/braces-depth-patch.test.ts` checks the installed transitive dependency, including an 8,001-character hostile pattern and direct AST inputs.

`pnpm audit` still reports the upstream package version: no advisory suppression or version spoofing is used. This is a local mitigation, not a claim that upstream has fixed the advisory. Replace it with an upstream release when one is available and rerun the regression and toolchain checks. Production dependency audit is checked separately.
