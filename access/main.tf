terraform {
  required_version = ">= 1.8.0"

  # Deliberately a *separate* state from `infra/`. See README.md: CI applies
  # `infra/` on every merge, and the deploy role must never hold the
  # sso-admin/identitystore permissions these resources need.
  # Partial backend configuration — the bucket name embeds the AWS account ID.
  # Supply it at init time; see ../README.md, "Repository configuration".
  #
  #   tofu init \
  #     -backend-config="bucket=$TF_STATE_BUCKET" \
  #     -backend-config="dynamodb_table=$TF_LOCK_TABLE"
  backend "s3" {
    key     = "litter-robot-diagnostics/access.tfstate"
    region  = "us-west-2"
    encrypt = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_ssoadmin_instances" "this" {}

locals {
  instance_arn      = tolist(data.aws_ssoadmin_instances.this.arns)[0]
  identity_store_id = tolist(data.aws_ssoadmin_instances.this.identity_store_ids)[0]
  account_id        = data.aws_caller_identity.current.account_id

  log_group_arn = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-watchdog"
  table_arn     = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.name_prefix}-watchdog"
  function_arn  = "arn:aws:lambda:${var.aws_region}:${local.account_id}:function:${var.name_prefix}-watchdog"
  topic_arn     = "arn:aws:sns:${var.aws_region}:${local.account_id}:${var.name_prefix}-watchdog-alerts"
  alarm_arn     = "arn:aws:cloudwatch:${var.aws_region}:${local.account_id}:alarm:${var.name_prefix}-watchdog-*"
}

data "aws_identitystore_user" "owner" {
  identity_store_id = local.identity_store_id

  alternate_identifier {
    unique_attribute {
      attribute_path  = "UserName"
      attribute_value = var.owner_user_name
    }
  }
}

resource "aws_ssoadmin_permission_set" "diagnostics_readonly" {
  name             = "LR4Diagnostics-ReadOnly"
  description      = "Read the LR4 watchdog's operational record. No writes, no device control, no secrets."
  instance_arn     = local.instance_arn
  session_duration = var.session_duration

  tags = {
    Application = "litter-robot-diagnostics"
    ManagedBy   = "opentofu"
  }
}

resource "aws_ssoadmin_permission_set_inline_policy" "diagnostics_readonly" {
  instance_arn       = local.instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.diagnostics_readonly.arn

  inline_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # The watchdog's own log group, and nothing else in the account.
      {
        Sid    = "ReadWatchdogLogs"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogStreams",
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
          "logs:StartQuery",
          "logs:GetQueryResults",
        ]
        Resource = [
          local.log_group_arn,
          "${local.log_group_arn}:*",
        ]
      },
      # `DescribeLogGroups` is a list operation: IAM evaluates it against an
      # empty log-group ARN (`log-group::log-stream:`), so a scoped Resource
      # never matches and the call is denied outright. Verified 2026-08-03.
      # It therefore has to be `*` or be dropped. Kept because `aws logs tail`
      # resolves the group through it, and it discloses log group *names*
      # only -- no streams, no events, no payloads.
      {
        Sid      = "DiscoverLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      # Alarm state and history: did the stuck/escalated alarm ever fire, and
      # when did it transition.
      {
        Sid    = "ReadWatchdogAlarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:DescribeAlarmHistory",
        ]
        Resource = local.alarm_arn
      },
      # Metric reads carry no resource ARN in IAM *and* do not honour the
      # `cloudwatch:namespace` condition key -- a first attempt scoped these to
      # the LR4Watchdog and AWS/Lambda namespaces and every call was denied
      # with "no identity-based policy allows", because the conditioned
      # statement simply never matched. Verified 2026-08-03.
      #
      # So this is genuinely account-wide across metric *metadata*: names,
      # dimensions, and datapoint values for any namespace. It buys the
      # Invocations/Errors check that proves the once-a-minute schedule is
      # actually running, which is worth more than the scope it gives up.
      # No log content and no payloads are reachable through it.
      {
        Sid    = "ReadWatchdogMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
        ]
        Resource = "*"
      },
      # The durable watchdog state, the intervention records, and the redacted
      # diagnostic capture all live in this one table. Read verbs only.
      {
        Sid    = "ReadWatchdogState"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable",
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = local.table_arn
      },
      # How the armed flag reads *in production*, which is the only
      # authoritative answer -- the tfvars file in the tree contradicts it.
      # Returns env vars, which hold a secret ARN but no secret value.
      {
        Sid      = "ReadWatchdogFunctionConfig"
        Effect   = "Allow"
        Action   = ["lambda:GetFunctionConfiguration"]
        Resource = local.function_arn
      },
      # Confirm the alert email is actually subscribed and confirmed. An
      # unconfirmed subscription means every alarm fires into nothing.
      {
        Sid    = "ReadAlertSubscriptions"
        Effect = "Allow"
        Action = [
          "sns:GetTopicAttributes",
          "sns:ListSubscriptionsByTopic",
        ]
        Resource = local.topic_arn
      },
      # Defence in depth. Nothing above grants any of this, but these are the
      # two things that must never be reachable from a diagnostic session:
      # the Whisker credentials, and the ability to arm or disarm a machine
      # that physically moves with a cat nearby.
      #
      # Prefixes rather than named actions, deliberately. A first version
      # listed PutItem/UpdateItem/DeleteItem and PutRule/EnableRule/
      # DisableRule, which left BatchWriteItem, TransactWriteItems,
      # DeleteRule and RemoveTargets reachable -- each of which can mutate
      # the record or stop the schedule just as effectively. A deny list that
      # has to enumerate every verb is a deny list that silently rots as AWS
      # adds APIs.
      #
      # None of these prefixes collide with the allows above: DynamoDB reads
      # are Describe/Get/BatchGet/Query/Scan, Lambda is Get only, and nothing
      # here grants EventBridge at all.
      {
        Sid    = "NeverReachCredentialsOrDeviceControl"
        Effect = "Deny"
        Action = [
          "secretsmanager:*",
          # The schedule itself: no rule, target, or enablement change.
          "events:*",
          # Arming lives in the function's environment; code and invocation
          # are equally direct routes to moving the globe.
          "lambda:Add*",
          "lambda:Delete*",
          "lambda:Invoke*",
          "lambda:Publish*",
          "lambda:Put*",
          "lambda:Remove*",
          "lambda:Tag*",
          "lambda:Untag*",
          "lambda:Update*",
          # Interventions must stay traceable, so the record is read-only:
          # no item writes, no table-level mutation, no restore or import
          # that could overwrite it wholesale.
          "dynamodb:BatchWrite*",
          "dynamodb:Delete*",
          "dynamodb:Import*",
          "dynamodb:Put*",
          "dynamodb:Restore*",
          "dynamodb:TransactWrite*",
          "dynamodb:Update*",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_ssoadmin_account_assignment" "owner" {
  instance_arn       = local.instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.diagnostics_readonly.arn

  principal_id   = data.aws_identitystore_user.owner.user_id
  principal_type = "USER"

  target_id   = local.account_id
  target_type = "AWS_ACCOUNT"
}
