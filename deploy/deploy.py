"""Put the current code on the hosted instance and (re)start it.

    python deploy/deploy.py                 install or update specdev
    python deploy/deploy.py page            publish only the page (no restart, conversations are kept)
    python deploy/deploy.py run "COMMAND"   run a shell command on the instance, as root

Needs the AWS CLI, signed in, and the `specdev` stack (deploy/specdev.yaml) created.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACK = "specdev"
MODEL = "global.anthropic.claude-sonnet-5"      # Sonnet 5.5 is not enabled on Bedrock in this account yet
SMALL_MODEL = "global.anthropic.claude-haiku-5-5"
STARTING_BUDGET = "10"          # dollars a new person can spend before topping up
STOP_MINUTES = "120"            # the instance switches itself off after this long unused
MCP_PERSON_FILE = ROOT / "deploy" / "mcp-person.txt"   # kept out of the repository
# Whose workbench assistants use through the MCP server: the sign-in address, from the environment or the
# file above ("" = no MCP server).
MCP_PERSON = os.environ.get("SPECDEV_MCP_PERSON") or (MCP_PERSON_FILE.read_text().strip() if MCP_PERSON_FILE.is_file() else "")
MCP_KEY_FILE = ROOT / "deploy" / "mcp-key.txt"     # the access key in the MCP address; made on first deploy


def mcp_settings(out: dict[str, str]) -> list[str]:
    if not MCP_PERSON:
        return []
    if not MCP_KEY_FILE.is_file():
        import secrets

        MCP_KEY_FILE.write_text(secrets.token_urlsafe(24))
    person = json.loads(aws("cognito-idp", "admin-get-user", "--user-pool-id", out["UserPoolId"], "--username", MCP_PERSON))
    sub = next(a["Value"] for a in person["UserAttributes"] if a["Name"] == "sub")
    return [f"SPECDEV_MCP_KEY={MCP_KEY_FILE.read_text().strip()}", f"SPECDEV_MCP_USER={sub}",
            f"SPECDEV_URL={out['Url'].rstrip('/')}"]


def publish_page(out: dict[str, str]) -> None:
    """The page and its settings go to the bucket, so they load even while the instance is off."""
    config = {"auth": {"domain": out["AuthDomain"], "client": out["UserPoolClientId"], "cookie": "specdev_token"}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(config, fh)
    aws("s3", "cp", fh.name, f"s3://{out['Bucket']}/site/api/config", "--content-type", "application/json")
    Path(fh.name).unlink()
    aws("s3", "cp", str(ROOT / "specdev" / "static" / "index.html"), f"s3://{out['Bucket']}/site/index.html",
        "--content-type", "text/html; charset=utf-8")


def start(instance: str) -> None:
    """Make sure the instance is running and answering Systems Manager."""
    state = json.loads(aws("ec2", "describe-instances", "--instance-ids", instance))
    if state["Reservations"][0]["Instances"][0]["State"]["Name"] != "running":
        print("starting the instance...")
        aws("ec2", "start-instances", "--instance-ids", instance)
        aws("ec2", "wait", "instance-running", "--instance-ids", instance)
    for _ in range(40):
        info = json.loads(aws("ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={instance}"))
        if any(i["PingStatus"] == "Online" for i in info["InstanceInformationList"]):
            return
        time.sleep(5)
    sys.exit("the instance is running but not answering Systems Manager")


def aws(*args: str) -> str:
    result = subprocess.run(["aws", *args, "--output", "json", "--no-cli-pager"], capture_output=True, text=True)
    if result.returncode:
        sys.exit(f"aws {' '.join(args[:2])} failed:\n{result.stderr.strip()}")
    return result.stdout


def outputs() -> dict[str, str]:
    stack = json.loads(aws("cloudformation", "describe-stacks", "--stack-name", STACK))["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def bundle() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "deploy" / "setup.sh", ROOT / "deploy" / "smoke.py",
                     *(ROOT / "specbot").rglob("*"), *(ROOT / "specdev").rglob("*")]:
            if path.is_file() and "__pycache__" not in path.parts:
                data = path.read_bytes()
                if path.suffix == ".sh":
                    data = data.replace(b"\r\n", b"\n")
                z.writestr(path.relative_to(ROOT).as_posix(), data)
    return buf.getvalue()


def run(instance: str, commands: list[str], timeout: int = 1800) -> int:
    """Run shell commands on the instance through Systems Manager; print what they print."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"commands": commands, "executionTimeout": [str(timeout)]}, fh)
    sent = json.loads(aws("ssm", "send-command", "--instance-ids", instance, "--document-name", "AWS-RunShellScript",
                          "--parameters", Path(fh.name).as_uri().replace("file:///", "file://")))
    Path(fh.name).unlink()
    command = sent["Command"]["CommandId"]
    while True:
        time.sleep(3)
        result = subprocess.run(["aws", "ssm", "get-command-invocation", "--command-id", command, "--instance-id",
                                 instance, "--output", "json", "--no-cli-pager"], capture_output=True, text=True)
        if result.returncode:
            continue                        # not registered yet
        state = json.loads(result.stdout)
        if state["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    print(state["StandardOutputContent"].rstrip())
    if state["StandardErrorContent"].strip():
        print("--- stderr ---\n" + state["StandardErrorContent"].rstrip())
    print(f"[{state['Status']}]")
    return 0 if state["Status"] == "Success" else 1


def main() -> int:
    out = outputs()
    instance = out["InstanceId"]
    if sys.argv[1:2] == ["page"]:           # only the page changed: nothing is restarted
        publish_page(out)
        print(f"published the page to {out['Url']}")
        return 0
    start(instance)
    if sys.argv[1:2] == ["run"]:
        return run(instance, sys.argv[2:])
    publish_page(out)

    key = "code/specdev.zip"
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
        fh.write(bundle())
    aws("s3", "cp", fh.name, f"s3://{out['Bucket']}/{key}")
    Path(fh.name).unlink()
    url = subprocess.run(["aws", "s3", "presign", f"s3://{out['Bucket']}/{key}", "--expires-in", "900"],
                         capture_output=True, text=True, check=True).stdout.strip()
    env = "\n".join([
        f"SPECDEV_COGNITO_POOL={out['UserPoolId']}",
        f"SPECDEV_COGNITO_CLIENT={out['UserPoolClientId']}",
        f"SPECDEV_COGNITO_DOMAIN={out['AuthDomain']}",
        f"SPECDEV_BUCKET={out['Bucket']}",
        "SPECDEV_DATA=/var/lib/specdev/data",
        "SPECDEV_ACTIVITY=/var/lib/specdev/last-active",
        f"SPECDEV_STOP_MINUTES={STOP_MINUTES}",
        f"SPECDEV_STARTING_BUDGET={STARTING_BUDGET}",
        "CLAUDE_CODE_USE_BEDROCK=1",
        "AWS_REGION=us-east-1",
        f"ANTHROPIC_MODEL={MODEL}",
        f"ANTHROPIC_SMALL_FAST_MODEL={SMALL_MODEL}",
        f"ANTHROPIC_DEFAULT_HAIKU_MODEL={SMALL_MODEL}",
        "DISABLE_AUTOUPDATER=1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        *mcp_settings(out),
    ])
    status = run(instance, [
        "set -e",
        "command -v unzip >/dev/null || (apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q unzip curl)",
        f"curl -fsSL '{url}' -o /tmp/specdev.zip",
        "rm -rf /opt/specdev/app.new && mkdir -p /opt/specdev/app.new",
        "unzip -q /tmp/specdev.zip -d /opt/specdev/app.new && rm /tmp/specdev.zip",
        f"cat >/etc/specdev.env <<'ENV'\n{env}\nENV",
        "bash /opt/specdev/app.new/deploy/setup.sh",
    ])
    if status == 0:
        print(f"\nspecdev is at {out['Url']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
