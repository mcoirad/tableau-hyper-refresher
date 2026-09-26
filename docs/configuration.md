# Configuration reference

## Refresh YAML

The document must contain `version: 1`. `defaults` applies to every table that
does not have an explicit entry under `tables`.

| Field | Meaning |
| --- | --- |
| `mode` | `full_replace`, `upsert`, or `merge` |
| `primary_key` | One or more source columns identifying a row |
| `sort_column` | Timestamp or monotonic value selecting the latest row |
| `incremental.enabled` | Use the stored watermark to restrict Redshift input |
| `incremental.lookback_minutes` | Re-read a window before the watermark |
| `delete.mode` | `none`, `is_deleted`, or `deleted_at` |
| `delete.column` | Delete marker used by `merge` |

## Runtime environment

Terraform sets these variables for Batch and Lambda where applicable:

- `AWS_BUCKET`, `AWS_PREFIX`
- `TABLEAU_HYPER_REFRESHER_BUCKET`, `TABLEAU_HYPER_REFRESHER_RETAINED_BUCKET`
- `TABLEAU_DATASOURCES_TABLE`, `TABLEAU_DATASOURCE_TABLES_TABLE`, `TABLEAU_TABLES_TABLE`
- `TABLEAU_REGION_NAME`, `TABLEAU_SERVER_URL`, `TABLEAU_SITE_ID`, `TABLEAU_SECRET_NAME`
- `TABLEAU_REDSHIFT_SECRET_NAME`, `TABLEAU_REFRESH_TAG`, `TABLEAU_PRODUCTION_TAG`
- `REDSHIFT_DB`, `REDSHIFT_CLUSTER_ID` or `REDSHIFT_SERVERLESS_WORKGROUP`
- `REDSHIFT_SECRET_ARN`
- `REDSHIFT_UNLOAD_ROLE_ARN`

Exactly one Redshift target must be configured. The Data API secret and both
Tableau-related Secrets Manager ARNs are required Terraform inputs.
