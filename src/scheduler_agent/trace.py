from datetime import datetime, timezone
import json
from pathlib import Path


class Trace:
    def __init__(self, path: str | Path, run_id: str, mode: str):
        self.path, self.run_id, self.mode = Path(path), run_id, mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每次运行必须使用新文件，禁止悄悄覆盖已存在的验收证据。
        self.file = self.path.open("x", encoding="utf-8")

    def write(self, event: str, **fields):
        self.file.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                                   "run_id": self.run_id, "model_mode": self.mode,
                                   "event": event, **fields}, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()
