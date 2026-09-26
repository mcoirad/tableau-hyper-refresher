resource "aws_batch_compute_environment" "this" {
  name  = var.name_prefix
  type  = "MANAGED"
  state = "ENABLED"

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.max_vcpus
    security_group_ids = var.security_group_ids
    subnets            = var.subnet_ids
  }

  tags = local.common_tags
}

resource "aws_batch_job_queue" "this" {
  name     = var.name_prefix
  state    = "ENABLED"
  priority = 10

  compute_environment_order {
    compute_environment = aws_batch_compute_environment.this.arn
    order               = 1
  }

  tags = local.common_tags
}

locals {
  job_definitions = {
    inspect = {
      vcpu              = tostring(var.inspect_vcpu)
      memory            = tostring(var.inspect_memory_mib)
      ephemeral_storage = null
    }
    unload = {
      vcpu              = tostring(var.unload_vcpu)
      memory            = tostring(var.unload_memory_mib)
      ephemeral_storage = null
    }
    refresh = {
      vcpu              = tostring(var.refresh_vcpu)
      memory            = tostring(var.refresh_memory_mib)
      ephemeral_storage = var.refresh_ephemeral_storage_gib
    }
  }
}

resource "aws_batch_job_definition" "jobs" {
  for_each = local.job_definitions

  name                  = "${var.name_prefix}-${each.key}"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true

  container_properties = jsonencode(merge({
    image            = local.batch_image_uri
    executionRoleArn = aws_iam_role.batch_execution.arn
    jobRoleArn       = aws_iam_role.batch_job.arn
    environment      = local.runtime_environment
    networkConfiguration = {
      assignPublicIp = var.assign_public_ip ? "ENABLED" : "DISABLED"
    }
    resourceRequirements = [
      { type = "VCPU", value = each.value.vcpu },
      { type = "MEMORY", value = each.value.memory },
    ]
    runtimePlatform = {
      cpuArchitecture       = "X86_64"
      operatingSystemFamily = "LINUX"
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.batch.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = each.key
      }
    }
    }, each.value.ephemeral_storage == null ? {} : {
    ephemeralStorage = { sizeInGiB = each.value.ephemeral_storage }
  }))

  tags = local.common_tags
}

locals {
  lambda_environment = {
    TABLEAU_REGION_NAME             = var.aws_region
    TABLEAU_SERVER_URL              = var.tableau_server_url
    TABLEAU_SITE_ID                 = var.tableau_site_id
    TABLEAU_SECRET_NAME             = var.tableau_secret_arn
    TABLEAU_REFRESH_TAG             = var.tableau_refresh_tag
    TABLEAU_PRODUCTION_TAG          = var.tableau_production_tag
    TABLEAU_DATASOURCES_TABLE       = aws_dynamodb_table.datasources.name
    TABLEAU_DATASOURCE_TABLES_TABLE = aws_dynamodb_table.datasource_tables.name
    TABLEAU_TABLES_TABLE            = aws_dynamodb_table.tables.name
  }
}

resource "aws_lambda_function" "discover" {
  function_name = local.discover_function_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.lambda_image_uri
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 300

  image_config {
    command = ["tableau_hyper_refresher.lambda_handlers.discover_datasources"]
  }

  environment { variables = local.lambda_environment }
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.discover.name
  }
  tags = local.common_tags
}

resource "aws_lambda_function" "plan" {
  function_name = local.plan_function_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.lambda_image_uri
  architectures = ["x86_64"]
  memory_size   = 256
  timeout       = 300

  image_config {
    command = ["tableau_hyper_refresher.lambda_handlers.create_unload_plan"]
  }

  environment { variables = local.lambda_environment }
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.plan.name
  }
  tags = local.common_tags
}

resource "aws_sfn_state_machine" "this" {
  name     = var.name_prefix
  role_arn = aws_iam_role.step_functions.arn
  type     = "STANDARD"
  publish  = true

  definition = templatefile("${path.module}/state_machine.asl.json", {
    discover_lambda_arn        = aws_lambda_function.discover.arn
    plan_lambda_arn            = aws_lambda_function.plan.arn
    job_queue_arn              = aws_batch_job_queue.this.arn
    inspect_job_definition_arn = aws_batch_job_definition.jobs["inspect"].arn
    unload_job_definition_arn  = aws_batch_job_definition.jobs["unload"].arn
    refresh_job_definition_arn = aws_batch_job_definition.jobs["refresh"].arn
    inspect_job_name           = var.inspect_job_name
    unload_job_name            = var.unload_job_name
    refresh_job_name           = var.refresh_job_name
    work_bucket                = aws_s3_bucket.work.id
    refresh_config_path        = var.refresh_config_path
  })

  logging_configuration {
    include_execution_data = false
    level                  = "ERROR"
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
  }

  tracing_configuration { enabled = true }
  tags = local.common_tags
}

resource "aws_sfn_alias" "stable" {
  name = "stable"
  routing_configuration {
    state_machine_version_arn = aws_sfn_state_machine.this.state_machine_version_arn
    weight                    = 100
  }
}
