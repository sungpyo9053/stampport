#!/usr/bin/env python3
"""Send a deploy-status SMS via AWS SNS.

Invoked from the GitHub Actions workflow:

    python scripts/notify_sms.py success
    python scripts/notify_sms.py failure

All credentials and the destination phone number come from environment
variables (which the workflow populates from GitHub Secrets):

    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION
    DEPLOY_NOTIFY_PHONE        E.164 format, e.g. +821012345678

It also reads several GITHUB_* env vars (provided automatically inside
Actions) to enrich the message body.

Exits 0 on success. On failure it prints to stderr and exits 1, but the
workflow should not let an SMS error fail the whole deploy — wrap the
step with `continue-on-error: true` if that matters.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _build_message(status: str) -> str:
    sha = os.environ.get("GITHUB_SHA", "")[:7] or "?"
    repo = os.environ.get("GITHUB_REPOSITORY", "?")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_no = os.environ.get("GITHUB_RUN_NUMBER", "?")
    actor = os.environ.get("GITHUB_ACTOR", "?")
    ref = os.environ.get("GITHUB_REF_NAME", "?")
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")

    if status == "success":
        body = (
            f"[Stampport] 배포 성공 #{run_no}\n"
            f"브랜치 {ref} · 커밋 {sha}\n"
            f"by {actor} · {ts}\n"
            f"확인: https://reviewdr.kr/stampport/"
        )
    else:
        run_url = (
            f"https://github.com/{repo}/actions/runs/{run_id}"
            if run_id
            else "(actions URL unavailable)"
        )
        body = (
            f"[Stampport] 배포 실패 #{run_no}\n"
            f"브랜치 {ref} · 커밋 {sha}\n"
            f"by {actor} · {ts}\n"
            f"로그: {run_url}"
        )
    # SNS SMS hard-caps a single segment at 140 bytes; multipart messages
    # cost more. Keep it tight but don't truncate the URL.
    return body


def main(argv: list[str]) -> int:
    status = (argv[1] if len(argv) > 1 else "").strip().lower()
    if status not in {"success", "failure"}:
        print(
            "usage: notify_sms.py {success|failure}",
            file=sys.stderr,
        )
        return 2

    phone = os.environ.get("DEPLOY_NOTIFY_PHONE", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    if not phone:
        print("DEPLOY_NOTIFY_PHONE is empty — skipping SMS", file=sys.stderr)
        return 1
    if not region:
        print("AWS_REGION is empty — skipping SMS", file=sys.stderr)
        return 1

    try:
        import boto3  # type: ignore
    except ImportError:
        print("boto3 not installed — skipping SMS", file=sys.stderr)
        return 1

    msg = _build_message(status)
    client = boto3.client("sns", region_name=region)
    resp = client.publish(
        PhoneNumber=phone,
        Message=msg,
        MessageAttributes={
            # Transactional gives the message higher delivery priority,
            # which is what we want for an ops alert.
            "AWS.SNS.SMS.SMSType": {
                "DataType": "String",
                "StringValue": "Transactional",
            },
        },
    )
    msg_id = resp.get("MessageId", "?")
    print(f"SNS published — status={status} message_id={msg_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
