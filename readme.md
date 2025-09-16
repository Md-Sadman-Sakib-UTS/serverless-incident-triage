# SIREN — Serverless Incident Triage (Showcase, ap-southeast-2)

A low-cost, end-to-end incident triage demo. It ingests system metrics and incident logs from S3, computes simple aggregates (including approx MTTR), and asks Anthropic Claude (via Amazon Bedrock in Sydney) to produce a concise posture summary. The flow is orchestrated by AWS Step Functions with AWS Lambda steps. It includes a human-in-the-loop approval gate and email notifications via Amazon SNS, plus CloudWatch EMF metrics and a small dashboard.

This README explains exactly what was built and how to run it.

---

## Contents
- What You Get
- Architecture
- Repository Layout
- Prerequisites
- Deploy
- Data → S3
- Configuration & Environment
- State Machine & Lambdas
  - metrics_get
  - logs_query
  - agent_invoke
  - request_approval (WAIT_FOR_TASK_TOKEN)
  - notify (SNS email)
- Run the Pipeline
  - Manual approval (step-by-step)
  - Hands-free demo (optional)
- Observability
- Model Selection
- Cost Controls
- Troubleshooting
- Clean Up

---

## What You Get
- **Region**: `ap-southeast-2` (Sydney)
- **Data sources (S3)**:
  - `data/system_performance_metrics.csv` (minute CPU/memory/disk)
  - `data/incident_event_log.csv` (opened/resolved/closed; category, priority, timestamps)
- **Pipeline (Step Functions)**: `metrics_get → logs_query → agent_invoke → request_approval → notify`
- **Notifications**: Email via SNS for both approval requests and final summaries
- **Human-in-the-loop**: `request_approval` waits for a task token you approve from the CLI
- **Observability**: CloudWatch EMF metrics + dashboard `SIREN-Posture`
- **Reliability**: Retries on each task, small payloads, short log retention
- **Cost**: Default model Claude 3 Haiku; AWS Budgets guard at $10/month

---

## Architecture

```mermaid
flowchart LR
  A[S3: metrics CSV] --> B[metrics_get (Lambda)]
  D[S3: incidents CSV] --> C[logs_query (Lambda)]
  B --> C --> E[agent_invoke (Lambda) → Bedrock Claude (ap-southeast-2)]
  E --> G[request_approval (LambdaInvoke.waitForTaskToken → email token via SNS)]
  G --> F[notify (Lambda → SNS email)]
  subgraph "Step Functions: SirenStateMachine"
    B --- C --- E --- G --- F
  end
```

---

## Repository Layout

```
siren/
├─ bedrock/
│   └─ knowledge_base/      # optional: if you later add DIY RAG
├─ data/
│   └─ raw/                 # local CSVs before upload to S3
├─ infra/                   # CDK v2 TypeScript app
│   ├─ bin/infra.ts
│   └─ lib/infra-stack.ts
├─ lambdas/
│   ├─ metrics_get/handler.py
│   ├─ logs_query/handler.py
│   ├─ agent_invoke/handler.py
│   └─ notify_slack/handler.py   # acts as notify + approval handler (SNS email)
├─ tools/
│   └─ demo.sh              # one-command runner (prints Claude summary)
└─ README.md
```

> The notifier Lambda file is named `notify_slack/handler.py`, but in this showcase it publishes to SNS email.

---

## Prerequisites
- AWS account with permissions for CDK, Lambda, Step Functions, S3, CloudWatch, Bedrock, SNS
- Region: `ap-southeast-2`
- Bedrock access (Sydney): enable Anthropic models (Haiku; Sonnet optional)
- Local tools:
  - Python 3.10+
  - Node.js 18+
  - AWS CLI v2
  - `jq`
  - AWS CDK v2 (use via `npx aws-cdk@2`)

Check Anthropic models in Sydney:

```bash
aws bedrock list-foundation-models \
  --region ap-southeast-2 \
  --by-provider anthropic \
  --query "modelSummaries[].modelId"
```

---

## Deploy

```bash
cd infra
# One-time bootstrap
export CDK_DEFAULT_REGION=ap-southeast-2
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
npx aws-cdk@2 bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION

# Build & deploy
npm run build
npx aws-cdk@2 deploy --require-approval never
```

