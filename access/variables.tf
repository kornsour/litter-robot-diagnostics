variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "name_prefix" {
  type        = string
  default     = "lr4"
  description = "Must match the prefix used by the infra/ module, or the ARNs below point at nothing."
}

variable "owner_user_name" {
  type        = string
  description = <<-EOT
    Identity Center UserName to assign the read-only permission set to.
    Deliberately has no default. A real login name sitting next to the account
    ID and the access-portal URL is most of what an SSO consent-phishing attempt
    needs, so it is supplied at apply time (terraform.tfvars, which is
    gitignored, or -var) rather than committed.
  EOT
}

variable "session_duration" {
  type        = string
  default     = "PT4H"
  description = <<-EOT
    ISO-8601 session length. Four hours is long enough to work an investigation
    without re-authenticating mid-thread, and the permission set can neither
    write nor read a secret, so a stale session is low-consequence. Shorten to
    PT1H if you would rather it expire sooner.
  EOT
}
