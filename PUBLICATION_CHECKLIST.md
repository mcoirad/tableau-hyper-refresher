# Publication checklist

The repository must remain private until every item is checked.

- [ ] The copyright holder has approved publication and Apache-2.0 licensing.
- [ ] `NOTICE` and `AUTHORS.md` attribution has been reviewed.
- [ ] Repository and clean Git history pass secret scanning.
- [ ] Searches find no internal names, domains, account IDs, ARNs, emails,
      datasource IDs, schemas, table names, logs, or TDS/TDSX exports outside
      attribution and deliberately fake examples.
- [ ] `uv sync --all-extras`, tests, lint, type checks, and both Docker builds
      pass from a fresh clone.
- [ ] Terraform format, validation, and TFLint pass.
- [ ] A private full refresh and incremental refresh pass against disposable
      Tableau and Redshift fixtures.
- [ ] Repeating the incremental execution is idempotent.
- [ ] Terraform teardown completes and retained artifacts are reviewed.
- [ ] The repository is changed to public only after all preceding gates pass.
- [ ] Tag `v0.1.0` and create a source-only GitHub release.
