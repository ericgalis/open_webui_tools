# Spec: Coding Jobs tool — review, remediation, documentation and post-session functions

- **Goal**: Extend the Open WebUI Coding Jobs tool (`coding_jobs.json`) with functions for the new coding-agent endpoints, so the Dev Manager model can start security tests, architecture reviews, remediations and documentation, fetch reports, and carry decisions back to the user.

- **Context** (verified in `open_webui_tools`): `coding_jobs.json` is the Open WebUI tool export: a list with one object `{id: "coding_jobs", name, meta:{description, manifest:{title, description, version, license}}, content:"<Python source>"}`. The source defines `UNTRUSTED_NOTICE`, `_call(valves, user, method, path, body, untrusted)`, `_requested_by(user)`, and `Tools` with `Valves` (`SERVICE_URL` default `http://coding-agent:8000`, `SERVICE_API_KEY`, `TIMEOUT_SECONDS`). Endpoints it calls today: `GET /repos`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs`, `POST /jobs/{id}/cancel`. The service-side contract is defined in the companion spec in `docker_compose_home_servers` (`docs/specs/coding-agent-review-remediation-documentation-and-post-session-endpoints.md`): `POST /security-test`, `POST /architecture-review`, `POST /remediate`, `POST /document`, `POST /post-session`, `GET /jobs/{id}/report`, `GET /jobs/{id}/decisions`, `POST /jobs/{id}/decisions`, `GET /chains`, `GET /chains/{id}`, `POST /chains/{id}/cancel`; new job final state `needs_decisions`; jobs gain `kind`, `report_job_id`, `followup_job_id`.

- **Changes** (all inside `coding_jobs.json`; bump `version` to `0.2.0` in `meta.manifest` and the docstring, and extend the descriptions to mention reviews/remediation/post-session):
  1. New tool functions on `Tools`, each a thin `_call` wrapper in the existing style, with docstrings in the current format (`:param ...:` lines):
     - `coding_security_test(repo, base_branch="")` → `POST /security-test`. Docstring: comprehensive security test of repo at branch; read-only; produces a markdown report; long-running; confirm repo and branch with the user first.
     - `coding_architecture_review(repo, base_branch="")` → `POST /architecture-review`. Same caveats.
     - `coding_remediate(repo, report_job_id, base_branch="")` → `POST /remediate`. Docstring: fixes the findings of a finished security or architecture review job; may end with `needs_decisions`; confirm first.
     - `coding_create_documentation(repo, base_branch="")` → `POST /document`. Writes updated README(s) on the job branch.
     - `coding_start_post_session(repo, base_branch="")` → `POST /post-session`. Docstring: queues architecture review → remediation → security test → remediation → documentation as one chain; pauses for the user's decisions when a remediation step needs them; confirm first.
     - `coding_get_report(job_id, offset=0, max_chars=40000)` → `GET /jobs/{id}/report` with `untrusted=True` — the report is derived from repository data.
     - `coding_get_decisions(job_id)` → `GET /jobs/{id}/decisions` with `untrusted=True`.
     - `coding_submit_decisions(job_id, decisions)` → `POST /jobs/{id}/decisions` body `{decisions}`. Docstring: submits the user's binding answers to a remediation job's pending questions and creates the follow-up job; return its job id.
     - `coding_chain_status(chain_id)` → `GET /chains/{id}` (untrusted not needed — chain records are runner-authored metadata; keep it trusted).
     - `coding_cancel_chain(chain_id)` → `POST /chains/{id}/cancel`.
  2. `coding_list_jobs`: add `"kind"` to the `keep` tuple so job kinds show in listings.
  3. `coding_job_status`: docstring updated to mention the `needs_decisions` state and that `coding_get_decisions`/`coding_get_report` retrieve the details.
  4. Reports and decisions content is repository-derived — every call returning it must use `untrusted=True` so the `UNTRUSTED_NOTICE` is prepended (this is the existing mechanism; no new code path).

- **Out of scope**: no changes to the sidecar service, other tool JSON files, or any Open WebUI server configuration; no new valves; **do not modify files under docs/specs/**.

- **Acceptance criteria**:
  1. All ten functions above exist with the exact HTTP method/path/body mapping to the service spec.
  2. `coding_get_report` and `coding_get_decisions` pass `untrusted=True`.
  3. `version` is `0.2.0` in both `meta.manifest` and the embedded docstring; descriptions mention the new capabilities.
  4. The file is valid JSON and the embedded `content` parses as valid Python.

- **Verification**:
  - `python3 -c "import json,ast; d=json.load(open('coding_jobs.json')); ast.parse(d[0]['content']); print('ok')"`
  - `grep -c "def coding_" coding_jobs.json` shows the expected function count (5 existing + 10 new = 15 `def coding_*`).
  - Manual smoke test after the service spec is deployed: from the Coding Jobs model, run one security test and one post-session chain end-to-end, including a decisions round-trip.

- **Assumptions**: the service spec's API ships unchanged; if the service implementation deviated, that spec is updated first and this one is reconciled before implementation.
