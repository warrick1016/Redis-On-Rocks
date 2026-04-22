#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


TARGET_PHRASE = os.environ.get(
    "TARGET_PHRASE", "couldn't open socket: connection refused"
)
WORKFLOW_NAME = os.environ.get("TARGET_WORKFLOW_NAME", "CI")
TARGET_WORKFLOW_FILE = os.environ.get("TARGET_WORKFLOW_FILE", ".github/workflows/ci.yml")
MIN_ROUNDS = int(os.environ.get("MIN_ROUNDS", "60"))
TARGET_PR_NUMBER = int(os.environ.get("TARGET_PR_NUMBER", "14"))
SESSION_ID = os.environ["MONITOR_SESSION_ID"]
TRIGGER_DESCRIPTION = os.environ.get(
    "MONITOR_TRIGGER_DESCRIPTION",
    "triggered on CI workflow completion plus manual workflow_dispatch",
)
DRY_RUN = os.environ.get("DRY_RUN") == "1"
JOB_SELECTION_MODE = os.environ.get("JOB_SELECTION_MODE", "prefix")
TARGET_JOB_PREFIXES = tuple(
    value.strip()
    for value in os.environ.get("TARGET_JOB_PREFIXES", "swap,swap-asan").split(",")
    if value.strip()
)
RESULT_POLICY = os.environ.get("RESULT_POLICY", "match_only")
RESULT_COMMENT_MODE = os.environ.get("RESULT_COMMENT_MODE", "attempt")
SNIPPET_LINE_COUNT = os.environ.get("SNIPPET_LINE_COUNT", "200")
MATCH_PRECEDING_LINE_COUNT = int(
    os.environ.get("MATCH_PRECEDING_LINE_COUNT", SNIPPET_LINE_COUNT)
)
TAIL_SNIPPET_LINE_COUNT = int(
    os.environ.get("TAIL_SNIPPET_LINE_COUNT", SNIPPET_LINE_COUNT)
)
SNIPPET_FALLBACK = os.environ.get("SNIPPET_FALLBACK", "none")
MATCH_LINE_POLICY = os.environ.get("MATCH_LINE_POLICY", "include")
RERUN_RETRY_AFTER_MINUTES = int(os.environ.get("RERUN_RETRY_AFTER_MINUTES", "10"))
ENABLE_SELF_DISPATCH_BACKSTOP = os.environ.get("ENABLE_SELF_DISPATCH_BACKSTOP") == "1"
FOLLOW_UP_POLL_SECONDS = int(os.environ.get("FOLLOW_UP_POLL_SECONDS", "300"))
MONITOR_WORKFLOW_FILE = os.environ.get("MONITOR_WORKFLOW_FILE", "")
MONITOR_WORKFLOW_REF = os.environ.get(
    "MONITOR_WORKFLOW_REF", os.environ.get("GITHUB_REF_NAME", "")
)
PASSIVE_COMPLETION_MONITOR = os.environ.get("PASSIVE_COMPLETION_MONITOR") == "1"
PASSIVE_POLL_INTERVAL_MINUTES = int(
    os.environ.get("PASSIVE_POLL_INTERVAL_MINUTES", "20")
)
ACTIVE_TRIGGER_IF_IDLE = os.environ.get("ACTIVE_TRIGGER_IF_IDLE") == "1"
ACTIVE_TRIGGER_POLL_INTERVAL_MINUTES = int(
    os.environ.get("ACTIVE_TRIGGER_POLL_INTERVAL_MINUTES", "30")
)
RECENT_COMPLETED_BACKFILL_MINUTES = int(
    os.environ.get("RECENT_COMPLETED_BACKFILL_MINUTES", "0")
)
ENABLE_CI_WORKFLOW_DISPATCH_RECOVERY = (
    os.environ.get("ENABLE_CI_WORKFLOW_DISPATCH_RECOVERY") == "1"
)
CI_WORKFLOW_FILE = os.environ.get("CI_WORKFLOW_FILE", TARGET_WORKFLOW_FILE)
CI_WORKFLOW_REF = os.environ.get("CI_WORKFLOW_REF", MONITOR_WORKFLOW_REF)
CI_DISPATCH_RETRY_AFTER_SECONDS = int(
    os.environ.get("CI_DISPATCH_RETRY_AFTER_SECONDS", "300")
)
CI_DISPATCH_WAIT_TIMEOUT_SECONDS = int(
    os.environ.get("CI_DISPATCH_WAIT_TIMEOUT_SECONDS", "120")
)
CI_DISPATCH_WAIT_POLL_SECONDS = int(
    os.environ.get("CI_DISPATCH_WAIT_POLL_SECONDS", "5")
)
ENABLE_PR_REOPEN_RECOVERY = os.environ.get("ENABLE_PR_REOPEN_RECOVERY") == "1"
PR_REOPEN_RETRY_AFTER_SECONDS = int(os.environ.get("PR_REOPEN_RETRY_AFTER_SECONDS", "1800"))
PR_REOPEN_WAIT_TIMEOUT_SECONDS = int(os.environ.get("PR_REOPEN_WAIT_TIMEOUT_SECONDS", "120"))
PR_REOPEN_WAIT_POLL_SECONDS = int(os.environ.get("PR_REOPEN_WAIT_POLL_SECONDS", "5"))
NON_FAILURE_CONCLUSIONS = {"success", "skipped", "neutral"}


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


def current_github_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_marker():
    return f"<!-- pr-ci-monitor-state session={SESSION_ID} -->"


def run_attempt_key(run_id, attempt):
    return f"{int(run_id)}:{int(attempt)}"


def run_attempt_sort_key(value):
    run_id_text, _, attempt_text = str(value).partition(":")
    try:
        run_id = int(run_id_text)
    except ValueError:
        run_id = 0
    try:
        attempt = int(attempt_text)
    except ValueError:
        attempt = 0
    return (run_id, attempt)


def result_marker(run_id, attempt, job_id=None):
    marker = f"<!-- pr-ci-monitor-result session={SESSION_ID} run={run_id} attempt={attempt}"
    if job_id is not None:
        marker += f" job={job_id}"
    return marker + " -->"


def job_name_key(job_name):
    return job_name.split(" (", 1)[0].strip()


def find_job_by_prefix(jobs, prefix):
    for job in jobs:
        if job_name_key(job.get("name", "")) == prefix:
            return job
    return None


def find_job_by_id(jobs, job_id):
    for job in jobs:
        if job.get("id") == job_id:
            return job
    return None


def is_failed_job(job):
    if job.get("status") != "completed":
        return False
    return job.get("conclusion") not in NON_FAILURE_CONCLUSIONS


def select_result_jobs(jobs):
    if JOB_SELECTION_MODE == "prefix":
        return [
            job
            for job in (find_job_by_prefix(jobs, prefix) for prefix in TARGET_JOB_PREFIXES)
            if job is not None and job.get("status") == "completed"
        ]
    if JOB_SELECTION_MODE == "failed":
        return [job for job in jobs if is_failed_job(job)]
    raise RuntimeError(f"Unsupported JOB_SELECTION_MODE `{JOB_SELECTION_MODE}`.")


def build_state_jobs(jobs, run):
    if JOB_SELECTION_MODE == "prefix":
        return [{"label": prefix, "job": find_job_by_prefix(jobs, prefix)} for prefix in TARGET_JOB_PREFIXES]
    if JOB_SELECTION_MODE == "failed":
        selected = [job for job in jobs if is_failed_job(job)] if run and run.get("status") == "completed" else jobs
        if not selected:
            return [{"label": "failed jobs", "job": None, "message": "none"}]
        return [{"label": job.get("name", "job"), "job": job} for job in selected]
    raise RuntimeError(f"Unsupported JOB_SELECTION_MODE `{JOB_SELECTION_MODE}`.")


