# Synthetic inventory importer

A fictional project for testing Project Viz. No actual inventory, private sessions, or user evidence is included. The project goal is to import an inventory CSV with traceable validation.

`PLAN.md` documents the work. `reports/import-validation.md` is an intentionally failed **synthetic** result. `catalog.json` contains the matching work graph, including a nested task and a planned task. It is a fixture, not evidence that software or tests were run.

From the repository root, use the bundled CLI:

```sh
python skills/project-viz/scripts/project_viz.py init --project examples/synthetic-project --title "Synthetic inventory importer" --goal "Import an inventory CSV with traceable validation."
python skills/project-viz/scripts/project_viz.py catalog --project examples/synthetic-project --file examples/synthetic-project/catalog.json
python skills/project-viz/scripts/project_viz.py start --project examples/synthetic-project --port 0
python skills/project-viz/scripts/project_viz.py status --project examples/synthetic-project
python skills/project-viz/scripts/project_viz.py stop --project examples/synthetic-project
```

The CLI returns the actual local browser URL. The default private state lives outside the distributable source; use a temporary `--state-dir` consistently on every command for isolated testing. Existing real Codex logs are not needed for this fixture; an empty temporary `--codex-home` can isolate source discovery as well.
