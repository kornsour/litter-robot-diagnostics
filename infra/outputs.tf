output "watchdog_function_name" {
  value = aws_lambda_function.watchdog.function_name
}

output "watchdog_state_table" {
  value = aws_dynamodb_table.watchdog.name
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "watchdog_alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
