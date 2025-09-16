#!/usr/bin/env bash
set -euo pipefail

SM_ARN="${SM_ARN:-$(aws stepfunctions list-state-machines \
  --query 'stateMachines[?contains(name, `SirenStateMachine`)].stateMachineArn' --output text)}"

if [[ -z "${SM_ARN}" ]]; then
  echo "Could not resolve State Machine ARN. Export SM_ARN and retry." >&2
  exit 1
fi

EXEC_ARN=$(aws stepfunctions start-execution --state-machine-arn "$SM_ARN" --input '{}' --query executionArn --output text)
echo "Started: $EXEC_ARN"

# tiny poll loop to avoid parsing before output is ready
while STATUS=$(aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" --query status --output text); \
      [ "$STATUS" = "RUNNING" ]; do sleep 1; done

if [[ "$STATUS" != "SUCCEEDED" ]]; then
  aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" --output json \
  | jq ' {status, error, cause} '
  exit 2
fi

# print only the Claude summary
aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" --output json \
| jq -r '.output | fromjson | .prev.llm_text // "(no llm_text)"'
