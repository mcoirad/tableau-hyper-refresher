variable "aws_region" {
  description = "AWS region in which to create ECR repositories."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for project resources."
  type        = string
  default     = "tableau-hyper-refresher"
}

