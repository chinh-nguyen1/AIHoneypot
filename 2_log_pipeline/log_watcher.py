import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pipeline_main import process_line

LOG_FILE_PATH = os.path.abspath("../1_decoy_web/logs/access.log")

class LogFileHandler(FileSystemEventHandler):
    def __init__(self, filepath):
        self.filepath = filepath
        self.file = open(filepath, "r", encoding="utf-8", errors="ignore")
        self.file.seek(0, os.SEEK_END)  # Di chuyển xuống cuối file log hiện tại

    def on_modified(self, event):
        if os.path.abspath(event.src_path) == self.filepath:
            while True:
                line = self.file.readline()
                if not line:
                    break
                process_line(line)

def start_watching():
    log_dir = os.path.dirname(LOG_FILE_PATH)
    os.makedirs(log_dir, exist_ok=True)
    if not os.path.exists(LOG_FILE_PATH):
        open(LOG_FILE_PATH, "w").close()

    event_handler = LogFileHandler(LOG_FILE_PATH)
    observer = Observer()
    observer.schedule(event_handler, path=log_dir, recursive=False)
    observer.start()
    print(f"[*] [Log Watcher] Đang lắng nghe file log thời gian thực tại: {LOG_FILE_PATH}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n[*] Đã dừng Log Watcher.")
    observer.join()

if __name__ == "__main__":
    start_watching()