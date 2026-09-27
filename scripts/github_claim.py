"""GitHub PR-author claim comment used by the live flows and integration test.

DevBounty's deterministic verification layer requires that the PR's own GitHub
author posts a comment of the form:

    devbounty-claim: bounty <id> payout <0x-address>

on the PR before verify_resolution can pay the registered payout address.
This module performs that author-side step against the real demo repo, using
GITHUB_TOKEN (never logged). Both the PR author account and the token belong
to the same identity here (hoveiser owns devbounty-demo's PRs).
"""

import json
import re
import urllib.request

PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def post_claim_comment(token: str, pr_url: str, bounty_id: str, payout_addr: str) -> int:
    m = PR_RE.search(pr_url)
    assert m, f"not a PR url: {pr_url}"
    owner, repo, number = m.groups()
    body = json.dumps(
        {"body": f"devbounty-claim: bounty {bounty_id} payout {payout_addr}"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "devbounty-claim",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        status = r.status
    if status != 201:
        raise RuntimeError(f"claim comment post returned HTTP {status}")
    return status
