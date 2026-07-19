# Learnings

- Report integration status as installed, connected, and verified. Do not call setup complete when process health passes but external authentication is still missing.
- With 12 hours left in a hackathon MVP, review architecture only for demo blockers, internal contradictions that block implementation, and factual drift that would misdirect the remaining work. Defer production hardening and long-term extensibility concerns.
- For architecture reviews, lead with the plain conclusion and the two or three changes that matter now. Keep evidence and line citations secondary; do not bury the decision in review terminology.
- For this hackathon MVP, treat security as visible trust hygiene, not a production-hardening requirement. Preserve the settled design and demo behavior; do not expand scope with production controls.
- At every phase handoff, state the next phase, why it is next, and whether it starts automatically. Never report only the completed phase and leave the user to ask what happens next.

## Databricks MCP

- Status as of 2026-07-18: installed, connected, and verified against the project workspace.
- Codex loads the project-scoped server from `.codex/config.toml` under the name `databricks`.
- The local upstream checkout lives at `.mcp/DatabricksMCP` and is pinned to commit `191a5bcdbeb06efd6f05706065f44e838113f155`.
- `scripts/run-databricks-mcp` loads the project `.env`, maps `DATABRICKS_ACCESS_TOKEN` to the Databricks SDK's expected `DATABRICKS_TOKEN`, removes the original variable from the child environment, and forces stdio plus read-only mode.
- `.env` must contain both `DATABRICKS_ACCESS_TOKEN` and `DATABRICKS_HOST`. Never copy the token into `.codex/config.toml`, scripts, logs, or documentation. `.gitignore` excludes `.env` and the downloaded MCP checkout.
- Live verification passed for MCP initialization, `health`, `current_identity`, `list_catalogs`, and `list_sql_warehouses`. Verification output must report pass/fail only; do not print identities, catalog names, warehouse details, hosts, or credentials.
- Run `codex mcp list` from the project root to confirm that `databricks` is enabled. Restart Codex or open a new task after changing MCP configuration.
- Current limitation: this pinned Phase 1 server is read-only. It can inspect identity, Unity Catalog catalogs, and SQL warehouses, but it cannot execute SQL or query facility rows.
- The Virtue Foundation Marketplace listing is installed as the Unity Catalog catalog `virtue_foundation_dais_2026`.
- For bounded row inspection, use the Databricks SQL Statements REST API with the project `.env` credentials. Keep warehouse IDs and credentials out of logs and docs; report only the query facts needed for the task.