This creates:
- S3 artifacts bucket
- Lambdas (`metrics_get`, `logs_query`, `agent_invoke`, `notify`/approval)
- Step Functions state machine with retries + approval gate
- CloudWatch log groups and dashboard `SIREN-Posture`
- SNS topic + email subscription

---

## Data → S3

Prepare two small CSVs (you can use your own):

Metrics (`system_performance_metrics.csv`)
```
timestamp,cpu_usage,memory_usage,disk_usage
```

Incidents (`incident_event_log.csv`)
```
number,incident_state,opened_at,resolved_at,closed_at,category,priority,...
```

Upload to your stack’s bucket:

```bash
BUCKET="<ArtifactsBucketName from your stack>"

aws s3 cp data/raw/system_performance_metrics.csv \
  "s3://$BUCKET/data/system_performance_metrics.csv"

aws s3 cp data/raw/incident_event_log.csv \
  "s3://$BUCKET/data/incident_event_log.csv"

aws s3 ls "s3://$BUCKET/data/"
```

---

## Configuration & Environment

Key Lambda env vars (set by CDK):
- `ARTIFACTS_BUCKET` — S3 bucket name for data/artifacts
- `DATA_KEY` — metrics CSV key (default `data/system_performance_metrics.csv`)
- `INCIDENTS_KEY` — incident CSV key (default `data/incident_event_log.csv`)
- `MAX_ROWS` — cap for scanned incident rows (default `50000`)
- `BEDROCK_REGION` — `ap-southeast-2`
- `BEDROCK_MODEL_ID` — default `anthropic.claude-3-haiku-20240307-v1:0`
- `SNS_TOPIC_ARN` — notify/approval email topic
- `AUTO_APPROVE` — optional (`true|false`) for hands-free demos (default off)

IAM principles:
- `s3:GetObject` for the data keys
- `logs:*` for application logging
- `bedrock:InvokeModel` scoped to Anthropic model ARNs in Sydney
- `sns:Publish` to the notify topic
- `states:SendTaskSuccess/Failure` (only used if auto-approve is enabled)

---

## State Machine & Lambdas

### metrics_get
Purpose: stream metrics CSV, compute tiny averages and head sample.

Example response:
```json
{
  "stage": "metrics_get",
  "ok": true,
  "count": 30,
  "averages": {"cpu": 42.8, "memory": 63.6, "disk": 50.7},
  "head": [{ "...": "..." }]
}
```

### logs_query
Purpose: stream incident CSV without full read; compute totals, top categories/priorities, and approx MTTR (hours). Emits EMF metric `RowsScanned` (`Namespace`: `SIREN/App`, `Stage`: `logs_query`).

Example response:
```json
{
  "stage": "logs_query",
  "ok": true,
  "summary": {
    "rows_scanned": 50001,
    "incidents": 7472,
    "resolved_count": 7013,
    "approx_mttr_hours": 253.95,
    "top_categories": [{"value": "Category 26", "count": 878}],
    "by_priority": [{"value": "3 - Moderate", "count": 6993}]
  },
  "prev": { "...": "metrics_get payload ..." }
}
```

### agent_invoke
Purpose: build a concise prompt from aggregates + averages; call Claude via Bedrock Messages API (Sydney). Emits EMF metric `SummarizerLatencyMs` (`Namespace`: `SIREN/App`, `Stage`: `agent_invoke`).

Example response:
```json
{
  "stage": "agent_invoke",
  "ok": true,
  "model": "anthropic.claude-3-haiku-20240307-v1:0",
  "region": "ap-southeast-2",
  "llm_text": "… concise posture summary …",
  "input_preview": {
    "incidents": 7472,
    "mttr_hours": 253.95,
    "averages": {"cpu": 42.8, "memory": 63.6, "disk": 50.7}
  }
}
```

### request_approval (WAIT_FOR_TASK_TOKEN)
Purpose: pause the workflow for a human decision. The notifier sends an email titled “SIREN Approval Requested” that includes a copy/paste CLI command. Step Functions waits until you call `send-task-success` (approve) or `send-task-failure` (reject).

### notify (SNS email)
Purpose: publish the final Claude summary to email and pass through a stable shape for the CLI demo.

Example response:
```json
{ "stage": "notify", "ok": true, "prev": { "llm_text": "..." } }
```

---

## Run the Pipeline

