import os, json, boto3, time


def _round(v, nd=1):
    try:
        return round(float(v), nd)
    except Exception:
        return v


def _build_prompt(event):
    summ = event.get("summary", {}) or {}
    prev = event.get("prev", {}) or {}
    avgs = prev.get("averages") or {}
    top_cat = (summ.get("top_categories") or [])[:2]
    top_pri = (summ.get("by_priority") or [])[:2]

    lines = []
    lines.append("You are an SRE assistant. Produce a concise incident posture summary.")
    lines.append(f"- Incidents total: {summ.get('incidents')}, resolved: {summ.get('resolved_count')}")
    lines.append(f"- Approx MTTR (hours): {summ.get('approx_mttr_hours')}")
    lines.append(f"- Avg CPU: {_round(avgs.get('cpu'))} | Avg Memory: {_round(avgs.get('memory'))} | Avg Disk: {_round(avgs.get('disk'))}")
    if top_cat: lines.append(f"- Top categories: {top_cat}")
    if top_pri: lines.append(f"- Top priorities: {top_pri}")
    lines.append("Write 4–6 sentences with: current health, likely hotspots, and 2 next actions. Return plain text.")
    return "\n".join(lines)


def _emit_llm_latency(ms: int):
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "SIREN/App",
                "Dimensions": [["Stage"]],
                "Metrics": [{"Name": "SummarizerLatencyMs", "Unit": "Milliseconds"}]
            }]
        },
        "Stage": "agent_invoke",
        "SummarizerLatencyMs": int(ms)
    }))


def handler(event, context):
    t0 = time.time()
    # Default to Haiku in Sydney; you can switch to Sonnet via env later
    model_id = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    region   = os.environ.get("BEDROCK_REGION", "ap-southeast-2")
    prompt   = _build_prompt(event)

    brt = boto3.client("bedrock-runtime", region_name=region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "max_tokens": 350,
        "temperature": 0.2
    }
    resp = brt.invoke_model(
        modelId=model_id,
        body=json.dumps(body).encode("utf-8"),
        accept="application/json",
        contentType="application/json"
    )
    data = json.loads(resp["body"].read())
    text = None
    try:
        content = data.get("content") or []
        if content and isinstance(content, list) and "text" in content[0]:
            text = content[0]["text"]
    except Exception:
        pass

    latency_ms = int((time.time() - t0) * 1000)
    _emit_llm_latency(latency_ms)

    return {
        "stage": "agent_invoke",
        "ok": True,
        "model": model_id,
        "region": region,
        "llm_text": text or "(no text returned)",
        "input_preview": {
            "incidents": (event.get("summary") or {}).get("incidents"),
            "mttr_hours": (event.get("summary") or {}).get("approx_mttr_hours"),
            "averages":  (event.get("prev") or {}).get("averages", {})
        }
    }
