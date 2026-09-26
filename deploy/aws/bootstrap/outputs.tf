output "batch_repository_url" {
  description = "ECR URL for the Batch worker image."
  value       = aws_ecr_repository.batch.repository_url
}

output "lambda_repository_url" {
  description = "ECR URL for the shared Lambda image."
  value       = aws_ecr_repository.lambda.repository_url
}

