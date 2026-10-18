"""Bring the specdev stack up to date with deploy/specdev.yaml, without ever replacing anything.

    python deploy/update_stack.py

It makes a change set (keeping every parameter, and the image the instance already runs), and
applies it only if every change is an in-place modification. If anything would be added,
removed or replaced (the instance above all, whose disk holds everyone's budgets and logs),
it prints the changes, throws the change set away and stops: nothing is changed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from deploy import STACK, aws, outputs


def cfn(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", "cloudformation", *args, "--stack-name", STACK, "--output", "json", "--no-cli-pager"],
                          capture_output=True, text=True)


def main() -> int:
    out = outputs()
    image = json.loads(aws("ec2", "describe-instances", "--instance-ids", out["InstanceId"]))
    image = image["Reservations"][0]["Instances"][0]["ImageId"]
    name = "update-" + time.strftime("%Y%m%d-%H%M%S")
    keep = [f"ParameterKey={k},UsePreviousValue=true" for k in ("Subnet", "Vpc", "CloudFrontPrefixList", "InstanceType", "Port")]
    print(f"making change set {name} (instance image {image})...")
    made = cfn("create-change-set", "--change-set-name", name, "--template-body", "file://deploy/specdev.yaml",
               "--capabilities", "CAPABILITY_IAM", "--parameters", f"ParameterKey=Image,ParameterValue={image}", *keep)
    if made.returncode:
        sys.exit(f"could not make the change set:\n{made.stderr.strip()}")
    cfn("wait", "change-set-create-complete", "--change-set-name", name)
    described = json.loads(cfn("describe-change-set", "--change-set-name", name).stdout or "{}")
    if described.get("Status") == "FAILED":
        cfn("delete-change-set", "--change-set-name", name)
        reason = described.get("StatusReason", "")
        if "didn't contain changes" in reason or "No updates" in reason:
            print("The stack is already up to date. Nothing to do.")
            return 0
        sys.exit(f"CloudFormation could not plan the update: {reason}")
    changes = [c["ResourceChange"] for c in described.get("Changes", [])]
    for c in changes:
        print(f"  {c['LogicalResourceId']:<20} {c['Action']:<8} replacement: {c.get('Replacement') or '-'}")
    unsafe = [c for c in changes if c["Action"] != "Modify" or c.get("Replacement") not in (None, "", "False")]
    if unsafe:
        cfn("delete-change-set", "--change-set-name", name)
        print("\nSTOPPED: these changes are not simple in-place updates, so nothing was applied:")
        for c in unsafe:
            print(f"  {c['LogicalResourceId']} ({c['Action']}, replacement: {c.get('Replacement')})")
        return 1
    print("\nAll changes are in-place updates. Applying...")
    applied = cfn("execute-change-set", "--change-set-name", name)
    if applied.returncode:
        sys.exit(f"could not apply the change set:\n{applied.stderr.strip()}")
    done = cfn("wait", "stack-update-complete")
    if done.returncode:
        sys.exit(f"the update did not finish cleanly:\n{done.stderr.strip()}")
    print("Stack updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
