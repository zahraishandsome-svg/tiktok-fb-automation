import sqlite3, os

db_path = "data/automation.db"
if not os.path.exists(db_path):
    print("No local DB — will be reset from GitHub cache anyway")
else:
    conn = sqlite3.connect(db_path)
    vid = "7643923272957955342"
    conn.execute(
        "UPDATE videos SET status='pending', retry_count=0, next_retry_date=NULL, error_message=NULL "
        "WHERE tiktok_video_id=?", (vid,)
    )
    print(f"Cleared retry for video {vid}: {conn.total_changes} row(s)")
    conn.execute(
        "DELETE FROM runs WHERE channel_id='page_1' AND run_date=date('now')"
    )
    print(f"Deleted today's run records: {conn.total_changes} total changes")
    conn.commit()
    conn.close()
    print("Done.")
