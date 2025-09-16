import os, csv, io, boto3

s3 = boto3.client('s3')

def handler(event, context):
    bucket = os.environ['ARTIFACTS_BUCKET']  # set by CDK
    key = os.environ.get('DATA_KEY', 'data/system_performance_metrics.csv')
    sample_rows = int(os.environ.get('SAMPLE_ROWS', '30'))  # keep small for payload cost

    # fetch file
    obj = s3.get_object(Bucket=bucket, Key=key)
    text = obj['Body'].read().decode('utf-8')

    # parse csv
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for i, r in enumerate(reader):
        if i >= sample_rows:
            break
        try:
            rows.append({
                "timestamp": r["timestamp"],
                "cpu": float(r["cpu_usage"]),
                "memory": float(r["memory_usage"]),
                "disk": float(r["disk_usage"])
            })
        except Exception:
            # skip malformed rows silently
            continue

    n = len(rows)
    if n == 0:
        return {"stage": "metrics_get", "ok": False, "error": "no rows parsed", "key": key}

    def avg(k): return sum(row[k] for row in rows) / n

    return {
        "stage": "metrics_get",
        "ok": True,
        "count": n,
        "averages": {"cpu": avg("cpu"), "memory": avg("memory"), "disk": avg("disk")},
        "head": rows[:5]  # tiny preview to keep Step Functions payloads small
    }