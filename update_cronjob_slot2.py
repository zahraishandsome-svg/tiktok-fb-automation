"""Update cron-job.org job #7683500 (Slot 2) from 20:00 UTC to 17:00 UTC (10PM PKT)."""
import urllib.request
import json

CRONJOB_API = "https://api.cron-job.org"
API_KEY = "5KcIThwoz1xdx77tUDu3NsiE4KgxSINE/Drm7BgGd4Q="
GH_TOKEN = "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"
REPO = "zahraishandsome-svg/tiktok-fb-automation"
JOB_ID = 7683500  # TikTok-FB Slot 2

payload = json.dumps({
    "job": {
        "url": f"https://api.github.com/repos/{REPO}/actions/workflows/upload-slot2.yml/dispatches",
        "enabled": True,
        "title": "TikTok-FB Slot 2 (10PM PKT)",
        "saveResponses": True,
        "schedule": {
            "timezone": "UTC",
            "hours": [17],
            "minutes": [0],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
        "requestMethod": 1,
        "extendedData": {
            "headers": {
                "Authorization": f"token {GH_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            "body": '{"ref":"main"}',
        },
    }
}).encode()

req = urllib.request.Request(
    f"{CRONJOB_API}/jobs/{JOB_ID}",
    data=payload,
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    method="PATCH",
)
try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        print(f"Updated job #{JOB_ID}: {result}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}")
