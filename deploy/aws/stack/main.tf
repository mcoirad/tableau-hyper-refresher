data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_ecr_repository" "batch" {
  name = "${var.name_prefix}-batch"
}

data "aws_ecr_repository" "lambda" {
  name = "${var.name_prefix}-lambda"
}

check "redshift_target" {
  assert {
    condition     = (var.redshift_cluster_id == null) != (var.redshift_serverless_workgroup == null)
    error_message = "Set exactly one of redshift_cluster_id or redshift_serverless_workgroup."
  }
}

locals {
  common_tags          = merge({ Project = var.name_prefix }, var.tags)
  bucket_base          = substr("${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}", 0, 53)
  work_bucket_name     = "${local.bucket_base}-work"
  retained_bucket_name = "${local.bucket_base}-retained"
  batch_image_uri      = "${data.aws_ecr_repository.batch.repository_url}:${var.image_tag}"
  lambda_image_uri     = "${data.aws_ecr_repository.lambda.repository_url}:${var.image_tag}"
  discover_function_name = coalesce(
    var.discover_function_name,
    "${var.name_prefix}-discover"
  )
  plan_function_name = coalesce(
    var.plan_function_name,
    "${var.name_prefix}-plan"
  )

  runtime_environment = [
    { name = "AWS_REGION", value = var.aws_region },
    { name = "AWS_BUCKET", value = local.work_bucket_name },
    { name = "AWS_PREFIX", value = "tables" },
    { name = "TABLEAU_HYPER_REFRESHER_BUCKET", value = local.work_bucket_name },
    { name = "TABLEAU_HYPER_REFRESHER_RETAINED_BUCKET", value = local.retained_bucket_name },
    { name = "TABLEAU_DATASOURCES_TABLE", value = aws_dynamodb_table.datasources.name },
    { name = "TABLEAU_DATASOURCE_TABLES_TABLE", value = aws_dynamodb_table.datasource_tables.name },
    { name = "TABLEAU_TABLES_TABLE", value = aws_dynamodb_table.tables.name },
    { name = "TABLEAU_REGION_NAME", value = var.aws_region },
    { name = "TABLEAU_SERVER_URL", value = var.tableau_server_url },
    { name = "TABLEAU_SITE_ID", value = var.tableau_site_id },
    { name = "TABLEAU_SECRET_NAME", value = var.tableau_secret_arn },
    { name = "TABLEAU_REDSHIFT_SECRET_NAME", value = var.tableau_redshift_secret_arn },
    { name = "TABLEAU_REFRESH_TAG", value = var.tableau_refresh_tag },
    { name = "TABLEAU_PRODUCTION_TAG", value = var.tableau_production_tag },
    { name = "REDSHIFT_DB", value = var.redshift_database },
    { name = "REDSHIFT_CLUSTER_ID", value = coalesce(var.redshift_cluster_id, "") },
    { name = "REDSHIFT_SERVERLESS_WORKGROUP", value = coalesce(var.redshift_serverless_workgroup, "") },
    { name = "REDSHIFT_SECRET_ARN", value = var.redshift_data_secret_arn },
    { name = "REDSHIFT_UNLOAD_ROLE_ARN", value = var.redshift_unload_role_arn },
  ]
}

resource "aws_s3_bucket" "work" {
  bucket = local.work_bucket_name
  tags   = local.common_tags
}

resource "aws_s3_bucket" "retained" {
  bucket = local.retained_bucket_name
  tags   = local.common_tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = {
    work     = aws_s3_bucket.work.id
    retained = aws_s3_bucket.retained.id
  }
  bucket = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each = {
    work     = aws_s3_bucket.work.id
    retained = aws_s3_bucket.retained.id
  }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "retained" {
  bucket = aws_s3_bucket.retained.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "work" {
  bucket = aws_s3_bucket.work.id
  rule {
    id     = "expire-transient-artifacts"
    status = "Enabled"
    filter {}
    expiration { days = 14 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_dynamodb_table" "datasources" {
  name         = "${var.name_prefix}-datasources"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tableau_datasource_id"
  attribute {
    name = "tableau_datasource_id"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "datasource_tables" {
  name         = "${var.name_prefix}-datasource-tables"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tableau_datasource_id"
  range_key    = "table_id"
  attribute {
    name = "tableau_datasource_id"
    type = "S"
  }
  attribute {
    name = "table_id"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  tags = local.common_tags
}

resource "aws_dynamodb_table" "tables" {
  name         = "${var.name_prefix}-tables"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "table_id"
  attribute {
    name = "table_id"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "batch" {
  name              = "/aws/batch/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "discover" {
  name              = "/aws/lambda/${var.name_prefix}-discover"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "plan" {
  name              = "/aws/lambda/${var.name_prefix}-plan"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}