def sanitize_block(text):
    return text.replace("```", "``\\`")


def job_sort_key(job):
    return (
        parse_github_timestamp(job.get("created_at"))
        or parse_github_timestamp(job.get("started_at"))
        or parse_github_timestamp(job.get("completed_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        job.get("id", 0),
    )


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

    def _request(self, method, path, payload=None, follow_redirects=True, use_auth=True):
        url = f"{self.base_url}{path}" if not path.startswith("http") else path
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.default_headers["User-Agent"],
        }
        if use_auth:
            headers.update(self.default_headers)
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if follow_redirects:
            opener = urllib.request.build_opener()
        else:
            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirectHandler)
        try:
            with opener.open(request) as response:
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

    def post_json(self, path, payload):
        if DRY_RUN:
            log(f"[dry-run] POST {path} {json.dumps(payload, ensure_ascii=False)}")
            return {}
        status, headers, body = self._request("POST", path, payload=payload)
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("POST", path, status, body))
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def patch_json(self, path, payload):
        if DRY_RUN:
            log(f"[dry-run] PATCH {path} {json.dumps(payload, ensure_ascii=False)}")
            return {}
        status, headers, body = self._request("PATCH", path, payload=payload)
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("PATCH", path, status, body))
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def post_empty(self, path, payload=None):
        if DRY_RUN:
            log(f"[dry-run] POST {path} {json.dumps(payload, ensure_ascii=False) if payload else ''}")
            return
        status, headers, body = self._request("POST", path, payload=payload)
        if status not in (200, 201, 202, 204):
            raise RuntimeError(self._format_error("POST", path, status, body))

    def get_workflow_run(self, run_id):
        return self.get_json(f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}")

    def get_workflow_job(self, job_id):
        return self.get_json(f"/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}")

    def cancel_workflow(self, run_id):
        self.post_empty(f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/cancel")

    def get_run_jobs(self, run_id, filter_mode="latest"):
        query = urllib.parse.urlencode({"filter": filter_mode, "per_page": 100})
        data = self.get_json(
            f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs?{query}"
        )
        return sorted(data.get("jobs", []), key=job_sort_key)

    def get_run_attempt_jobs(self, run_id, attempt_number, filter_mode="latest"):
        query = urllib.parse.urlencode({"filter": filter_mode, "per_page": 100})
        data = self.get_json(
            f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs?{query}"
        )
        return sorted(data.get("jobs", []), key=job_sort_key)

    def list_pr_comments(self, pr_number):
        comments = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": 100, "page": page})
            batch = self.get_json(
                f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments?{query}"
            )
            if not batch:
                return comments
            comments.extend(batch)
            if len(batch) < 100:
                return comments
            page += 1

    def get_pr(self, pr_number):
        return self.get_json(f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")

    def download_job_logs(self, job_id):
        path = f"/repos/{self.owner}/{self.repo}/actions/jobs/{job_id}/logs"
        status, headers, body = self._request("GET", path, follow_redirects=False)
        if status not in (301, 302, 303, 307, 308):
            raise RuntimeError(self._format_error("GET", path, status, body))
        download_url = headers.get("Location")
        if not download_url:
            raise RuntimeError(f"GET {path} returned {status} without a Location header")
        status, headers, body = self._request(
            "GET", download_url, follow_redirects=True, use_auth=False
        )
        if status < 200 or status >= 300:
            raise RuntimeError(self._format_error("GET", download_url, status, body))
        charset = "utf-8"
        content_type = headers.get("Content-Type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return body.decode(charset, errors="replace")

    def rerun_workflow(self, run_id):
        self.post_empty(
            f"/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/rerun",
            {"enable_debug_logging": False},
        )

    def dispatch_workflow(self, workflow_file, ref, inputs):
        identifiers = [workflow_file]
        workflow_basename = os.path.basename(workflow_file)
        if workflow_basename and workflow_basename != workflow_file:
            identifiers.append(workflow_basename)

        errors = []
        for index, identifier in enumerate(identifiers):
            workflow_id = urllib.parse.quote(identifier, safe="")
            try:
                self.post_empty(
                    f"/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_id}/dispatches",
                    {"ref": ref, "inputs": inputs},
                )
                return identifier
            except RuntimeError as exc:
                message = str(exc)
                errors.append(f"`{identifier}`: {message}")
                should_retry_with_basename = (
                    index + 1 < len(identifiers)
                    and "does not have 'workflow_dispatch' trigger" in message
                )
                if should_retry_with_basename:
                    log(
                        f"Dispatch via workflow identifier `{identifier}` failed; "
                        f"retrying with `{identifiers[index + 1]}`."
                    )
                    continue
                if len(errors) == 1:
                    raise
                raise RuntimeError(" ; ".join(errors))

        raise RuntimeError("; ".join(errors))

    def update_pull_request_state(self, pr_number, state):
        self.patch_json(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}",
            {"state": state},
        )

    @staticmethod
    def _format_error(method, path, status, body):
        text = body.decode("utf-8", errors="replace") if body else ""
        return f"{method} {path} failed with HTTP {status}: {text}"


def parse_event():
    with open(os.environ["GITHUB_EVENT_PATH"], "r", encoding="utf-8") as handle:
        return json.load(handle)


def find_state_comment(comments):
    marker = state_marker()
    for comment in comments:
        if marker in comment.get("body", ""):
            return comment
    return None


def parse_state(comment):
    default = {
        "session_id": SESSION_ID,
        "target_pr_number": TARGET_PR_NUMBER,
        "completed_rounds": 0,
        "processed_run_attempts": [],
        "last_action": "initialized",
        "last_processed_attempt": None,
        "last_processed_run_id": None,
        "last_rerun_request_run_attempt": None,
        "last_rerun_request_at": None,
        "last_follow_up_dispatch_at": None,
        "last_follow_up_dispatch_reason": None,
        "last_follow_up_dispatch_target": None,
        "last_pr_reopen_run_attempt": None,
        "last_pr_reopen_at": None,
        "last_ci_dispatch_head_sha": None,
        "last_ci_dispatch_at": None,
    }
    if not comment:
        return default

    body = comment.get("body", "")
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    if not match:
        return default

    try:
        loaded = json.loads(match.group(1))
    except json.JSONDecodeError:
        return default

    default.update(loaded)
    if default.get("session_id") != SESSION_ID:
        return default

    processed_run_attempts = set()
    for value in default.get("processed_run_attempts", []):
        if isinstance(value, str) and ":" in value:
            processed_run_attempts.add(value)

    legacy_run_id = default.get("tracked_run_id")
    for value in default.get("processed_attempts", []):
        try:
            attempt = int(value)
        except (TypeError, ValueError):
            continue
        if legacy_run_id is None:
            continue
        processed_run_attempts.add(run_attempt_key(legacy_run_id, attempt))

    default["processed_run_attempts"] = sorted(
        processed_run_attempts,
        key=run_attempt_sort_key,
    )
    default.pop("processed_attempts", None)
    default.pop("tracked_run_id", None)
    default.pop("tracked_head_sha", None)
    return default


def progress_summary(state):
    completed = int(state.get("completed_rounds", 0))
    if PASSIVE_COMPLETION_MONITOR:
        return f"recorded `{completed}` completed CI attempt(s) in this session"
    return f"`{completed}/{MIN_ROUNDS}`"


def active_polling_mode_enabled():
    return PASSIVE_COMPLETION_MONITOR and ACTIVE_TRIGGER_IF_IDLE


def current_event_supports_active_trigger():
    return os.environ["GITHUB_EVENT_NAME"] in ("push", "schedule", "workflow_dispatch")


def monitoring_mode_summary_line():
    if active_polling_mode_enabled():
        return (
            "- Active polling mode: completed CI attempts are still recorded with per-job "
            "snippets, and poll runs keep the latest PR head covered by rerunning an idle "
            "current-head CI attempt or bootstrapping one if that head has no CI run yet."
        )
    if PASSIVE_COMPLETION_MONITOR:
        return (
            "- Passive mode: enabled; this monitor only records newly completed CI attempts "
            "and never triggers reruns or recovery workflows."
        )
    return "- This session follows PR-level CI activity across reruns and new commits."


def monitoring_mode_detail_line(state):
    if active_polling_mode_enabled():
        return (
            f"- Progress: {progress_summary(state)}; scheduled checks run about every "
            f"{ACTIVE_TRIGGER_POLL_INTERVAL_MINUTES} minute(s)."
        )
    if PASSIVE_COMPLETION_MONITOR:
        return (
            "- This session passively records newly completed PR-level CI attempts across "
            "reruns and new commits."
        )
    return f"- Progress: {progress_summary(state)}"


def passive_monitoring_note():
    return (
        "Passive monitoring mode is enabled; no CI rerun, workflow_dispatch recovery, "
        "PR reopen recovery, monitor self-dispatch, or CI cancellation was requested."
    )


def active_polling_note():
    return (
        "Active polling mode is enabled; completed CI runs still post per-job snippets, "
        "and scheduled/manual polls keep the latest PR head covered by rerunning an idle "
        "current-head CI attempt or bootstrapping one via workflow_dispatch when that head "
        "has no CI run yet."
    )


def passive_polling_wait_note():
    return (
        f"Passive monitoring mode is enabled; the next scheduled check runs about every "
        f"{PASSIVE_POLL_INTERVAL_MINUTES} minute(s)."
    )


def active_polling_wait_note():
    return (
        f"Active polling mode is enabled; the next scheduled check runs about every "
        f"{ACTIVE_TRIGGER_POLL_INTERVAL_MINUTES} minute(s) and will bootstrap the latest "
        "PR head if it still has no CI run."
    )


def wait_note():
    if active_polling_mode_enabled():
        return active_polling_wait_note()
    return passive_polling_wait_note()


def maybe_trigger_idle_ci(client, state, run):
    if not active_polling_mode_enabled():
        return False, "Active polling mode is disabled."
    if not current_event_supports_active_trigger():
        return False, "This event only records results; active triggering waits for the next poll."

    attempt = int(run.get("run_attempt", 1))
    run_attempt = run_attempt_key(run["id"], attempt)
    retry_needed, retry_reason = should_retry_processed_attempt(state, run_attempt)
    if not retry_needed:
        return False, f"{retry_reason} {active_polling_wait_note()}"

    client.rerun_workflow(run["id"])
    remember_rerun_request(state, run_attempt)
    return True, (
        f"{retry_reason} Requested another full CI rerun because no CI workflow is "
        "currently running for this PR head."
    )


def maybe_bootstrap_latest_head_ci(client, pr, state):
    if not active_polling_mode_enabled():
        return False, "Active polling mode is disabled.", None
    if not current_event_supports_active_trigger():
        return False, "This event only records results; active bootstrapping waits for the next poll.", None

    dispatch_needed, dispatch_reason = should_dispatch_ci_recovery(state, pr)
    if not dispatch_needed:
        return False, f"{dispatch_reason} {active_polling_wait_note()}", None

    fresh_run = dispatch_ci_workflow_for_recovery(client, pr, state)
    bootstrap_note = (
        f"{dispatch_reason} Requested a CI workflow_dispatch bootstrap for current PR head "
        f"`{pr['head']['sha'][:7]}`."
    )
    if fresh_run:
        return True, (
            f"{bootstrap_note} Observed CI run `{fresh_run['id']}` attempt "
            f"`{fresh_run.get('run_attempt', 1)}` with status "
            f"`{fresh_run.get('status')}`."
        ), fresh_run

    return True, (
        f"{bootstrap_note} No dispatched CI run appeared within "
        f"{CI_DISPATCH_WAIT_TIMEOUT_SECONDS} second(s). {active_polling_wait_note()}"
    ), None


def upsert_state_comment(client, pr_number, comment, state, run, state_jobs, note):
    body = build_state_comment_body(pr_number, state, run, state_jobs, note)
    if comment:
        client.patch_json(
            f"/repos/{client.owner}/{client.repo}/issues/comments/{comment['id']}",
            {"body": body},
        )
    else:
        client.post_json(
            f"/repos/{client.owner}/{client.repo}/issues/{pr_number}/comments",
            {"body": body},
        )


def build_state_comment_body(pr_number, state, run, state_jobs, note):
    latest_statuses = []
    for entry in state_jobs:
        label = entry.get("label", "job")
        job = entry.get("job")
        if job:
            prefix = f"`{label}`: " if label != job.get("name", "") else ""
            latest_statuses.append(
                f"- {prefix}[{job['name']} #{job['id']}]({job.get('html_url', '')}) — "
                f"`{job.get('status')}/{job.get('conclusion')}`"
            )
        else:
            latest_statuses.append(f"- `{label}`: {entry.get('message', 'not found')}")

    payload = json.dumps(state, indent=2, sort_keys=True)
    lines = [
        state_marker(),
        "## PR CI Monitor state",
        "",
        f"- PR: #{pr_number}",
        f"- Session: `{SESSION_ID}`",
        f"- Trigger: {TRIGGER_DESCRIPTION}",
        "- This workflow is **not** long-running; GitHub starts a fresh monitor run on each trigger, then exits.",
        monitoring_mode_summary_line(),
        monitoring_mode_detail_line(state),
    ]
    if PASSIVE_COMPLETION_MONITOR and not active_polling_mode_enabled():
        lines.append(f"- Progress: {progress_summary(state)}")
    lines.extend(
        [
        f"- Latest action: {state.get('last_action', 'n/a')}",
        f"- Note: {note}",
        ]
    )

    if run:
        lines.append(
            f"- Latest CI event: [CI #{run.get('run_number', '?')} / attempt {run.get('run_attempt', '?')}]"
            f"({run.get('html_url', '')})"
        )
    lines.extend(["", "### Latest monitored jobs", ""])
    lines.extend(latest_statuses)
    lines.extend(["", "```json", payload, "```"])
    return "\n".join(lines)


def existing_result_markers(comments):
    markers = set()
    prefix = f"<!-- pr-ci-monitor-result session={SESSION_ID} "
    for comment in comments:
        for line in comment.get("body", "").splitlines():
            if line.startswith(prefix):
                markers.add(line.strip())
    return markers


def workflow_dispatch_title_prefix():
    return f"PR #{TARGET_PR_NUMBER} "


def workflow_dispatch_title(pr):
    return f"{workflow_dispatch_title_prefix()}{pr['head']['sha']}"


def run_matches_target_pr(run, pr):
    if run.get("name") != WORKFLOW_NAME:
        return False
    if any(pr_info.get("number") == TARGET_PR_NUMBER for pr_info in run.get("pull_requests", [])):
        return True
    if run.get("event") == "workflow_dispatch":
        return run.get("display_title", "").startswith(workflow_dispatch_title_prefix())
    return False


def run_matches_current_pr_head(run, pr):
    if not run_matches_target_pr(run, pr):
        return False
    if run.get("event") == "workflow_dispatch":
        return run.get("display_title", "") == workflow_dispatch_title(pr)
    return run.get("head_sha") == pr["head"]["sha"]


def find_recent_ci_runs_for_pr(client, pr):
    workflow_id = urllib.parse.quote(TARGET_WORKFLOW_FILE, safe="")
    query = urllib.parse.urlencode({"per_page": 100})
    runs = client.get_json(
        f"/repos/{client.owner}/{client.repo}/actions/workflows/{workflow_id}/runs?{query}"
    ).get("workflow_runs", [])
    candidates = [run for run in runs if run_matches_target_pr(run, pr)]
    candidates.sort(
        key=lambda run: (
            parse_github_timestamp(run.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            run.get("id", 0),
        )
    )
    return candidates


def find_latest_ci_run_for_pr(client, pr):
    runs = [run for run in find_recent_ci_runs_for_pr(client, pr) if run_matches_current_pr_head(run, pr)]
    return runs[-1] if runs else None


def load_jobs_for_run_attempt(client, run_id, attempt):
    return client.get_run_attempt_jobs(run_id, attempt, filter_mode="latest")


def backfill_sort_key(run):
    return (
        parse_github_timestamp(run.get("updated_at"))
        or parse_github_timestamp(run.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        run.get("id", 0),
    )


def find_recent_completed_backfill_candidates(client, pr, state):
    if RECENT_COMPLETED_BACKFILL_MINUTES <= 0:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=RECENT_COMPLETED_BACKFILL_MINUTES
    )
    processed_run_attempts = set(state.get("processed_run_attempts", []))
    candidates = []
    for run in find_recent_ci_runs_for_pr(client, pr):
        if run.get("status") != "completed":
            continue
        completed_at = parse_github_timestamp(run.get("updated_at"))
        if not completed_at or completed_at < cutoff:
            continue

        attempt = int(run.get("run_attempt", 1))
        if run_attempt_key(run["id"], attempt) in processed_run_attempts:
            continue
        candidates.append(run)

    return sorted(candidates, key=backfill_sort_key)


def backfill_recent_completed_runs(client, pr, state, state_comment, comments):
    current_state_comment = state_comment
    current_comments = comments
    backfilled = 0

    for run in find_recent_completed_backfill_candidates(client, pr, state):
        attempt = int(run.get("run_attempt", 1))
        jobs = load_jobs_for_run_attempt(client, run["id"], attempt)
        processed = process_completed_runs_for_pr(
            client,
            run,
            state,
            current_state_comment,
            current_comments,
            attempt=attempt,
            jobs=jobs,
            action_label="backfilled recent completed CI run",
            note_prefix=(
                f"Backfilled an unrecorded completed CI run from the past "
                f"{RECENT_COMPLETED_BACKFILL_MINUTES} minute(s)."
            ),
            allow_active_trigger=False,
        )
        if not processed:
            continue

        backfilled += 1
        current_comments = client.list_pr_comments(TARGET_PR_NUMBER)
        current_state_comment = find_state_comment(current_comments)

    return backfilled, current_state_comment, current_comments


def cancel_outdated_ci_runs(client, pr, latest_run):
    cancelled = []
    for run in find_recent_ci_runs_for_pr(client, pr):
        if run.get("status") == "completed":
            continue
        if latest_run is not None and run.get("id") == latest_run.get("id"):
            continue
        try:
            client.cancel_workflow(run["id"])
        except RuntimeError as exc:
            log(f"Failed to cancel outdated CI run {run['id']}: {exc}")
            continue
        cancelled.append(run)
        log(f"Cancelled outdated CI run {run['id']} for PR #{TARGET_PR_NUMBER}.")
    return cancelled


def resolve_target_context(client, event, pr):
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name == "workflow_run":
        workflow_run = event.get("workflow_run", {})
        if workflow_run.get("name") != WORKFLOW_NAME:
            return None, None, "Ignored non-CI workflow_run event."
        if not workflow_run.get("id"):
            return None, None, "Ignored workflow_run event without a run id."
        run = client.get_workflow_run(workflow_run["id"])
        for key in (
            "status",
            "conclusion",
            "run_attempt",
            "html_url",
            "created_at",
            "updated_at",
            "pull_requests",
            "head_sha",
            "head_branch",
            "display_title",
            "event",
            "name",
            "run_number",
        ):
            if key in workflow_run and workflow_run.get(key) is not None:
                run[key] = workflow_run.get(key)
        if not run_matches_target_pr(run, pr):
            return None, None, f"Ignored CI completion unrelated to PR #{TARGET_PR_NUMBER}."
        return run, None, None
    if event_name == "workflow_job":
        workflow_job = event.get("workflow_job", {})
        if workflow_job.get("workflow_name") and workflow_job.get("workflow_name") != WORKFLOW_NAME:
            return None, None, "Ignored non-CI workflow_job event."
        if workflow_job.get("action") and workflow_job.get("action") != "completed":
            return None, None, "Ignored non-completed workflow_job event."
        if not workflow_job.get("id"):
            return None, None, "Ignored workflow_job event without a job id."
        job = client.get_workflow_job(workflow_job["id"])
        run = client.get_workflow_run(job["run_id"])
        if run.get("name") != WORKFLOW_NAME:
            return None, None, "Ignored non-CI workflow_job event."
        if not run_matches_target_pr(run, pr):
            return None, None, f"Ignored completed job unrelated to PR #{TARGET_PR_NUMBER}."
        return run, job, None
    if event_name in ("workflow_dispatch", "schedule", "push"):
        run = find_latest_ci_run_for_pr(client, pr)
        if not run:
            return None, None, f"No matching CI workflow run found for PR #{TARGET_PR_NUMBER}."
        return run, None, None
    return None, None, f"Unsupported event `{event_name}`."


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
    end_time_exclusive = end_time + timedelta(seconds=1)

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
        if timestamp >= end_time_exclusive:
            if in_window:
                break
            continue
        if in_window and "Post job cleanup." in raw_line:
            break
        in_window = True
        result.append(raw_line)
    return result


def collect_job_excerpt(client, job):
    test_step = find_test_step(job)
    if not test_step:
        if RESULT_POLICY == "always":
            return {
                "job": job,
                "note": "No completed `test` step was found in this job.",
                "snippet": None,
            }
        return None
    if test_step.get("status") != "completed":
        if RESULT_POLICY == "always":
            return {
                "job": job,
                "note": f"The `test` step did not complete (status: `{test_step.get('status')}`).",
                "snippet": None,
            }
        return None

    log_text = client.download_job_logs(job["id"])
    test_lines = extract_test_step_lines(log_text, test_step)
    if not test_lines:
        if RESULT_POLICY == "always":
            return {
                "job": job,
                "note": "The completed `test` step did not produce any captured output.",
                "snippet": None,
            }
        return None

    match_index = None
    for index, line in enumerate(test_lines):
        if TARGET_PHRASE in line:
            match_index = index
            break

    if match_index is not None:
        start_index = max(0, match_index - MATCH_PRECEDING_LINE_COUNT)
        end_index = match_index + (1 if MATCH_LINE_POLICY == "include" else 0)
        snippet_lines = test_lines[start_index:end_index]
        if MATCH_LINE_POLICY == "include":
            note = f"Captured the first `{TARGET_PHRASE}` line with its preceding {MATCH_PRECEDING_LINE_COUNT} lines."
        elif snippet_lines:
            note = f"Captured the {len(snippet_lines)} line(s) immediately before the first `{TARGET_PHRASE}`."
        else:
            note = f"Found `{TARGET_PHRASE}` at the start of the test output, so no preceding lines were available."
        return {
            "job": job,
            "note": note,
            "snippet": "\n".join(snippet_lines) if snippet_lines else None,
        }

    if SNIPPET_FALLBACK == "tail":
        snippet_lines = test_lines[-TAIL_SNIPPET_LINE_COUNT:]
        return {
            "job": job,
            "note": f"Captured the last {len(snippet_lines)} line(s) of the completed `test` step output.",
            "snippet": "\n".join(snippet_lines),
        }

    if RESULT_POLICY == "always":
        return {
            "job": job,
            "note": f"`{TARGET_PHRASE}` was not found, and no fallback snippet was configured.",
            "snippet": None,
        }

    return None


def result_summary(round_number):
    return (
        f"Detected `{TARGET_PHRASE}` in CI round `{round_number}/{MIN_ROUNDS}`."
        if RESULT_POLICY == "match_only"
        else f"Captured failed job test output for CI round `{round_number}/{MIN_ROUNDS}`."
    )


def post_job_result_comments_if_needed(client, pr_number, run, round_number, jobs, comments):
    markers = existing_result_markers(comments)
    selected_jobs = select_result_jobs(jobs)
    if not selected_jobs:
        log(f"No matching jobs to report for run {run['id']} attempt {run.get('run_attempt', 1)}.")
        return False

    posted = False
    for job in selected_jobs:
        marker = result_marker(run["id"], run.get("run_attempt", 1), job.get("id"))
        if marker in markers:
            log(
                f"Result comment already exists for run {run['id']} attempt {run.get('run_attempt', 1)} "
                f"job {job.get('id')}."
            )
            continue

        excerpt = collect_job_excerpt(client, job)
        if not excerpt:
            continue

        lines = [
            marker,
            result_summary(round_number),
            "",
            f"- Session: `{SESSION_ID}`",
            f"- Workflow run: [CI #{run.get('run_number', '?')} / attempt {run.get('run_attempt', '?')}]({run.get('html_url', '')})",
            "",
            f"### {job['name']}",
            "",
            f"- Job: {job.get('html_url', '')}",
            f"- Conclusion: `{job.get('conclusion')}`",
            f"- Capture: {excerpt.get('note', 'n/a')}",
            "",
        ]
        if excerpt.get("snippet"):
            lines.extend(
                [
                    "```text",
                    sanitize_block(excerpt["snippet"]),
                    "```",
                    "",
                ]
            )

        client.post_json(
            f"/repos/{client.owner}/{client.repo}/issues/{pr_number}/comments",
            {"body": "\n".join(lines).rstrip()},
        )
        markers.add(marker)
        posted = True
        log(
            f"Posted result comment for run {run['id']} attempt {run.get('run_attempt', 1)} "
            f"job {job.get('id')}."
        )
    return posted


def post_attempt_result_comment_if_needed(client, pr_number, run, round_number, jobs, comments):
    marker = result_marker(run["id"], run.get("run_attempt", 1))
    if marker in existing_result_markers(comments):
        log(f"Result comment already exists for run {run['id']} attempt {run.get('run_attempt', 1)}.")
        return False

    selected_jobs = select_result_jobs(jobs)
    if not selected_jobs:
        log(f"No matching jobs to report for run {run['id']} attempt {run.get('run_attempt', 1)}.")
        return False

    results = []
    for job in selected_jobs:
        excerpt = collect_job_excerpt(client, job)
        if excerpt:
            results.append(excerpt)

    if not results:
        log(f"No result snippets collected for run {run['id']} attempt {run.get('run_attempt', 1)}.")
        return False

    lines = [
        marker,
        result_summary(round_number),
        "",
        f"- Session: `{SESSION_ID}`",
        f"- Workflow run: [CI #{run.get('run_number', '?')} / attempt {run.get('run_attempt', '?')}]({run.get('html_url', '')})",
        "",
    ]

    for result in results:
        job = result["job"]
        lines.extend(
            [
                f"### {job['name']}",
                "",
                f"- Job: {job.get('html_url', '')}",
                f"- Conclusion: `{job.get('conclusion')}`",
                f"- Capture: {result.get('note', 'n/a')}",
                "",
            ]
        )
        if result.get("snippet"):
            lines.extend(
                [
                    "```text",
                    sanitize_block(result["snippet"]),
                    "```",
                    "",
                ]
            )

    client.post_json(
        f"/repos/{client.owner}/{client.repo}/issues/{pr_number}/comments",
        {"body": "\n".join(lines).rstrip()},
    )
    log(f"Posted result comment for run {run['id']} attempt {run.get('run_attempt', 1)}.")
    return True


def post_result_comment_if_needed(client, pr_number, run, round_number, jobs, comments):
    if RESULT_COMMENT_MODE == "job":
        return post_job_result_comments_if_needed(client, pr_number, run, round_number, jobs, comments)
    if RESULT_COMMENT_MODE == "attempt":
        return post_attempt_result_comment_if_needed(client, pr_number, run, round_number, jobs, comments)
    raise RuntimeError(f"Unsupported RESULT_COMMENT_MODE `{RESULT_COMMENT_MODE}`.")


def remember_rerun_request(state, run_attempt):
    state["last_rerun_request_run_attempt"] = run_attempt
    state["last_rerun_request_at"] = current_github_timestamp()


def remember_follow_up_dispatch(state, target, reason):
    state["last_follow_up_dispatch_at"] = current_github_timestamp()
    state["last_follow_up_dispatch_target"] = target
    state["last_follow_up_dispatch_reason"] = reason


def remember_pr_reopen(state, run_attempt):
    state["last_pr_reopen_run_attempt"] = run_attempt
    state["last_pr_reopen_at"] = current_github_timestamp()


def remember_ci_dispatch(state, head_sha):
    state["last_ci_dispatch_head_sha"] = head_sha
    state["last_ci_dispatch_at"] = current_github_timestamp()


def should_dispatch_follow_up(state, target):
    if not ENABLE_SELF_DISPATCH_BACKSTOP:
        return False, "Self-dispatch backstop is disabled."
    if not MONITOR_WORKFLOW_FILE or not MONITOR_WORKFLOW_REF:
        return False, "Self-dispatch backstop is not fully configured."

    last_target = state.get("last_follow_up_dispatch_target")
    if last_target != target:
        return True, "No follow-up monitor run has been queued for this target yet."

    last_dispatched_at = parse_github_timestamp(state.get("last_follow_up_dispatch_at"))
    if not last_dispatched_at:
        return True, "The previous follow-up dispatch timestamp is missing, so dispatching again."

    dispatch_interval = timedelta(seconds=FOLLOW_UP_POLL_SECONDS)
    if datetime.now(timezone.utc) - last_dispatched_at >= dispatch_interval:
        return True, (
            f"The previous follow-up monitor dispatch for this target is older than "
            f"{FOLLOW_UP_POLL_SECONDS} second(s)."
        )

    return False, (
        f"A follow-up monitor run for this target was already dispatched at "
        f"`{state.get('last_follow_up_dispatch_at')}`."
    )


def queue_follow_up_monitor_if_needed(client, state, target, reason):
    dispatch_needed, dispatch_reason = should_dispatch_follow_up(state, target)
    if not dispatch_needed:
        log(dispatch_reason)
        return dispatch_reason

    try:
        dispatched_identifier = client.dispatch_workflow(
            MONITOR_WORKFLOW_FILE,
            MONITOR_WORKFLOW_REF,
            {
                "pr_number": str(TARGET_PR_NUMBER),
                "delay_seconds": str(FOLLOW_UP_POLL_SECONDS),
                "source": "self-backstop",
                "reason": reason[:120],
            },
        )
    except RuntimeError as exc:
        failure_message = f"Failed to queue a follow-up monitor run via workflow_dispatch: {exc}"
        log(failure_message)
        return f"{dispatch_reason} {failure_message}"

    remember_follow_up_dispatch(state, target, reason)
    queued_message = (
        f"Queued a follow-up monitor run via workflow_dispatch in about "
        f"{FOLLOW_UP_POLL_SECONDS} second(s)."
    )
    if dispatched_identifier != MONITOR_WORKFLOW_FILE:
        queued_message = (
            f"{queued_message} Retried with workflow identifier `{dispatched_identifier}`."
        )
    log(f"{queued_message} Target={target}. Reason={reason}")
    return f"{dispatch_reason} {queued_message}"


def should_retry_processed_attempt(state, run_attempt):
    last_requested_run_attempt = state.get("last_rerun_request_run_attempt")
    if last_requested_run_attempt != run_attempt:
        return True, "No rerun request has been recorded yet for this completed attempt."

    last_requested_at = parse_github_timestamp(state.get("last_rerun_request_at"))
    if not last_requested_at:
        return True, "The previous rerun request timestamp is missing, so retrying once."

    retry_after = timedelta(minutes=RERUN_RETRY_AFTER_MINUTES)
    if datetime.now(timezone.utc) - last_requested_at >= retry_after:
        return True, (
            f"The previous rerun request for this attempt is older than "
            f"{RERUN_RETRY_AFTER_MINUTES} minute(s)."
        )

    return False, (
        f"A rerun was already requested for this attempt at `{state.get('last_rerun_request_at')}`; "
        "waiting for GitHub to create a newer attempt."
    )


def should_reopen_processed_attempt(state, run, run_attempt):
    if not ENABLE_PR_REOPEN_RECOVERY:
        return False, "PR reopen recovery is disabled."
    if run.get("conclusion") != "startup_failure":
        return False, "PR reopen recovery only applies to `startup_failure` runs."

    last_reopen_run_attempt = state.get("last_pr_reopen_run_attempt")
    if last_reopen_run_attempt != run_attempt:
        return True, "No PR reopen recovery has been recorded yet for this stalled attempt."

    last_reopen_at = parse_github_timestamp(state.get("last_pr_reopen_at"))
    if not last_reopen_at:
        return True, "The previous PR reopen timestamp is missing, so reopening again."

    reopen_after = timedelta(seconds=PR_REOPEN_RETRY_AFTER_SECONDS)
    if datetime.now(timezone.utc) - last_reopen_at >= reopen_after:
        return True, (
            f"The previous PR reopen recovery for this attempt is older than "
            f"{PR_REOPEN_RETRY_AFTER_SECONDS} second(s)."
        )

    return False, (
        f"The PR was already reopened for this attempt at `{state.get('last_pr_reopen_at')}`; "
        "waiting for GitHub to create a fresh pull_request CI run."
    )


def should_dispatch_ci_recovery(state, pr):
    if not ENABLE_CI_WORKFLOW_DISPATCH_RECOVERY:
        return False, "CI workflow_dispatch recovery is disabled."
    if not CI_WORKFLOW_FILE or not CI_WORKFLOW_REF:
        return False, "CI workflow_dispatch recovery is not fully configured."

    head_sha = pr["head"]["sha"]
    if state.get("last_ci_dispatch_head_sha") != head_sha:
        return True, "No CI workflow_dispatch recovery has been recorded yet for this PR head."

    last_dispatched_at = parse_github_timestamp(state.get("last_ci_dispatch_at"))
    if not last_dispatched_at:
        return True, "The previous CI workflow_dispatch timestamp is missing, so dispatching again."

    retry_after = timedelta(seconds=CI_DISPATCH_RETRY_AFTER_SECONDS)
    if datetime.now(timezone.utc) - last_dispatched_at >= retry_after:
        return True, (
            f"The previous CI workflow_dispatch recovery for this PR head is older than "
            f"{CI_DISPATCH_RETRY_AFTER_SECONDS} second(s)."
        )

    return False, (
        f"A CI workflow_dispatch recovery for this PR head was already requested at "
        f"`{state.get('last_ci_dispatch_at')}`."
    )


def wait_for_fresh_pr_ci_run(client, pr_number, previous_run_id, expected_head_sha):
    deadline = time.time() + PR_REOPEN_WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        pr = client.get_pr(pr_number)
        latest_run = find_latest_ci_run_for_pr(client, pr)
        if (
            latest_run
            and latest_run.get("id") != previous_run_id
            and latest_run.get("head_sha") == expected_head_sha
        ):
            return latest_run
        time.sleep(PR_REOPEN_WAIT_POLL_SECONDS)
    return None


def wait_for_dispatched_ci_run(client, pr_number, previous_run_ids):
    deadline = time.time() + CI_DISPATCH_WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        pr = client.get_pr(pr_number)
        latest_run = find_latest_ci_run_for_pr(client, pr)
        if latest_run and latest_run.get("id") not in previous_run_ids:
            return latest_run
        time.sleep(CI_DISPATCH_WAIT_POLL_SECONDS)
    return None


def reopen_pr_for_recovery(client, pr_number, state, run_attempt, previous_run):
    client.update_pull_request_state(pr_number, "closed")
    time.sleep(3)
    client.update_pull_request_state(pr_number, "open")
    remember_pr_reopen(state, run_attempt)
    return wait_for_fresh_pr_ci_run(
        client,
        pr_number,
        previous_run["id"],
        previous_run.get("head_sha"),
    )


def dispatch_ci_workflow_for_recovery(client, pr, state):
    previous_run_ids = {run.get("id") for run in find_recent_ci_runs_for_pr(client, pr)}
    client.dispatch_workflow(
        CI_WORKFLOW_FILE,
        CI_WORKFLOW_REF,
        {
            "pr_number": str(TARGET_PR_NUMBER),
            "checkout_ref": pr["head"]["ref"],
            "checkout_sha": pr["head"]["sha"],
        },
    )
    remember_ci_dispatch(state, pr["head"]["sha"])
    return wait_for_dispatched_ci_run(client, TARGET_PR_NUMBER, previous_run_ids)


def process_completed_runs_for_pr(
    client,
    run,
    state,
    state_comment,
    comments,
    attempt=None,
    jobs=None,
    action_label="recorded completed CI run",
    note_prefix="",
    allow_active_trigger=True,
):
    if not PASSIVE_COMPLETION_MONITOR and int(state.get("completed_rounds", 0)) >= MIN_ROUNDS:
        return False
    if run.get("status") != "completed":
        return False

    attempt = int(attempt if attempt is not None else run.get("run_attempt", 1))
    run_attempt = run_attempt_key(run["id"], attempt)
    processed_run_attempts = set(state.get("processed_run_attempts", []))
    if run_attempt in processed_run_attempts:
        return False

    if jobs is None:
        jobs = load_jobs_for_run_attempt(client, run["id"], attempt)
    round_number = int(state.get("completed_rounds", 0)) + 1
    post_result_comment_if_needed(client, TARGET_PR_NUMBER, run, round_number, jobs, comments)

    processed_run_attempts.add(run_attempt)
    state["processed_run_attempts"] = sorted(
        processed_run_attempts,
        key=run_attempt_sort_key,
    )
    state["completed_rounds"] = round_number
    state["last_processed_attempt"] = attempt
    state["last_processed_run_id"] = run["id"]

    note = (
        f"Processed CI run `{run['id']}` attempt `{attempt}` "
        f"as recorded item `{round_number}` in this session."
    )
    if note_prefix:
        note = f"{note_prefix} {note}"
    if PASSIVE_COMPLETION_MONITOR:
        state["last_action"] = action_label
        note += f" {active_polling_note() if active_polling_mode_enabled() else passive_monitoring_note()}"
        if allow_active_trigger:
            rerun_requested, rerun_note = maybe_trigger_idle_ci(client, state, run)
            if rerun_requested:
                state["last_action"] = "requested CI rerun after idle poll"
            note += f" {rerun_note}"
    elif round_number < MIN_ROUNDS:
        client.rerun_workflow(run["id"])
        remember_rerun_request(state, run_attempt)
        state["last_action"] = f"requested CI rerun after round {round_number}"
        note += " Requested another full CI rerun."
        note += " " + queue_follow_up_monitor_if_needed(
            client,
            state,
            f"rerun:{run_attempt}",
            f"wait-for-rerun:{run['id']}:{attempt}",
        )
    else:
        state["last_action"] = "target rounds reached"
        note += " Reached the configured round target."

    upsert_state_comment(
        client,
        TARGET_PR_NUMBER,
        state_comment,
        state,
        run,
        build_state_jobs(jobs, run),
        note,
    )
    return True


def main():
    token = os.environ["GITHUB_TOKEN"]
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    client = GitHubClient(token, owner, repo)
    event = parse_event()
    pr = client.get_pr(TARGET_PR_NUMBER)
    comments = client.list_pr_comments(TARGET_PR_NUMBER)
    state_comment = find_state_comment(comments)
    state = parse_state(state_comment)

    if pr.get("state") != "open":
        state["last_action"] = "stopped because PR is not open"
        upsert_state_comment(client, TARGET_PR_NUMBER, state_comment, state, None, [], f"PR is `{pr.get('state')}`.")
        return 0

    run, event_job, reason = resolve_target_context(client, event, pr)
    if not run:
        if reason and reason.startswith("Ignored "):
            log(reason)
            return 0
        follow_up_note = ""
        if reason and "No matching CI workflow run found" in reason:
            if active_polling_mode_enabled():
                bootstrapped, bootstrap_note, fresh_run = maybe_bootstrap_latest_head_ci(
                    client,
                    pr,
                    state,
                )
                if bootstrapped:
                    state["last_action"] = "dispatched CI workflow for current PR head"
                    if fresh_run:
                        fresh_attempt = int(fresh_run.get("run_attempt", 1))
                        fresh_jobs = load_jobs_for_run_attempt(
                            client,
                            fresh_run["id"],
                            fresh_attempt,
                        )
                        upsert_state_comment(
                            client,
                            TARGET_PR_NUMBER,
                            state_comment,
                            state,
                            fresh_run,
                            build_state_jobs(fresh_jobs, fresh_run),
                            f"{reason} {bootstrap_note}",
                        )
                    else:
                        upsert_state_comment(
                            client,
                            TARGET_PR_NUMBER,
                            state_comment,
                            state,
                            None,
                            [],
                            f"{reason} {bootstrap_note}",
                        )
                    return 0
                state["last_action"] = "waiting for latest PR head CI run"
                follow_up_note = " " + bootstrap_note
            elif PASSIVE_COMPLETION_MONITOR:
                state["last_action"] = "waiting for latest PR head CI run"
                follow_up_note = " " + wait_note()
            else:
                follow_up_note = " " + queue_follow_up_monitor_if_needed(
                    client,
                    state,
                    f"no-run:{pr['head']['sha']}",
                    f"wait-for-ci-run:{pr['head']['sha']}",
                )
        if state.get("last_action") == "initialized":
            state["last_action"] = "no-op"
        upsert_state_comment(
            client,
            TARGET_PR_NUMBER,
            state_comment,
            state,
            None,
            [],
            f"{reason}{follow_up_note}",
        )
        return 0

    latest_run = find_latest_ci_run_for_pr(client, pr)
    backfilled_runs, state_comment, comments = backfill_recent_completed_runs(
        client,
        pr,
        state,
        state_comment,
        comments,
    )
    cancelled_runs = []
    if not PASSIVE_COMPLETION_MONITOR:
        cancelled_runs = cancel_outdated_ci_runs(client, pr, latest_run)
    cancelled_note = ""
    if cancelled_runs:
        cancelled_ids = ", ".join(f"`{item['id']}`" for item in cancelled_runs)
        cancelled_note = f" Cancelled older active CI runs: {cancelled_ids}."

    if latest_run is None:
        state["last_action"] = "waiting for latest PR head CI run"
        if active_polling_mode_enabled():
            bootstrapped, bootstrap_note, fresh_run = maybe_bootstrap_latest_head_ci(
                client,
                pr,
                state,
            )
            if bootstrapped:
                state["last_action"] = "dispatched CI workflow for current PR head"
                if fresh_run:
                    fresh_attempt = int(fresh_run.get("run_attempt", 1))
                    fresh_jobs = load_jobs_for_run_attempt(
                        client,
                        fresh_run["id"],
                        fresh_attempt,
                    )
                    upsert_state_comment(
                        client,
                        TARGET_PR_NUMBER,
                        state_comment,
                        state,
                        fresh_run,
                        build_state_jobs(fresh_jobs, fresh_run),
                        (
                            f"No CI workflow run exists yet for the current PR head "
                            f"`{pr['head']['sha'][:7]}`.{cancelled_note} {bootstrap_note}"
                        ),
                    )
                else:
                    upsert_state_comment(
                        client,
                        TARGET_PR_NUMBER,
                        state_comment,
                        state,
                        None,
                        [],
                        (
                            f"No CI workflow run exists yet for the current PR head "
                            f"`{pr['head']['sha'][:7]}`.{cancelled_note} {bootstrap_note}"
                        ),
                    )
                return 0
            follow_up_note = bootstrap_note
        else:
            follow_up_note = (
                wait_note()
                if PASSIVE_COMPLETION_MONITOR
                else queue_follow_up_monitor_if_needed(
                    client,
                    state,
                    f"latest-run-missing:{pr['head']['sha']}",
                    f"wait-for-latest-run:{pr['head']['sha']}",
                )
            )
        upsert_state_comment(
            client,
            TARGET_PR_NUMBER,
            state_comment,
            state,
            None,
            [],
            (
                f"No CI workflow run exists yet for the current PR head "
                f"`{pr['head']['sha'][:7]}`.{cancelled_note} {follow_up_note}"
            ),
        )
        return 0

    if run.get("id") != latest_run.get("id"):
        state["last_action"] = "ignored non-latest CI run"
        upsert_state_comment(
            client,
            TARGET_PR_NUMBER,
            state_comment,
            state,
            latest_run,
            [],
            f"Ignored CI run `{run['id']}` because the latest PR-head CI run is `{latest_run['id']}`.{cancelled_note}",
        )
        return 0

    attempt = int(run.get("run_attempt", 1))
    jobs = load_jobs_for_run_attempt(client, run["id"], attempt)
    state_jobs = build_state_jobs(jobs, run)
    run_attempt = run_attempt_key(run["id"], attempt)
    processed_run_attempts = set(state.get("processed_run_attempts", []))

    if os.environ["GITHUB_EVENT_NAME"] == "workflow_job":
        if RESULT_COMMENT_MODE != "job":
            log("Ignoring workflow_job event because RESULT_COMMENT_MODE is not `job`.")
            return 0
        if run_attempt in processed_run_attempts:
            log(
                f"Run {run['id']} attempt {attempt} was already processed; "
                "skipping completed-job reporting."
            )
            return 0
        round_number = int(state.get("completed_rounds", 0)) + 1
        if event_job:
            current_job = find_job_by_id(jobs, event_job.get("id")) or event_job
            post_job_result_comments_if_needed(
                client,
                TARGET_PR_NUMBER,
                run,
                round_number,
                [current_job],
                comments,
            )
            comments = client.list_pr_comments(TARGET_PR_NUMBER)
        process_completed_runs_for_pr(
            client,
            run,
            state,
            state_comment,
            comments,
            attempt=attempt,
            jobs=jobs,
        )
        return 0

    if process_completed_runs_for_pr(
        client,
        run,
        state,
        state_comment,
        comments,
        attempt=attempt,
        jobs=jobs,
    ):
        return 0

    if run.get("status") != "completed":
        state["last_action"] = "waiting for CI workflow completion"
        follow_up_note = (
            wait_note()
            if PASSIVE_COMPLETION_MONITOR
            else queue_follow_up_monitor_if_needed(
                client,
                state,
                f"in-progress:{run_attempt}",
                f"wait-for-ci-completion:{run['id']}:{attempt}",
            )
        )
        upsert_state_comment(
            client,
            TARGET_PR_NUMBER,
            state_comment,
            state,
            run,
            state_jobs,
            (
                f"CI run `{run['id']}` attempt `{run.get('run_attempt', '?')}` is still "
                f"`{run.get('status')}`.{cancelled_note} {follow_up_note}"
            ),
        )
        return 0

    if run_attempt in processed_run_attempts:
        if PASSIVE_COMPLETION_MONITOR:
            rerun_requested, rerun_note = maybe_trigger_idle_ci(client, state, run)
            if rerun_requested:
                state["last_action"] = "requested CI rerun after idle poll"
                note = (
                    f"Run `{run['id']}` attempt `{attempt}` was already processed in this session."
                    f"{cancelled_note} {rerun_note}"
                )
            else:
                state["last_action"] = "waiting for newer completed CI run"
                note = (
                    f"Run `{run['id']}` attempt `{attempt}` was already processed in this session."
                    f"{cancelled_note} {rerun_note if active_polling_mode_enabled() else wait_note()}"
                )
            upsert_state_comment(
                client,
                TARGET_PR_NUMBER,
                state_comment,
                state,
                run,
                state_jobs,
                note,
            )
            return 0
        if int(state.get("completed_rounds", 0)) < MIN_ROUNDS:
            if run.get("conclusion") == "startup_failure" and state.get("last_rerun_request_run_attempt") == run_attempt:
                recovery_note = (
                    f"Run `{run['id']}` attempt `{attempt}` remained stuck in `startup_failure` "
                    "after rerun requests. "
                )
                reopen_needed, reopen_reason = should_reopen_processed_attempt(state, run, run_attempt)
                if reopen_needed:
                    fresh_run = reopen_pr_for_recovery(
                        client,
                        TARGET_PR_NUMBER,
                        state,
                        run_attempt,
                        run,
                    )
                    state["last_action"] = "reopened PR to bootstrap fresh CI run"
                    if fresh_run:
                        fresh_jobs = client.get_run_jobs(fresh_run["id"], filter_mode="latest")
                        follow_up_note = queue_follow_up_monitor_if_needed(
                            client,
                            state,
                            f"fresh-run:{fresh_run['id']}:{fresh_run.get('run_attempt', 1)}",
                            f"wait-for-fresh-ci-run:{fresh_run['id']}:{fresh_run.get('run_attempt', 1)}",
                        )
                        upsert_state_comment(
                            client,
                            TARGET_PR_NUMBER,
                            state_comment,
                            state,
                            fresh_run,
                            build_state_jobs(fresh_jobs, fresh_run),
                            (
                                f"{recovery_note}The PR was closed and reopened. {reopen_reason} "
                                f"Observed a fresh PR CI run `{fresh_run['id']}` attempt "
                                f"`{fresh_run.get('run_attempt', 1)}` with status "
                                f"`{fresh_run.get('status')}`. {follow_up_note}"
                            ),
                        )
                        return 0
                    recovery_note += (
                        f"The PR was closed and reopened. {reopen_reason} "
                        f"No fresh PR CI run appeared within {PR_REOPEN_WAIT_TIMEOUT_SECONDS} second(s). "
                    )
                else:
                    recovery_note += f"{reopen_reason} "

                dispatch_needed, dispatch_reason = should_dispatch_ci_recovery(state, pr)
                if dispatch_needed:
                    fresh_run = dispatch_ci_workflow_for_recovery(client, pr, state)
                    state["last_action"] = "dispatched CI workflow for PR head"
                    if fresh_run:
                        fresh_jobs = client.get_run_jobs(fresh_run["id"], filter_mode="latest")
                        follow_up_note = queue_follow_up_monitor_if_needed(
                            client,
                            state,
                            f"fresh-run:{fresh_run['id']}:{fresh_run.get('run_attempt', 1)}",
                            f"wait-for-fresh-ci-run:{fresh_run['id']}:{fresh_run.get('run_attempt', 1)}",
                        )
                        upsert_state_comment(
                            client,
                            TARGET_PR_NUMBER,
                            state_comment,
                            state,
                            fresh_run,
                            build_state_jobs(fresh_jobs, fresh_run),
                            (
                                f"{recovery_note}{dispatch_reason} "
                                f"Requested a CI workflow_dispatch recovery run for "
                                f"`{pr['head']['ref']}` at `{pr['head']['sha'][:7]}`. "
                                f"Observed CI run `{fresh_run['id']}` attempt "
                                f"`{fresh_run.get('run_attempt', 1)}` with status "
                                f"`{fresh_run.get('status')}`. {follow_up_note}"
                            ),
                        )
                    else:
                        follow_up_note = queue_follow_up_monitor_if_needed(
                            client,
                            state,
                            f"dispatch:{run_attempt}:{pr['head']['sha']}",
                            f"wait-for-dispatched-ci:{run['id']}:{attempt}",
                        )
                        upsert_state_comment(
                            client,
                            TARGET_PR_NUMBER,
                            state_comment,
                            state,
                            run,
                            state_jobs,
                            (
                                f"{recovery_note}{dispatch_reason} "
                                f"Requested a CI workflow_dispatch recovery run for "
                                f"`{pr['head']['ref']}` at `{pr['head']['sha'][:7]}`. "
                                f"No dispatched CI run appeared within {CI_DISPATCH_WAIT_TIMEOUT_SECONDS} second(s). "
                                f"{follow_up_note}"
                            ),
                        )
                    return 0

                state["last_action"] = "waiting for CI workflow dispatch recovery"
                follow_up_note = queue_follow_up_monitor_if_needed(
                    client,
                    state,
                    f"dispatch:{run_attempt}:{pr['head']['sha']}",
                    f"wait-for-dispatched-ci:{run['id']}:{attempt}",
                )
                upsert_state_comment(
                    client,
                    TARGET_PR_NUMBER,
                    state_comment,
                    state,
                    run,
                    state_jobs,
                    f"{recovery_note}{dispatch_reason} {follow_up_note}",
                )
                return 0
            retry_needed, retry_reason = should_retry_processed_attempt(state, run_attempt)
            if retry_needed:
                client.rerun_workflow(run["id"])
                remember_rerun_request(state, run_attempt)
                state["last_action"] = "re-requested CI rerun for processed attempt"
                follow_up_note = queue_follow_up_monitor_if_needed(
                    client,
                    state,
                    f"processed:{run_attempt}",
                    f"wait-for-new-attempt:{run['id']}:{attempt}",
                )
                upsert_state_comment(
                    client,
                    TARGET_PR_NUMBER,
                    state_comment,
                    state,
                    run,
                    state_jobs,
                    (
                        f"Run `{run['id']}` attempt `{attempt}` was already counted, "
                        f"but no newer attempt is active yet. {retry_reason}{cancelled_note} "
                        f"Requested another full CI rerun. {follow_up_note}"
                    ),
                )
                return 0
        state["last_action"] = "attempt already processed"
        follow_up_note = queue_follow_up_monitor_if_needed(
            client,
            state,
            f"processed:{run_attempt}",
            f"wait-for-new-attempt:{run['id']}:{attempt}",
        )
        upsert_state_comment(
            client,
            TARGET_PR_NUMBER,
            state_comment,
            state,
            run,
            state_jobs,
            (
                f"Run `{run['id']}` attempt `{attempt}` was already processed in this session. "
                f"{should_retry_processed_attempt(state, run_attempt)[1]}{cancelled_note} "
                f"{follow_up_note}"
            ),
        )
        return 0

    state["last_action"] = "target rounds reached"
    upsert_state_comment(
        client,
        TARGET_PR_NUMBER,
        state_comment,
        state,
        run,
        state_jobs,
        f"Observed completed CI run `{run['id']}` attempt `{attempt}`, but the configured round target was already reached.{cancelled_note}",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
