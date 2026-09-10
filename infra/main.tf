terraform {
  required_version = ">= 1.8.0"

  # Partial backend configuration. The state bucket name embeds the AWS account
  # ID, so it is supplied at init time instead of being committed:
  #
  #   tofu init \
  #     -backend-config="bucket=$TF_STATE_BUCKET" \
  #     -backend-config="dynamodb_table=$TF_LOCK_TABLE"
  #
  # A backend block cannot reference variables — that is an OpenTofu/Terraform
  # limitation, not a style choice — so -backend-config is the only way to keep
  # these values out of the tree. See README.md, "Repository configuration".
  backend "s3" {
    key     = "litter-robot-diagnostics/watchdog.tfstate"
    region  = "us-west-2"
    encrypt = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # These five are already on every resource in the live stack, applied out of
  # band and never declared here. Without this block a plan reads them as drift
  # and strips them -- which is what the first real `tofu plan` against this
  # configuration turned out to be proposing, on eleven resources at once.
  # Declaring them makes the configuration match what is deployed, so an apply
  # changes only what it means to change.
  default_tags {
    tags = {
      Application = "litter-robot-diagnostics"
      Environment = "personal"
      Lifecycle   = "active"
      ManagedBy   = "opentofu"
      Owner       = "akaiserauer"
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# GitHub issues the `sub` claim in two shapes, and this role has to accept both.
#
# The original is `repo:OWNER/REPO:ref:refs/heads/BRANCH`. GitHub now also
# issues an immutable-id form that appends the numeric owner and repository ids
# to each name -- `repo:kornsour@12611126/litter-robot-diagnostics@1346772541:...`
# -- so that a rename cannot silently transfer trust to whoever claims the freed
# name. Pinning only the first shape is why every CI deploy since 2026-08-28
# failed `AssumeRoleWithWebIdentity`: the role and the provider were both
# correct, and the claim simply no longer matched the string.
#
# Only the ids are wildcarded. The owner, the repository and the branch all stay
# exact, so this still trusts one branch of one repository -- `@*` can only
# stand in for the digits that identify the very same owner and repo.
locals {
  github_owner      = split("/", var.github_repository)[0]
  github_repo_name  = split("/", var.github_repository)[1]
  github_deploy_ref = "ref:refs/heads/${var.github_default_branch}"
  github_deploy_subs = [
    "repo:${var.github_repository}:${local.github_deploy_ref}",
    "repo:${local.github_owner}@*/${local.github_repo_name}@*:${local.github_deploy_ref}",
  ]
}

resource "aws_iam_role" "github_deploy" {
  name = "${var.name_prefix}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # A list is OR-ed, so either shape is accepted. `StringLike` is required
        # for the wildcard; the audience above stays an exact `StringEquals`.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = local.github_deploy_subs
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${var.name_prefix}-github-deploy"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.tf_state_bucket}/litter-robot-diagnostics/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.tf_state_bucket}"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:DeleteItem", "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${var.name_prefix}-watchdog-tofu-lock"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetOpenIDConnectProvider"]
        Resource = aws_iam_openid_connect_provider.github.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:ListTagsOfResource", "dynamodb:TagResource", "dynamodb:UntagResource", "dynamodb:UpdateTable",
          "events:DeleteRule", "events:DescribeRule", "events:ListTagsForResource", "events:ListTargetsByRule", "events:PutRule", "events:PutTargets", "events:RemoveTargets",
          "lambda:AddPermission", "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConfiguration", "lambda:GetFunctionRecursionConfig", "lambda:GetPolicy", "lambda:GetRuntimeManagementConfig", "lambda:ListTags", "lambda:ListVersionsByFunction", "lambda:RemovePermission", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:DescribeLogGroups", "logs:DeleteMetricFilter", "logs:DescribeMetricFilters", "logs:ListTagsForResource", "logs:PutMetricFilter", "logs:PutRetentionPolicy", "logs:TagResource", "logs:UntagResource",
          "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "cloudwatch:PutMetricAlarm", "cloudwatch:TagResource", "cloudwatch:UntagResource",
          "sns:CreateTopic", "sns:DeleteTopic", "sns:GetSubscriptionAttributes", "sns:GetTopicAttributes", "sns:ListSubscriptionsByTopic", "sns:ListTagsForResource", "sns:SetTopicAttributes", "sns:Subscribe", "sns:TagResource", "sns:UntagResource", "sns:Unsubscribe",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:PutRolePolicy", "iam:PassRole"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-*"
      }
    ]
  })
}

