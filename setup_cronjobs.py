"""
Add TikTok-FB automation cron jobs to cron-job.org.
Usage: python setup_cronjobs.py
"""
import json
import time
import urllib.request
import urllib.error

CRONJOB_API = "https://api.cron-job.org"
API_KEY = "5KcIThwoz1xdx77tUDu3NsiE4KgxSINE/Drm7BgGd4Q="
GITHUB_TOKEN = "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"
REPO = "zahraishandsome-svg/tiktok-fb-automation"

JOBS = [
    ("TikTok-FB Slot1 (8PM PKT)", "upload-slot1.yml", 15, 0),
    ("TikTok-FB Slot2 (10PM PKT)", "upload-slot2.yml", 17, 0),
]


def create_job(title, workflow_file, hour, minute):
    dispatch_url = (
        f"https://api.github.com/repos/{REPO}"
        f"/actions/workflows/{workflow_file}/dispatches"
    )
    payload = json.dumps({
        "job": {
            "url": dispatch_url,
            "enabled": True,
            "title": title,
            "saveResponses": True,
            "schedule": {
                "timezone": "UTC",
                "hours": [hour],
                "minutes": [minute],
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1],
            },
            "requestMethod": 1,
            "extendedData": {
                "headers": {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                "body": '{"ref":"main"}',
            },
        }
    }).encode()

    req = urllib.request.Request(
        f"{CRONJOB_API}/jobs",
        data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            job_id = result.get("jobId", "?")
            print(f"  OK #{job_id}: {title}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  FAIL: {title} — HTTP {e.code}: {body}")
        return False


if __name__ == "__main__":
    print("Creating TikTok-FB cron-job.org entries...\n")
    for title, wf, hour, minute in JOBS:
        create_job(title, wf, hour, minute)
        time.sleep(2)
    print("\nDone.")
