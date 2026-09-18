# Work protocol

This protocol separates persistent work from the sessions and tools that execute it. The runtime is project-local and uses Python's standard library. v0.2 has passive Codex-log collection, explicit semantic checkpoints, and SSH access plans for remote viewers; it has no Hooks or background model calls.

## Commands and identity

Resolve `scripts/project_viz.py` from the installed skill folder. Project commands accept `--project PATH` (default: current directory), `--state-dir PATH` (exact private state folder), and `--codex-home PATH` (optional source location). Keep overrides the same across project commands. The project path selects identity; do not reuse another project's private state directory. The local `tunnel` command instead reads a copied access plan with `--file PATH`.

| Command | Purpose |
| --- | --- |
| `doctor` | Report interpreter, paths, and source/runtime availability. |
| `init --title TITLE --goal TEXT` | Initialize project metadata; an overall goal is distinct from the latest activity. |
| `start --port 0 [--host 127.0.0.1] [--open]` | Start/reuse the viewer in the background; port zero chooses a free port. |
| `start --foreground` | Run under an external terminal or supervisor. |
| `start --ssh-target HOST --local-port 8894 [--ssh-port 22]` | Start/reuse the remote viewer and return a local SSH access plan. |
| `access --ssh-target HOST --local-port 8894 [--ssh-port 22] [--output PATH]` | Generate/update access to the running viewer; optionally save the private plan. |
| `tunnel --file PATH [--open] [--local-port 0]` | On the browser's computer, start SSH from the plan, verify the viewer, and optionally open it. |
| `status` / `stop` | Inspect the viewer and its configured access plan, or stop this project's matching viewer. |
| `import-history --seconds 15` | Bounded history ingestion; inspect coverage/errors before claiming completeness. |
| `context --limit 30` | Read normalized public context, existing work IDs, and project-document candidates. |
| `emit --file event.json` | Apply one semantic event; omit `--file` to read JSON stdin. |
| `catalog --file catalog.json` | Atomically upsert a curated project/work catalog; omission does not delete old work. |
| `export --output graph.json` | Export curated graph/summaries, excluding full raw source logs. |

All normal CLI responses are JSON. Use `--help` to check the installed version's options. Loopback is the default; do not silently expose a project on a public interface. No command should be interpreted as permission to resume a Codex conversation, run project jobs, or submit experiments.

Context is paginated without deleting older records. Its `pagination.nextOffset` can be passed as `context --offset N`, and `pagination.nextWorkOffset` as `context --work-offset N` (200 work IDs per page). Inspect these values when organizing a long-running project; one recent page is not its entire history.

## Remote browser access

Run `start` and `access` on the machine hosting the project. `start --project PATH --port 0 --ssh-target user@host_or_alias --local-port 8894` returns an `access` object with the following local instructions:

| Field | Use on the computer running the browser |
| --- | --- |
| `sshCommand` | Run in a terminal to forward the local port to the remote viewer. |
| `powershellCommand` | Run the corresponding command in Windows PowerShell. |
| `browserUrl` | Open after the SSH forwarding connection is established. |

Keep the forwarding terminal open. These commands require a local OpenSSH client but no local Python runtime. The server cannot establish a forwarding connection on the user's computer. A healthy server process confirms remote readiness; it does not establish that the user's local browser can reach it. For remote sessions, the Skill must provide the access command and local `browserUrl`, not just the server's localhost address.

If `--ssh-target` is omitted and `SSH_CONNECTION` is available, infer the server IP and SSH port from that connection. A private server address may require an explicit reachable `--ssh-target` or a local SSH configuration alias. `--ssh-port` selects an explicit SSH port; SSH aliases can supply configuration such as the user, port, and jump host. Authentication uses local OpenSSH facilities; the plan does not store an SSH password.

`access --project PATH --ssh-target alias --local-port 8894 [--ssh-port 22]` reuses the running viewer to generate or update the plan and returns the plan fields directly. `start` and `status --project PATH` include the plan in their `access` field. To change a busy local port for manual forwarding, choose a different `--local-port` with `access` and use its updated command and URL. After updating the Skill, run `start` to replace an older matching viewer without discarding history; use a fresh plan after any viewer restart.

For the optional local helper, add `--output PATH` to the server-side `access` command, then privately copy that JSON file to the local computer. It contains the viewer access token and must stay out of commits and shared artifacts. Run this command on the local computer:

```sh
python <local-skill>/scripts/project_viz.py tunnel --file <copied-access.json> --open
```

