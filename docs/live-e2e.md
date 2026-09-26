# Opt-in live end-to-end validation

Live validation is intentionally excluded from CI. Use a non-production Tableau
site/project, disposable Redshift tables, and dedicated AWS resources.

1. Publish a small datasource containing a Hyper extract and tag it with the
   configured refresh tag.
2. Populate a source table with a stable primary key, an `updated_at` column,
   and a nullable delete marker.
3. Deploy the stack and start an execution with that datasource LUID and
   `force_full_load: true`.
4. Verify the published row set and DynamoDB watermarks.
5. Insert, update, and delete fixture rows; start an incremental execution.
6. Verify inserts, latest-row updates, deletions, and watermark advancement.
7. Run the same execution input again and confirm idempotent results.
8. Review CloudWatch logs for secrets or credentials before teardown.
9. Empty generated buckets and run the confirmed destroy target.

Do not reuse production datasource IDs, account IDs, schemas, or secrets in
committed tests, screenshots, issues, or logs.

