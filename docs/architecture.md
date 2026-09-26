# Architecture

```text
Step Functions
  ├─ Lambda: discover tagged datasources
  ├─ AWS Batch: inspect TDSX metadata
  ├─ Lambda: deduplicate the unload plan
  ├─ AWS Batch map: Redshift UNLOAD → S3
  └─ AWS Batch map: mutate Hyper → publish datasource

DynamoDB stores datasource metadata, datasource/table watermarks, and shared
table unload state. The transient S3 bucket holds Redshift output and generated
Hyper files; its objects expire after 14 days. The retained bucket holds source
and refreshed TDSX packages and has versioning enabled.
```

The two Lambda functions share one lightweight image. All three Batch job
definitions share one worker image but have independent CPU, memory, command,
and ephemeral-storage settings.

Incremental watermarks are advanced only after a refreshed datasource is
published successfully. The unload stage uses a lookback window and the lowest
applicable datasource watermark, allowing shared Redshift tables to be reused
without skipping data for a lagging datasource.

