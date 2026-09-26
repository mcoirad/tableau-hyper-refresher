data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_execution" {
  name               = "${var.name_prefix}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "batch_execution" {
  role       = aws_iam_role.batch_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "batch_job" {
  name               = "${var.name_prefix}-batch-job"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "batch_job" {
  statement {
    sid = "Artifacts"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.work.arn,
      "${aws_s3_bucket.work.arn}/*",
      aws_s3_bucket.retained.arn,
      "${aws_s3_bucket.retained.arn}/*",
    ]
  }

  statement {
    sid = "WorkflowState"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
    ]
    resources = [
      aws_dynamodb_table.datasources.arn,
      aws_dynamodb_table.datasource_tables.arn,
      aws_dynamodb_table.tables.arn,
    ]
  }

  statement {
    sid       = "RuntimeSecrets"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [var.tableau_secret_arn, var.tableau_redshift_secret_arn, var.redshift_data_secret_arn]
  }

  statement {
    sid = "RedshiftDataApi"
    actions = [
      "redshift-data:CancelStatement",
      "redshift-data:DescribeStatement",
      "redshift-data:ExecuteStatement",
      "redshift-data:GetStatementResult",
    ]
    resources = ["*"]
  }

  statement {
    sid = "RedshiftCredentials"
    actions = [
      "redshift:GetClusterCredentials",
      "redshift-serverless:GetCredentials",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "batch_job" {
  name   = "runtime"
  role   = aws_iam_role.batch_job.id
  policy = data.aws_iam_policy_document.batch_job.json
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid = "Logging"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.discover.arn}:*",
      "${aws_cloudwatch_log_group.plan.arn}:*",
    ]
  }

  statement {
    sid = "WorkflowState"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Scan",
    ]
    resources = [
      aws_dynamodb_table.datasources.arn,
      aws_dynamodb_table.datasource_tables.arn,
      aws_dynamodb_table.tables.arn,
    ]
  }

  statement {
    sid       = "TableauSecret"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [var.tableau_secret_arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "runtime"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

data "aws_iam_policy_document" "step_functions_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "step_functions" {
  name               = "${var.name_prefix}-step-functions"
  assume_role_policy = data.aws_iam_policy_document.step_functions_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "step_functions" {
  statement {
    sid       = "InvokeHelpers"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.discover.arn, aws_lambda_function.plan.arn]
  }

  statement {
    sid = "RunBatchJobs"
    actions = [
      "batch:DescribeJobs",
      "batch:SubmitJob",
      "batch:TerminateJob",
    ]
    resources = ["*"]
  }

  statement {
    sid = "BatchEventRule"
    actions = [
      "events:DescribeRule",
      "events:PutRule",
      "events:PutTargets",
    ]
    resources = ["arn:${data.aws_partition.current.partition}:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForBatchJobsRule"]
  }

  statement {
    sid = "StateMachineLogging"
    actions = [
      "logs:CreateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:DescribeLogGroups",
      "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:UpdateLogDelivery",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "step_functions" {
  name   = "runtime"
  role   = aws_iam_role.step_functions.id
  policy = data.aws_iam_policy_document.step_functions.json
}
