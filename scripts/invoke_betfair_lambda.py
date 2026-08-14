from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    args = parse_args()
    with tempfile.TemporaryDirectory() as tmpdir:
        payload_path = Path(tmpdir) / "payload.json"
        response_path = Path(tmpdir) / "response.json"
        payload_path.write_text(
            json.dumps({"csv": args.input_csv.read_text(encoding="utf-8")}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "aws",
                "lambda",
                "invoke",
                "--region",
                args.region,
                "--function-name",
                args.function_name,
                "--cli-binary-format",
                "raw-in-base64-out",
                "--payload",
                f"file://{payload_path}",
                str(response_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(result.stdout or "{}")
        if metadata.get("FunctionError"):
            raise RuntimeError(f"Betfair Lambda failed: {response_path.read_text()}")

        response = json.loads(response_path.read_text(encoding="utf-8"))
        status_code = int(response.get("statusCode", 500))
        body = json.loads(response.get("body") or "{}")
        if status_code >= 400:
            raise RuntimeError(f"Betfair Lambda returned {status_code}: {body}")

        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        args.output_csv.write_text(str(body["csv"]), encoding="utf-8")
        configured = body.get("betfair_configured")
        auth_error = body.get("auth_error_type")
        print(f"Wrote {args.output_csv}; betfair_configured={configured}")
        if auth_error:
            print(f"Betfair Lambda auth_error_type={auth_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Invoke the Betfair enrichment Lambda.")
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    main()
