import urllib.request, json
API_KEY = "5KcIThwoz1xdx77tUDu3NsiE4KgxSINE/Drm7BgGd4Q="
req = urllib.request.Request("https://api.cron-job.org/jobs",
    headers={"Authorization": f"Bearer {API_KEY}"})
with urllib.request.urlopen(req) as r:
    jobs = json.loads(r.read()).get("jobs", [])
for j in jobs:
    if "TikTok-FB" in j.get("title",""):
        s = j.get("schedule", {})
        print(f"  #{j['jobId']} | {j['title']} | hours={s.get('hours')} min={s.get('minutes')} | enabled={j['enabled']}")
