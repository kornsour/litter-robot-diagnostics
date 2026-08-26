variable "state_bucket" {
  type        = string
  description = <<-EOT
    Name of the S3 bucket to create for OpenTofu state. No default: the
    conventional name embeds the AWS account ID, which is kept out of the tree.
    S3 bucket names are globally unique, so this has to be account-specific in
    practice — `<prefix>-watchdog-tofu-state-<account-id>` is the shape used.
  EOT
}

variable "lock_table" {
  type        = string
  default     = "lr4-watchdog-tofu-lock"
  description = "DynamoDB table for state locking. Name carries no account ID, so it defaults."
}
