def handler(event, context):
    # placeholder: later we’ll integrate a human/auto approval gate
    return {"stage": "approval_check", "approved": True, "prev": event}