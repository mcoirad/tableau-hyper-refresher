variable "aws_region" {
  type        = string
  description = "AWS region for all runtime resources."
  default     = "us-east-1"
}

variable "name_prefix" {
  type        = string
  description = "Lowercase prefix used for all resource names."
  default     = "tableau-hyper-refresher"
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.name_prefix))
    error_message = "name_prefix may contain lowercase letters, digits, and hyphens only."
  }
}

variable "image_tag" {
  type        = string
  description = "Immutable tag pushed by `make images`."
  default     = "dev"
}

variable "subnet_ids" {
  type        = list(string)
  description = "Existing subnets with Redshift connectivity and outbound access to Tableau."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Existing security groups for AWS Batch tasks."
}

variable "assign_public_ip" {
  type        = bool
  description = "Assign public IPs to Fargate jobs. Prefer private subnets with NAT."
  default     = false
}

variable "max_vcpus" {
  type        = number
  description = "Maximum vCPUs for the managed Batch environment."
  default     = 32
}

variable "inspect_vcpu" {
  type        = number
  description = "vCPU allocation for inspect jobs."
  default     = 1
}

variable "inspect_memory_mib" {
  type        = number
  description = "Memory allocation for inspect jobs, in MiB."
  default     = 2048
}

variable "unload_vcpu" {
  type        = number
  description = "vCPU allocation for unload jobs."
  default     = 2
}

variable "unload_memory_mib" {
  type        = number
  description = "Memory allocation for unload jobs, in MiB."
  default     = 4096
}

variable "refresh_vcpu" {
  type        = number
  description = "vCPU allocation for refresh jobs."
  default     = 4
}

variable "refresh_memory_mib" {
  type        = number
  description = "Memory allocation for refresh jobs, in MiB."
  default     = 16384
}

variable "refresh_ephemeral_storage_gib" {
  type        = number
  description = "Ephemeral storage allocation for refresh jobs, in GiB."
  default     = 40
}

variable "discover_function_name" {
  type        = string
  description = "Optional explicit datasource-discovery Lambda name."
  default     = null
  nullable    = true
}

variable "plan_function_name" {
  type        = string
  description = "Optional explicit unload-planning Lambda name."
  default     = null
  nullable    = true
}

variable "inspect_job_name" {
  type        = string
  description = "AWS Batch job name used for inspect submissions."
  default     = "tableau-hyper-inspect"
}

variable "unload_job_name" {
  type        = string
  description = "AWS Batch job name used for unload submissions."
  default     = "tableau-hyper-unload"
}

variable "refresh_job_name" {
  type        = string
  description = "AWS Batch job name used for refresh submissions."
  default     = "tableau-hyper-refresh"
}

variable "tableau_server_url" {
  type        = string
  description = "Base URL for Tableau Cloud or Tableau Server."
}

variable "tableau_site_id" {
  type        = string
  description = "Tableau site content URL."
}

variable "tableau_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing connected-app JWT credentials."
}

variable "tableau_redshift_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing username/password embedded during publication."
}

variable "tableau_refresh_tag" {
  type        = string
  description = "Datasource tag selecting Hyper refresh candidates."
  default     = "use_hyper_refresh"
}

variable "tableau_production_tag" {
  type        = string
  description = "Datasource tag used in failure summaries."
  default     = "production"
}

variable "redshift_database" {
  type        = string
  description = "Redshift database name."
}

variable "redshift_cluster_id" {
  type        = string
  description = "Provisioned Redshift cluster ID; omit when using Serverless."
  default     = null
  nullable    = true
}

variable "redshift_serverless_workgroup" {
  type        = string
  description = "Redshift Serverless workgroup; omit when using a provisioned cluster."
  default     = null
  nullable    = true
}

variable "redshift_data_secret_arn" {
  type        = string
  description = "Secrets Manager ARN used by the Redshift Data API."
}

variable "redshift_unload_role_arn" {
  type        = string
  description = "IAM role Redshift assumes to write UNLOAD data to the work bucket."
}

variable "refresh_config_path" {
  type        = string
  description = "Path to refresh strategy YAML inside the Batch image."
  default     = "/app/config/refresh-config.yaml"
}

variable "log_retention_days" {
  type        = number
  description = "CloudWatch log retention."
  default     = 30
}

variable "tags" {
  type        = map(string)
  description = "Additional AWS tags."
  default     = {}
}
