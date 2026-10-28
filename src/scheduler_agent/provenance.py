"""运行证据指纹。只读取代码、提示和合成数据，不读取 .env。"""
from pathlib import Path
import platform

from .models import digest


def run_provenance(dataset, prompt: str, schemas: list) -> dict:
    package = Path(__file__).parent
    source = {path.name: path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))}
    data = {name: getattr(dataset, name) for name in ("materials", "rules", "existing_schedule", "candidate_slots")}
    return {"source_sha256": digest(source), "prompt_sha256": digest(prompt),
            "tools_sha256": digest(schemas), "dataset_sha256": digest(data),
            "python_version": platform.python_version()}
