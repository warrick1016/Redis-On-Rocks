#!/usr/bin/env python3

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


TARGET_PHRASE = os.environ.get(
    "TARGET_PHRASE", "couldn't open socket: connection refused"
)
WORKFLOW_NAME = os.environ.get("TARGET_WORKFLOW_NAME", "CI")
TARGET_JOBS = ("swap", "swap-asan")


def parse_github_timestamp(value):
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1]
    if "." in value:
        base, fraction = value.split(".", 1)
        value = f"{base}.{fraction[:6].ljust(6, '0')}+00:00"
    else:
        value = f"{value}+00:00"
    return datetime.fromisoformat(value)


def parse_log_timestamp(line):
    token = line.split(" ", 1)[0]
    try:
        return parse_github_timestamp(token)
    except ValueError:
        return None


def log(message):
    print(message, flush=True)


class GitHubClient:
    def __init__(self, token, owner, repo):
        self.owner = owner
        self.repo = repo
        self.base_url = "https://api.github.com"
        self.default_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-ci-monitor-workflow",
        }

    def _request(self, method, path, payload=None):
        url = f"{self.base_url}{path}"
        headers = dict(self.default_headers)
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def get_json(self, path):
        status, headers, body = self._request("GET", path)
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("GET", path, status, body))
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def get_text(self, path):
        status, headers, body = self._request("GET", path)
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("GET", path, status, body))
        charset = "utf-8"
        content_type = headers.get("Content-Type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return body.decode(charset, errors="replace")

    def post_json(self, path, payload):
        status, headers, body = self._request("POST", path, payload=payload)
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("POST", path, status, body))
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def post_empty(self, path, payload=None):
        status, headers, body = self._request("POST", path, payload=payload)
        if status not in (200, 201, 202, 204):
            raise RuntimeError(self._format_error("POST", path, status, body))

    @staticmethod
    def _format_error(method, path, status, body):
        text = body.decode("utf-8", errors="replace") if body else ""
        return f"{method} {path} failed with HTTP {status}: {text}"


def target_job_key(job_name):
    if job_name.startswith("swap-asan"):
        return "swap-asan"
    if job_name.startswith("swap"):
        return "swap"
    return None


def find_test_step(job):
    for step in job.get("steps", []):
        if step.get("name", "").strip().lower() == "test":
            return step
    for step in job.get("steps", []):
        if "test" in step.get("name", "").strip().lower():
            return step
    return None


def extract_test_step_lines(log_text, test_step):
    start_time = parse_github_timestamp(test_step.get("started_at"))
    end_time = parse_github_timestamp(test_step.get("completed_at"))
    if not start_time or not end_time:
        return []

    result = []
    in_window = False
    for raw_line in log_text.splitlines():
        timestamp = parse_log_timestamp(raw_line)
        if timestamp is None:
            if in_window:
                result.append(raw_line)
            continue
        if timestamp < start_time:
            in_window = False
            continue
        if timestamp > end_time:
            if in_window:
                break
            continue
        in_window = True
        result.append(raw_line)
    return result


def get_pr(client, pr_number):
    return client.get_json(f"/repos/{client.owner}/{client.repo}/pulls/{pr_number}")


def get_run_jobs(client, run_id):
    query = urllib.parse.urlencode({"filter": "latest", "per_page": 100})
    data = client.get_json(
        f"/repos/{client.owner}/{client.repo}/actions/runs/{run_id}/jobs?{query}"
    )
    return data.get("jobs", [])


def find_target_run(client, head_ref, head_sha):
    query = urllib.parse.urlencode({"event": "pull_request", "branch": head_ref, "per_page": 20})
    runs = client.get_json(
        f"/repos/{client.owner}/{client.repo}/actions/runs?{query}"
    ).get("workflow_runs", [])
    candidates = [run for run in runs if run.get("head_sha") == head_sha]

    for require_ci in (True, False):
        for run in candidates:
            if require_ci and run.get("name") != WORKFLOW_NAME:
                continue
            jobs = get_run_jobs(client, run["id"])
            keys = {target_job_key(job.get("name", "")) for job in jobs}
            if set(TARGET_JOBS).issubset(keys):
                return run, jobs
    return None, []


