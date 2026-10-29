import argparse
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from myself_agent.config import DEFAULT_CONFIG, load_settings
from myself_agent.scenarios import SCENARIOS
from myself_agent.workflow import Workflow, write_json


def main():
    parser = argparse.ArgumentParser(description="DeepSeek + LangGraph isolated software workflow")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-root", type=Path, default=Path("/data/myself-agent/runs"))
    parser.add_argument("--max-repairs", type=int, choices=range(4), default=3)
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="csv-orders")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--brief-file", type=Path)
    parser.add_argument("--baseline-file", type=Path)
    parser.add_argument("--run-id", help="Existing web-created run directory, 32 hex characters")
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="Explicitly replace round-0 stats with a faulty stub to demonstrate real repair",
    )
    args = parser.parse_args()
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    run_dir = None
    try:
        settings = load_settings(
            args.config,
            run_root=args.run_root,
            max_repairs=args.max_repairs,
        )
        subprocess.run(
            ["sudo", "-n", "docker", "image", "inspect", settings.image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
        run_id = args.run_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run identifier")
        run_dir = settings.run_root / run_id
        if run_dir.is_symlink():
            raise ValueError("Run directory must not be a symlink")
        if args.run_id:
            if not (run_dir / "request.json").is_file():
                raise ValueError("Web-created request is required")
        else:
            run_dir.mkdir(parents=True, mode=0o750, exist_ok=False)
        brief = args.brief_file.read_text(encoding="utf-8") if args.brief_file else ""
        if len(brief) > 1500:
            raise ValueError("Implementation preferences are too long")
        result = Workflow(
            settings,
            run_dir,
            run_id,
            args.inject_failure,
            args.scenario,
            brief,
            args.concurrency,
            base_artifacts=json.loads(args.baseline_file.read_text())
            if args.baseline_file
            else None,
        ).run()
    except Exception as error:
        # Avoid dumping HTTP error bodies, credentials, or arbitrary SDK reprs.
        result = {"outcome": "error", "error_type": type(error).__name__}
        if run_dir is not None:
            result["run_directory"] = str(run_dir)
            write_json(run_dir / "error.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["outcome"] == "passed" else 1
