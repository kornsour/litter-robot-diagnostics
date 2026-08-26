variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "name_prefix" {
  type    = string
  default = "lr4"
}

variable "whisker_secret_arn" {
  type        = string
  sensitive   = true
  description = "ARN of an existing Secrets Manager JSON secret containing username and password."
}

variable "armed" {
  type        = bool
  default     = false
  description = "Explicitly permit device-control commands. Detection-only is the safe default."
}

variable "alarm_email" {
  type        = string
  default     = ""
  description = <<-EOT
    Address to notify when the unit is stuck, when the watchdog stands down, when
    the waste drawer needs emptying, when the unit reports a latched hardware
    fault, or when the scheduled check is failing.
    Leave empty to create the topic without a subscriber. AWS sends a
    confirmation mail that must be accepted before any notification is delivered.
  EOT
}

variable "drawer_warn_percent" {
  type        = number
  default     = 85
  description = "DFILevelPercent at or above which the waste drawer needs emptying."
}

variable "drawer_clear_percent" {
  type        = number
  default     = 60
  description = <<-EOT
    Level the drawer must fall back to before the warning is released. The gap
    below drawer_warn_percent is hysteresis, not slack: the DFI ToF swings
    several percent at rest, and a narrow band would flap the alarm and re-send
    the mail on every swing.
  EOT
}

variable "drawer_consecutive_samples" {
  type        = number
  default     = 5
  description = <<-EOT
    Consecutive at-rest readings above the threshold before warning. The check
    runs once a minute, so this is the number of minutes a single noisy sample
    must sustain itself before it can send mail.
  EOT
}

variable "fault_consecutive_samples" {
  type        = number
  default     = 3
  description = <<-EOT
    Consecutive readings of the same latched fault flag before it is reported.
    Deliberately lower than drawer_consecutive_samples: a fault status is a
    categorical flag rather than a noisy ToF distance, so persistence buys
    nothing beyond riding over a single garbled or partial payload.
  EOT
}

variable "tf_state_bucket" {
  type        = string
  description = <<-EOT
    Name of the S3 bucket holding this module's OpenTofu state. Deliberately has
    no default: the name embeds the AWS account ID, and committing it here would
    put the account ID back in the tree that the -backend-config indirection
    exists to keep it out of. Supply it via TF_VAR_tf_state_bucket, and pass the
    same value to `tofu init -backend-config="bucket=..."`.
  EOT
}

variable "github_repository" {
  type        = string
  description = <<-EOT
    OWNER/REPO allowed to assume the deploy role via GitHub OIDC. This is the
    `sub` claim GitHub mints, so it must match the repository exactly — moving
    the code to a different owner or name without updating this leaves the role
    unassumable. In CI, pass the github.repository context value rather than
    hardcoding it.
  EOT
}

variable "github_default_branch" {
  type        = string
  default     = "main"
  description = "Branch whose workflow runs may assume the deploy role."
}