data "archive_file" "watchdog" {
  type        = "zip"
  source_dir  = "${path.module}/../build/lambda"
  output_path = "${path.module}/../build/lr4-watchdog.zip"
}

resource "aws_dynamodb_table" "watchdog" {
  name         = "${var.name_prefix}-watchdog"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "robot_id"
  range_key    = "recorded_at"

  attribute {
    name = "robot_id"
    type = "S"
  }
  attribute {
    name = "recorded_at"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/${var.name_prefix}-watchdog"
  retention_in_days = 30
}

resource "aws_iam_role" "watchdog" {
  name = "${var.name_prefix}-watchdog"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "watchdog" {
  name = "${var.name_prefix}-watchdog"
  role = aws_iam_role.watchdog.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:DeleteItem", "dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.watchdog.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:UpdateSecret"]
        Resource = var.whisker_secret_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.watchdog.arn}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "watchdog" {
  function_name    = "${var.name_prefix}-watchdog"
  role             = aws_iam_role.watchdog.arn
  filename         = data.archive_file.watchdog.output_path
  source_code_hash = data.archive_file.watchdog.output_base64sha256
  handler          = "lr4_diagnostics.lambda_handler.handler"
  runtime          = "python3.14"
  architectures    = ["arm64"]
  timeout          = 600
  memory_size      = 512

  environment {
    variables = {
      WATCHDOG_STATE_TABLE       = aws_dynamodb_table.watchdog.name
      WHISKER_SECRET_ARN         = var.whisker_secret_arn
      WATCHDOG_ARMED             = tostring(var.armed)
      WATCHDOG_REZERO_ARMED      = tostring(var.rezero_armed)
      DRAWER_WARN_PERCENT        = tostring(var.drawer_warn_percent)
      DRAWER_CLEAR_PERCENT       = tostring(var.drawer_clear_percent)
      DRAWER_CONSECUTIVE_SAMPLES = tostring(var.drawer_consecutive_samples)
      FAULT_CONSECUTIVE_SAMPLES  = tostring(var.fault_consecutive_samples)
    }
  }

  depends_on = [aws_cloudwatch_log_group.watchdog]
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-watchdog-alerts"

  # The deploy role grants itself SNS, alarm and metric-filter permissions in
  # this same configuration, so the policy update has to land before anything
  # that needs it. Without this Terraform is free to try the topic first and
  # fail with AccessDenied on a clean apply.
  depends_on = [aws_iam_role_policy.github_deploy]
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# A stuck unit is the thing the owner actually needs to know about: the globe is
# parked away from home and the cats are going elsewhere in the house. This
# fires whether or not the watchdog is armed, so detection-only still shortens
# the time to discovery from hours to minutes.
resource "aws_cloudwatch_log_metric_filter" "stuck" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-stuck"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_STUCK"

  metric_transformation {
    name          = "WatchdogStuckDetections"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "stuck" {
  alarm_name        = "${var.name_prefix}-watchdog-stuck"
  alarm_description = "The LR4 is stuck; the watchdog has assessed it as needing recovery."
  namespace         = "LR4Watchdog"
  metric_name       = aws_cloudwatch_log_metric_filter.stuck.metric_transformation[0].name
  statistic         = "Sum"
  period            = 300
  # A marker alarm is held in ALARM by the marker repeating once a minute, so
  # any gap in invocations reads as "condition cleared". Recovery is exactly
  # such a gap: it holds the DynamoDB lease while it drives the globe and
  # watches it park, so every invocation behind it skips and emits nothing.
  # Measured 2026-09-07: a 5m17s recovery blanked minutes 11:57-12:01, which
  # dropped this to OK and re-raised it the next minute -- a second mail for a
  # condition that never changed. Requiring one breaching datapoint in three
  # periods rides out a lease held for the full Lambda timeout. The cost is
  # that a genuine clear takes 15 minutes to show, which nothing acts on:
  # there are no `ok_actions` here.
  evaluation_periods  = 3
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Escalation is a one-way latch: the watchdog will not act again until the STATE
# item is cleared by hand, so an unnoticed escalation is an unattended unit.
resource "aws_cloudwatch_log_metric_filter" "escalated" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-escalated"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_ESCALATED"

  metric_transformation {
    name          = "WatchdogEscalations"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "escalated" {
  alarm_name          = "${var.name_prefix}-watchdog-escalated"
  alarm_description   = "Recovery failed repeatedly; the watchdog has stood down and needs a human."
  namespace           = "LR4Watchdog"
  metric_name         = aws_cloudwatch_log_metric_filter.escalated.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The handler fails closed, so a sustained error means the unit is unmonitored
# rather than unsafe. Two periods keeps a single expired-token blip quiet.
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name_prefix}-watchdog-errors"
  alarm_description   = "The scheduled watchdog check is erroring; the unit is not being monitored."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.watchdog.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Whisker only announces a full drawer through an app push -- the notification
# settings offer no email and there is no webhook to subscribe to. The handler
# emits the marker line once the level has held above the threshold, and repeats
# it until the drawer is emptied, so this stays in ALARM for the whole fill and
# sends exactly one mail per fill.
resource "aws_cloudwatch_log_metric_filter" "drawer_full" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-drawer-full"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_DRAWER_FULL"

  metric_transformation {
    name          = "WatchdogDrawerFull"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "drawer_full" {
  alarm_name        = "${var.name_prefix}-watchdog-drawer-full"
  alarm_description = "The LR4 waste drawer is at or above ${var.drawer_warn_percent}% and needs emptying."
  namespace         = "LR4Watchdog"
  metric_name       = aws_cloudwatch_log_metric_filter.drawer_full.metric_transformation[0].name
  statistic         = "Sum"
  period            = 300
  # A marker alarm is held in ALARM by the marker repeating once a minute, so
  # any gap in invocations reads as "condition cleared". Recovery is exactly
  # such a gap: it holds the DynamoDB lease while it drives the globe and
  # watches it park, so every invocation behind it skips and emits nothing.
  # Measured 2026-09-07: a 5m17s recovery blanked minutes 11:57-12:01, which
  # dropped this to OK and re-raised it the next minute -- a second mail for a
  # condition that never changed. Requiring one breaching datapoint in three
  # periods rides out a lease held for the full Lambda timeout. The cost is
  # that a genuine clear takes 15 minutes to show, which nothing acts on:
  # there are no `ok_actions` here.
  evaluation_periods  = 3
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  # Deliberately no `ok_actions`: alarm actions fire on state transitions only,
  # so this is exactly one mail per fill however long the drawer stays full, and
  # notifying on the clear would double that for a fact the owner already knows
  # -- they are the one who emptied it.
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# The unit latches hardware fault flags and keeps reporting them until the
# firmware clears them, but it returns to an idle display in the meantime -- so
# neither the app nor `assess` shows anything wrong. The first
# globeMotorFaultStatus=FAULT_TIMEOUT on this unit (2026-08-14 14:44Z) sat
# latched for over four hours behind a normal blue light and a healthy
# assessment. This is the only alarm that can see that.
resource "aws_cloudwatch_log_metric_filter" "motor_fault" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-motor-fault"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_MOTOR_FAULT"

  metric_transformation {
    name          = "WatchdogMotorFault"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "motor_fault" {
  alarm_name        = "${var.name_prefix}-watchdog-motor-fault"
  alarm_description = "The LR4 is reporting a latched hardware fault flag."
  namespace         = "LR4Watchdog"
  metric_name       = aws_cloudwatch_log_metric_filter.motor_fault.metric_transformation[0].name
  statistic         = "Sum"
  period            = 300
  # A marker alarm is held in ALARM by the marker repeating once a minute, so
  # any gap in invocations reads as "condition cleared". Recovery is exactly
  # such a gap: it holds the DynamoDB lease while it drives the globe and
  # watches it park, so every invocation behind it skips and emits nothing.
  # Measured 2026-09-07: a 5m17s recovery blanked minutes 11:57-12:01, which
  # dropped this to OK and re-raised it the next minute -- a second mail for a
  # condition that never changed. Requiring one breaching datapoint in three
  # periods rides out a lease held for the full Lambda timeout. The cost is
  # that a genuine clear takes 15 minutes to show, which nothing acts on:
  # there are no `ok_actions` here.
  evaluation_periods  = 3
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  # Same shape as the drawer alarm and for the same reason: the repeating marker
  # holds this in ALARM for the whole episode, so it is one mail per fault rather
  # than one per check. `ok_actions` is omitted deliberately -- the flag clears at
  # the *start* of the next cycle (measured 2026-08-14), so an OK notification
  # would announce a recovery that has not been demonstrated.
  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_event_rule" "watchdog" {
  name                = "${var.name_prefix}-watchdog-every-minute"
  description         = "Run one fail-closed LR4 watchdog check every minute."
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "watchdog" {
  rule = aws_cloudwatch_event_rule.watchdog.name
  arn  = aws_lambda_function.watchdog.arn
  retry_policy {
    maximum_event_age_in_seconds = 60
    maximum_retry_attempts       = 0
  }
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeScheduledInvocation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.watchdog.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watchdog.arn
}

# `WATCHDOG_ESCALATED` cannot cover this one. CloudWatch text patterns match
# whole tokens, so that filter never sees `WATCHDOG_REZERO_ESCALATED` -- the
# re-zero could stand down permanently without anything saying so. Escalation
# is a one-way latch cleared only by hand at the unit, so a silent one is a
# mitigation that has quietly stopped mitigating.
resource "aws_cloudwatch_log_metric_filter" "rezero_escalated" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-rezero-escalated"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_REZERO_ESCALATED"

  metric_transformation {
    name          = "WatchdogRezeroEscalations"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "rezero_escalated" {
  alarm_name          = "${var.name_prefix}-watchdog-rezero-escalated"
  alarm_description   = "Proactive scale re-zero failed repeatedly; the watchdog has stood down and needs a human."
  namespace           = "LR4Watchdog"
  metric_name         = aws_cloudwatch_log_metric_filter.rezero_escalated.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The scale-drift signal the whole investigation turns on (docs/hypothesis.md):
# a weekly maxWeight past what the household's cats can physically produce.
# Worth its own alarm rather than folding into the stuck alarm, because it
# fires while the unit still looks healthy -- that early warning is the point.
#
# The period is an hour, not five minutes, because the handler reads the weekly
# summary on `rezero_poll_interval` rather than every invocation; a 300s window
# would be empty for 11 of every 12 periods and flap once an hour. Two periods
# then means a drift episode is one mail, and clears two hours after the weekly
# number comes back under the ceiling.
resource "aws_cloudwatch_log_metric_filter" "scale_drift" {
  depends_on     = [aws_iam_role_policy.github_deploy]
  name           = "${var.name_prefix}-watchdog-scale-drift"
  log_group_name = aws_cloudwatch_log_group.watchdog.name
  pattern        = "WATCHDOG_SCALE_DRIFT"

  metric_transformation {
    name          = "WatchdogScaleDrift"
    namespace     = "LR4Watchdog"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "scale_drift" {
  alarm_name          = "${var.name_prefix}-watchdog-scale-drift"
  alarm_description   = "Weekly maxWeight is past the physical ceiling; the base scale is drifting again."
  namespace           = "LR4Watchdog"
  metric_name         = aws_cloudwatch_log_metric_filter.scale_drift.metric_transformation[0].name
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 2
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
