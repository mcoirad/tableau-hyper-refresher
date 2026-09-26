output "state_machine_arn" {
  description = "Unqualified state machine ARN."
  value       = aws_sfn_state_machine.this.arn
}

output "state_machine_alias_arn" {
  description = "Stable state machine alias ARN used to start refreshes."
  value       = aws_sfn_alias.stable.arn
}

output "work_bucket" {
  value = aws_s3_bucket.work.id
}

output "retained_bucket" {
  value = aws_s3_bucket.retained.id
}

