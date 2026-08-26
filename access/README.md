# Read-only diagnostic access

An IAM Identity Center permission set that can read the watchdog's operational
record — CloudWatch logs, alarm history, the DynamoDB state and intervention
records — and nothing else. It exists so that inspecting production does not
require the account root user.

## Why this is not in `infra/`

Two reasons, both load-bearing:

1. **CI applies `infra/`.** `.github/workflows/deploy-watchdog.yml` triggers on
   `infra/**` and runs `tofu apply` there with the `lr4-github-deploy` role.
   That role holds no `sso-admin` or `identitystore` permissions, so these
   resources would fail every deploy.
2. **It must stay that way.** A CI role that can manage permission sets can
   grant itself administrator access. Giving the deploy role those permissions
   to fix (1) would open a privilege-escalation path in exchange for the
   ability to read logs — a bad trade. This module is applied by hand, by a
   principal that already has the authority, and keeps its own state file.

## What it grants

| Scope | Actions |
|---|---|
| `/aws/lambda/lr4-watchdog` log group | describe, filter, get, Insights queries |
| `lr4-watchdog-*` alarms | `DescribeAlarms`, `DescribeAlarmHistory` |
| `LR4Watchdog` + `AWS/Lambda` namespaces | `GetMetricStatistics`, `GetMetricData`, `ListMetrics` |
| `lr4-watchdog` table | `DescribeTable`, `GetItem`, `BatchGetItem`, `Query`, `Scan` |
| `lr4-watchdog` function | `GetFunctionConfiguration` |
| `lr4-watchdog-alerts` topic | `GetTopicAttributes`, `ListSubscriptionsByTopic` |

And an explicit `Deny` on Secrets Manager, on all of EventBridge, on every
mutating Lambda verb, and on every DynamoDB write verb — expressed as action
*prefixes* (`dynamodb:Put*`, `lambda:Update*`, `events:*`, …) rather than named
actions. An earlier version enumerated `PutItem`/`UpdateItem`/`DeleteItem` and
left `BatchWriteItem`, `TransactWriteItems`, `DeleteRule` and `RemoveTargets`
reachable; a deny list that has to name every verb rots as AWS adds APIs.

None of those prefixes collide with the allows above: the DynamoDB reads are
`Describe`/`Get`/`BatchGet`/`Query`/`Scan`, Lambda is `Get` only, and
EventBridge is not granted at all.

Nothing in the allow list grants any of it; the deny is there so a later edit
cannot introduce them by accident.

Two deliberate imprecisions, both metadata-only:

- **`AWS/Lambda` metrics are account-wide.** CloudWatch metric reads carry no
  resource ARN in IAM, so they are constrained by namespace instead. This
  exposes invocation and error *counts* for other functions in the account —
  no log content, no payloads.
- **`logs:DescribeLogGroups`** may ignore the resource scope depending on how
  the API is called; it reveals log group names only.

## Applying it

Needs a principal that can manage Identity Center — the account root user or an
existing administrator, not the CI role.

`owner_user_name` has no default and must be supplied — put it in
`access/terraform.tfvars` (gitignored) or pass `-var owner_user_name=...`:

```bash
cd access
tofu init \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="dynamodb_table=${TF_LOCK_TABLE:-lr4-watchdog-tofu-lock}"
tofu plan
```

Review, then `tofu apply`. Afterwards, add a profile to `~/.aws/config`:

```ini
[profile lr4-readonly]
sso_session = lr4
sso_account_id = <AWS_ACCOUNT_ID>
sso_role_name = LR4Diagnostics-ReadOnly
region = us-west-2

[sso-session lr4]
sso_start_url = https://<IDENTITY_STORE_ID>.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access
```

Both placeholders are deliberate. An account ID and a working access-portal URL
are not credentials, but together with a valid user name they are most of what
an SSO consent-phishing attempt needs, and this file is public. Fill them in
locally; do not commit the filled-in version.

To find them: `aws sts get-caller-identity --query Account --output text` gives
the account ID, and `aws sso-admin list-instances` gives the identity-store ID
(`d-` followed by ten hex characters). The `d-` form is the identity-store
default and keeps working even if a custom subdomain is later configured in
*IAM Identity Center → Settings → AWS access portal URL*.

Then `aws sso login --profile lr4-readonly`, and pass `--profile lr4-readonly`
to diagnostic commands. Credentials are short-lived; no access keys are created
anywhere in this module.

Once this works, the account root user is no longer needed for reading
production, which is the point of the exercise.