def list_pr_comments(client, pr_number):
    comments = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        batch = client.get_json(
            f"/repos/{client.owner}/{client.repo}/issues/{pr_number}/comments?{query}"
        )
        if not batch:
            return comments
        comments.extend(batch)
        if len(batch) < 100:
            return comments
        page += 1


def existing_comment_markers(client, pr_number):
    markers = set()
    for comment in list_pr_comments(client, pr_number):
        body = comment.get("body", "")
        for line in body.splitlines():
            if line.startswith("<!-- pr-ci-monitor "):
                markers.add(line.strip())
    return markers


def build_comment(marker, job, run_id, attempt, snippet_lines):
    snippet = "\n".join(snippet_lines).replace("```", "``\\`")
    return (
        f"{marker}\n"
        f"Detected `{TARGET_PHRASE}` in the `test` step of `{job['name']}`.\n\n"
        f"- Run: `{run_id}` attempt `{attempt}`\n"
        f"- Job: {job.get('html_url', '')}\n\n"
        f"First match with the preceding 200 log lines:\n\n"
        f"```text\n{snippet}\n```"
    )


def comment_if_needed(client, pr_number, job, run_id, attempt, markers):
    if job.get("status") != "completed":
        return

    test_step = find_test_step(job)
    if not test_step or test_step.get("status") != "completed":
        log(f"Skip {job['name']}: no completed test step.")
        return

    log_text = client.get_text(
        f"/repos/{client.owner}/{client.repo}/actions/jobs/{job['id']}/logs"
    )
    test_lines = extract_test_step_lines(log_text, test_step)

    match_index = None
    for index, line in enumerate(test_lines):
        if TARGET_PHRASE in line:
            match_index = index
            break

    if match_index is None:
        log(f"No target phrase in {job['name']} for run {run_id} attempt {attempt}.")
        return

    marker = f"<!-- pr-ci-monitor run={run_id} attempt={attempt} job={job['id']} -->"
    if marker in markers:
        log(f"Comment already exists for {job['name']} in run {run_id} attempt {attempt}.")
        return

    start_index = max(0, match_index - 200)
    snippet_lines = test_lines[start_index : match_index + 1]
    comment = build_comment(marker, job, run_id, attempt, snippet_lines)
    client.post_json(
        f"/repos/{client.owner}/{client.repo}/issues/{pr_number}/comments",
        {"body": comment},
    )
    markers.add(marker)
    log(f"Posted comment for {job['name']} in run {run_id} attempt {attempt}.")


def all_jobs_completed(jobs):
    return bool(jobs) and all(job.get("status") == "completed" for job in jobs)


def main():
    token = os.environ["GITHUB_TOKEN"]
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    pr_number = int(os.environ.get("PR_NUMBER", "14"))
    client = GitHubClient(token, owner, repo)

    pr = get_pr(client, pr_number)
    if pr.get("state") != "open":
        log(f"PR #{pr_number} is {pr.get('state')}; nothing to monitor.")
        return 0

    head_ref = pr["head"]["ref"]
    head_sha = pr["head"]["sha"]
    run, jobs = find_target_run(client, head_ref, head_sha)
    if not run:
        log(f"No matching CI workflow run found for PR #{pr_number}.")
        return 0

    run_id = run["id"]
    attempt = run.get("run_attempt") or max((job.get("run_attempt", 1) for job in jobs), default=1)
    log(f"Tracking run {run_id} attempt {attempt} for PR #{pr_number}.")

    markers = existing_comment_markers(client, pr_number)
    for job in jobs:
        if target_job_key(job.get("name", "")) in TARGET_JOBS:
            comment_if_needed(client, pr_number, job, run_id, attempt, markers)

    if all_jobs_completed(jobs):
        client.post_empty(f"/repos/{client.owner}/{client.repo}/actions/runs/{run_id}/rerun")
        log(f"Triggered rerun for workflow run {run_id} attempt {attempt}.")
    else:
        status_text = ", ".join(
            f"{job['name']}={job.get('status')}/{job.get('conclusion')}"
            for job in jobs
            if target_job_key(job.get("name", "")) in TARGET_JOBS
        )
        log(f"Target jobs still running: {status_text}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