The helper starts SSH, verifies that the forwarded endpoint matches the planned viewer, and opens the browser after verification. It remains in the foreground. `--local-port 0` selects an available port on the local computer and outputs the updated browser URL. Ctrl+C closes forwarding without stopping the remote viewer; `stop --project PATH` on the server stops the viewer when needed. The viewer and forwarding use `127.0.0.1` by default. Platform support statements must distinguish documented commands from actual Windows/macOS test results.

## Semantic event

Synthetic example, reporting a failed acceptance check rather than a failed shell process:

```json
{
  "event_id": "demo-import-validation-001",
  "work_id": "import-validation",
  "parent_id": "data-import",
  "kind": "task",
  "title": "Validate imported records",
  "summary": "Validation finished; two duplicate identifiers remain.",
  "detail": "Repair the duplicate handling before accepting this import.",
  "execution_state": "finished",
  "outcome": "failed",
  "evidence": [
    {"path": "reports/import-validation.md", "line": 3, "note": "Synthetic acceptance result"}
  ],
  "sources": [
    {"session_id": "demo-session", "turn_id": "demo-turn"}
  ],
  "session_id": "demo-session",
  "turn_id": "demo-turn"
}
```

Required identity fields are `event_id` and `work_id`. Supply `execution_state` and `outcome` explicitly. `parent_id`, `kind`, `title`, `summary`, `detail`, `evidence`, `sources`, `session_id`, and `turn_id` provide optional context. Use `kind: task` for work and `kind: stage` only for a documented grouping. Omit `parent_id` for a top-level work item; never invent a parent that has not been created.

- Choose a stable, opaque or concise local `work_id`. Retain it across renames and sessions; it is not a mutable title or a session ID.
- Use a unique `event_id` for a new checkpoint and the same ID for a transport retry. A later changed result is a new event, so earlier failure evidence remains recorded.
- Keep task identities separate when two goals happen in the same session. Explicit source bindings take precedence over inferred grouping.
- Add only public, relevant source identifiers and explanations. Do not copy hidden reasoning, credentials, or whole tool outputs into curated summaries.

## Execution and outcome

| Field | Values | Meaning |
| --- | --- | --- |
| `execution_state` | `planned`, `running`, `waiting`, `finished`, `blocked`, `unknown` | Whether the work is executing or can proceed. |
| `outcome` | `unverified`, `passed`, `failed`, `inconclusive` | Whether the stated acceptance criterion has support. |

`finished + unverified` means execution ended without a verified outcome. `finished + failed` is a completed evaluation with a negative result. `running + unverified` describes ongoing work. A successful tool call, code compilation, or agent turn proves only its own operation; it does not close the larger objective. State acceptance scope in the summary or evidence. A `passed` event must include evidence; the runtime rejects a bare success assertion without it. Silence, missing logs, or a disappearing process never establish success.

## Curated catalog

Use this for importing reviewed history or reorganizing an existing graph. Names and stages come from the selected project's documents and public work. An empty or uncertain project does not need a filled-in template.

```json
{
  "project": {
    "title": "Synthetic inventory importer",
    "goal": "Import an inventory CSV with traceable validation."
  },
  "nodes": [
    {
      "id": "data-import",
      "kind": "task",
      "label": "Build inventory import",
      "summary": "A small synthetic work item for demonstrating the protocol.",
      "detail": "The input contract is documented; acceptance still needs verification.",
      "execution_state": "running",
      "outcome": "unverified",
      "evidence": [{"path": "PLAN.md", "line": 3}],
      "sources": []
    },
    {
      "id": "import-validation",
      "parentId": "data-import",
      "kind": "task",
      "label": "Validate imported records",
      "summary": "A synthetic check reports duplicate identifiers.",
      "execution_state": "finished",
      "outcome": "failed",
      "evidence": [{"path": "reports/import-validation.md", "line": 3}],
      "sources": []
    }
  ]
}
```

Catalog nodes use `id`, `parentId`, and `label`; events use `work_id`, `parent_id`, and `title`. Both describe the same stable work identities. `catalog` is atomic upsert, not replacement: old work omitted from an import remains. Reuse IDs and inspect existing context before curating another batch. Evidence paths are relative to the chosen project, use `/`, and must point to material actually read. Use a line number and a short note when useful; do not turn a missing file into proof. Preserve source timestamps in a summary when historical status could otherwise be mistaken for current activity.

## Optional project convention

Only when requested for that project, use a removable block such as:

```markdown
<!-- project-viz:start -->
When Project Viz is available, preserve existing work IDs and report meaningful
public progress using its work protocol. Execution ending does not imply a
passed outcome. Missing viewer access must not block the project's actual work.
<!-- project-viz:end -->
```

Keep the rest of `AGENTS.md` unchanged. Removal deletes only this block. This convention is guidance for later sessions, not proof that the skill has loaded, a hook has run, or every event has been captured.