### One-liner (prints only the Claude summary)
```bash
./tools/demo.sh
```
The script starts an execution, waits for completion, and prints `.prev.llm_text`.

### Manual approval (step-by-step)
1. Run the demo:
   ```bash
   ./tools/demo.sh
   ```
2. Watch for the “SIREN Approval Requested” email. Copy the token (characters inside the single quotes after `--task-token`).
3. Approve:
   ```bash
   TOKEN='PASTE_TOKEN_HERE'
   aws stepfunctions send-task-success \
     --task-token "$TOKEN" \
     --task-output '{"approved": true}' \
     --region ap-southeast-2
   ```
4. The execution resumes; the terminal prints the Claude summary. You also receive a “SIREN Posture Summary” email.

Example final text (sanitized):
> “The current health of the system is generally stable, with 7,013 incidents resolved out of 7,472. The average time to resolve incidents is ~254 hours. Avg CPU 42.8%, memory 63.6%, disk 50.7%. Top categories are ‘Category 26’ and ‘Category 53’. Most incidents are ‘3 - Moderate’. Next: address top categories and reduce MTTR.”

### Hands-free demo (optional)
Enable auto-approve in CDK (for demo environments only):

```ts
// infra/lib/infra-stack.ts
notifySlack.addEnvironment('AUTO_APPROVE', 'true');
```

Redeploy, then simply run `./tools/demo.sh`.

---

## Observability
- **EMF Metrics** (CloudWatch → Metrics → Custom → `SIREN/App`):
  - `RowsScanned` (`Stage=logs_query`)
  - `SummarizerLatencyMs` (`Stage=agent_invoke`)
- **Dashboard** (CloudWatch → Dashboards → `SIREN-Posture`):
  1. Rows Scanned (sum, 5-minute period)
  2. LLM Latency p95 (5-minute period)

Tip: after a run, set the time range to “Last 1h” and refresh.

---

## Model Selection

Default model:
```
anthropic.claude-3-haiku-20240307-v1:0
```

Switch to Sonnet (if enabled in Sydney):
```ts
// infra/lib/infra-stack.ts
agentInvoke.addEnvironment('BEDROCK_MODEL_ID', 'anthropic.claude-3-sonnet-20240229-v1:0');
// or: 'anthropic.claude-3-5-sonnet-20240620-v1:0'
```

Deploy:
```bash
cd infra && npm run build && npx aws-cdk@2 deploy --require-approval never
```

---

## Cost Controls
- Manual trigger (no schedule)
- Short log retention (e.g., 7 days)
- Small payload caps (`SAMPLE_ROWS`, `MAX_ROWS`)
- AWS Budgets guard at $10/month with alerts:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws budgets create-budget \
  --account-id "$ACCOUNT_ID" \
  --budget '{"BudgetName":"SIREN-monthly-10usd","BudgetLimit":{"Amount":"10","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}'

ALERT_EMAIL="you@example.com"
aws budgets create-notification --account-id "$ACCOUNT_ID" --budget-name "SIREN-monthly-10usd" \
  --notification '{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"}' \
  --subscribers "[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]"

aws budgets create-notification --account-id "$ACCOUNT_ID" --budget-name "SIREN-monthly-10usd" \
  --notification '{"NotificationType":"FORECASTED","ComparisonOperator":"GREATER_THAN","Threshold":100,"ThresholdType":"PERCENTAGE"}' \
  --subscribers "[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]"
```

---

## Troubleshooting
- **TIMED_OUT during approval**: approve within the wait window using the latest token email; otherwise start a new run.
- **Invalid token**: copy only the characters inside the single quotes after `--task-token` in the email.
- **No widgets on dashboard**: run a demo first, set “Last 1h,” refresh. Use `aws cloudwatch put-dashboard` if needed.
- **NoSuchKey**: ensure both CSVs are uploaded to `s3://<ArtifactsBucketName>/data/...` and env keys match.
- **AccessDenied / model not enabled**: enable Anthropic models in Bedrock (Sydney) and ensure the Lambda role allows `bedrock:InvokeModel`.
- **CLI parsing race**: don’t `fromjson` until the execution status is `SUCCEEDED`.

---

## Clean Up

```bash
# Remove the stack
cd infra
npx aws-cdk@2 destroy --force

# Clear data but keep the stack
aws s3 rm "s3://<ArtifactsBucketName>/data/" --recursive
```

---


