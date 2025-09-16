import os, json, boto3

sns = boto3.client('sns')


def _publish(topic_arn: str, subject: str, message: str):
    if topic_arn:
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=message)


def handler(event, context):
    """
    Mode A: Approval request (event has taskToken + summary)
      -> Email the token + copy/paste CLI commands. Step Functions WAITS.
    Mode B: Final notify (no taskToken; event has llm_text)
      -> Email the Claude summary. Return stable shape for demo.sh.
    """
    topic = os.environ.get('SNS_TOPIC_ARN')

    if isinstance(event, dict) and 'taskToken' in event:
        token = event['taskToken']
        summary = event.get('summary') or "No summary."
        approve_cmd = (
            "aws stepfunctions send-task-success "
            f"--task-token '{token}' "
            f"--task-output '{json.dumps({'approved': True})}' "
            "--region ap-southeast-2"
        )
        reject_cmd = (
            "aws stepfunctions send-task-failure "
            f"--task-token '{token}' "
            "--error 'Rejected' --cause 'User declined' "
            "--region ap-southeast-2"
        )
        message = (
            "SIREN approval requested.\n\n"
            f"Summary:\n{summary}\n\n"
            "To APPROVE:\n"
            f"{approve_cmd}\n\n"
            "To REJECT:\n"
            f"{reject_cmd}\n"
        )
        _publish(topic, "SIREN Approval Requested", message)
        return {"stage": "request_approval", "ok": True}

    text = (event.get('llm_text') if isinstance(event, dict) else None) or "No summary."
    _publish(topic, "SIREN Posture Summary", text)
    return {"stage": "notify", "ok": True, "prev": event}
