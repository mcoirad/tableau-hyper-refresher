# Tableau Hyper Refresher

Tableau Hyper Refresher is a reference AWS workflow for rebuilding or
incrementally updating published Tableau Hyper extracts from Amazon Redshift.
The default Redshift driver for Tableau only runs extract refreshes on the Redshift lead node, underutilizing multi node setups.

The `Tableau Hyper Refresher` can massively parallelize your Tableau extract refreshes, making full use of your Redshift cluster, achieving 2x performance in the test use cases.

This project is provided as-is, without a support SLA. Issues and pull requests
are welcome. Tableau and Tableau Hyper are trademarks of Salesforce, Inc.; this
independent project is not affiliated with or endorsed by Salesforce or Tableau.

## What it does

The Step Functions workflow performs four stages:

1. Discover scheduled Tableau datasources carrying the configured refresh tag.
2. Inspect downloaded TDSX metadata and build a deduplicated Redshift unload plan.
3. Run one AWS Batch unload job for each source table or custom SQL expression.
4. Update each datasource using `full_replace`, `upsert`, or `merge`, then publish it.

The implementation supports provisioned Redshift and Redshift Serverless. It
does not support Snowflake or other warehouses (and is probably not needed in those usecases)

## Costs and prerequisites

Deployment creates ECR repositories, two S3 buckets, three DynamoDB tables,
CloudWatch log groups, two Lambda functions, an AWS Batch Fargate environment,
and a Standard Step Functions state machine.

You need:

- Python 3.12, `uv`, Docker, Terraform 1.6+, AWS CLI v2, and AWS credentials.
- Existing VPC subnets and security groups with Redshift connectivity and HTTPS
  egress to Tableau.
- An existing Redshift cluster or Serverless workgroup.
- A Redshift IAM role authorized to `UNLOAD` into the generated work bucket.
- A Tableau connected app with REST API scopes required for content reads,
  datasource download/update, file uploads, tasks, tags, jobs, and users.

## Secrets

Create two JSON values in AWS Secrets Manager.

Connected-app secret:

```json
{
  "client_id": "connected-app-client-id",
  "secret_id": "connected-app-secret-id",
  "secret_value": "connected-app-secret-value",
  "user": "service-account@example.com"
}
```

Datasource Redshift credentials embedded when republishing:

```json
{
  "username": "tableau_reader",
  "password": "replace-me"
}
```

Never commit these values or put them in Terraform variables. Terraform accepts
only their Secrets Manager ARNs.

## Configuration

Refresh behavior is controlled by versioned YAML. Copy
[`examples/refresh-config.yaml`](examples/refresh-config.yaml) and edit it before
building the Batch image. `full_replace` is the safe default. `upsert` requires
a primary key and sort column. `merge` adds delete handling through an
`is_deleted` boolean or `deleted_at` timestamp.

Runtime configuration is supplied through Terraform-generated environment
variables. See [`docs/configuration.md`](docs/configuration.md) for the complete
contract.

## Local development

```bash
uv sync --all-extras
uv run pytest -m "not e2e"
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The compatible CLI surface is:

```bash
uv run tableau-hyper-refresher inspect --datasource-id UUID --force-full-load false
uv run tableau-hyper-refresher unload --table-text analytics.events --table-type table --table-key analytics.events
uv run tableau-hyper-refresher refresh --datasource-id UUID --refresh-config examples/refresh-config.yaml
```

These commands require the same AWS, Tableau, Redshift, S3, and DynamoDB
environment variables as deployed jobs.

## Deploy to AWS

Copy and edit the example variables:

```bash
cp deploy/aws/stack/terraform.tfvars.example deploy/aws/stack/terraform.tfvars
```

Bootstrap ECR, build and push both source images, then deploy the stack:

```bash
AWS_REGION=us-east-1 IMAGE_TAG=v0.1.0 make bootstrap
AWS_REGION=us-east-1 IMAGE_TAG=v0.1.0 make images
AWS_REGION=us-east-1 IMAGE_TAG=v0.1.0 make deploy
```

The ECR repositories use immutable tags. Choose a new tag after changing either
image. Terraform state is local by default; production users should configure a
remote backend before applying.

The Redshift unload role supplied to Terraform must separately allow writes to
the work bucket output by the stack. The deployment intentionally does not
modify an existing Redshift role.

Start a full refresh through the `state_machine_alias_arn` output:

```json
{ "force_full_load": true }
```

To target specific datasources without tag discovery:

```json
{
  "force_full_load": false,
  "datasources": ["tableau-datasource-luid"]
}
```

## Teardown

The stack contains data-bearing resources. Review the Terraform plan, empty S3
buckets if necessary, and explicitly confirm destruction:

```bash
CONFIRM_DESTROY=yes AWS_REGION=us-east-1 IMAGE_TAG=v0.1.0 make destroy
```

The confirmed target destroys the runtime stack first and then the bootstrap
ECR repositories, including their private images.

## Troubleshooting

- A Batch task in private subnets needs NAT or equivalent HTTPS egress for
  Tableau, AWS APIs, and ECR image pulls.
- JWT failures usually indicate a wrong site ID, connected-app subject, secret,
  scope, or system clock.
- Redshift `UNLOAD` failures usually indicate that the cluster/workgroup cannot
  assume the unload role or that the role cannot write to the work bucket.
- Large TDSX files need the refresh job's configured 40 GiB ephemeral storage.
- The workflow advances incremental watermarks only after Tableau publication
  succeeds, so failed publishes can safely be retried.

See [`docs/architecture.md`](docs/architecture.md) and
[`docs/live-e2e.md`](docs/live-e2e.md) for more detail.
