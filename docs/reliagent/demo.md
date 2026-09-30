# ReliAgent 3–5 Minute Demo

Run from the repository root:

```bash
sh scripts/reliagent_ml_demo.sh
```

Pass a directory to preserve the generated SQLite database, artifacts, Trace,
Raw JSON, and Markdown report:

```bash
sh scripts/reliagent_ml_demo.sh /tmp/reliagent-demo
```

The script uses only Python and shell utilities already present in the project.
Set `PYTHON_BIN` when the repository virtual environment is not at `.venv`.

## Talk track

1. **Runtime boundary — 30 seconds.** Show that the experiment is a real
   subprocess ToolCall represented by Run, Step, ToolCall, Approval, and Event
   rows in SQLite.
2. **Approval — 30 seconds.** The Workflow stops before the effect-bearing
   experiment. `allow_once` belongs to this attempt and definition; it is not a
   reusable global permission.
3. **Real process loss — 45 seconds.** The approved process writes its metrics
   and Effect Marker, then the Runtime checkpoint terminates the CLI process
   with exit code 87 before `tool.completed` can be committed.
4. **Recovery decision — 60 seconds.** A new CLI process scans the orphan. The
   high-risk attempt becomes `recoverable` and is never replayed automatically.
   Reconciliation verifies the persisted process evidence and artifacts, then
   records `tool.reconciled`.
5. **Idempotency evidence — 45 seconds.** Two additional `resume` calls make no
   new effect. The final report says `effect_count=1` and
   `duplicate_effect=false`.
6. **Evaluation evidence — 30 seconds.** Open
   the current SHA-keyed evidence directory linked from `README.md`, then point
   to `ml-workflow-report.md`, its Raw JSON, and the manifest digest.
   Explain that 24/24 is scenario-contract correctness, not 24 successful tasks.

The last stdout line is a machine-readable JSON summary. A successful run must
show exit code 87, succeeded final status, one effect, no duplicate, a
contiguous Trace, and no missing lifecycle events.
