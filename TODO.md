# Kiln Development Plan

## Notes

- Message queue: SQLite at `.kiln/messages.db`, accessed via two MCP servers — `kiln-db` (generic `mcp-sqlite`, read/write) and `kiln-channel` (`kiln/mcp-server/channel.py`, blocking `wait_for_message()` for Claude agents; Copilot has no channel, only `kiln-db` polling)
- Receive/handoff mechanics live in the `/kiln-receive` and `/kiln-handoff` skills (`kiln/skills/kiln-receive`, `kiln/skills/kiln-handoff`), not inline in role files or the loop templates
- Testing: use the `selftest` profile (`kiln/profiles.json`) — see README "Communication System Health Check"
- Diagnostics: `bin/kiln-db.ps1` (`list-messages`/`show-message`/`stats`/`retry-message`/`clear-old`), `.kiln/logs/channel-<role>.log`, `.kiln/logs/claude-debug-<role>.log` (`--debug-file`)

---

## 1. Extend to Other Agent Types (Codex, Grok)

**Goal:** Enable MCP and full swarm integration for Codex and Grok backends

**Current state:** `codex`/`grok` are accepted as config values (e.g. `kiln.ps1`'s agent-name validation) but have no case in the actual command builders (`Build-WezTermAgentCommand`, `Get-WindowsTerminalAgentCommand` in `bin/kiln.ps1`) — selecting either just echoes "Agent not supported" instead of launching anything.

### 1.1 Codex Agent Implementation

- [ ] **Research Codex MCP & Permission Support**
  - Investigate if Codex CLI supports MCP servers
  - Identify config file location and format (likely `~/.codex/config.json` or similar)
  - Find equivalent to Claude's `--permission-mode bypassPermissions` or Copilot's `--allow-all`
  - Document any authentication requirements or setup steps

- [ ] **Implement Codex MCP Configuration**
  - Add Codex detection to `Prepare-AgentConfigs` (PowerShell)
  - Create `~/.codex/mcp-config.json` in `prepare_agent_configs` (Bash)
  - Add a Codex case to `Build-WezTermAgentCommand` / `Get-WindowsTerminalAgentCommand` (`kiln.ps1`) and the Unix equivalent (`kiln.sh`)
  - Include permission flags in the launch command
  - Test with a `codex-test` profile

- [ ] **Validate Codex Integration**
  - Launch a single Codex agent and verify MCP tool access
  - Test message send/receive from other agents
  - Verify Codex can read/write files in its worktree
  - Check for any session/timeout issues

### 1.2 Grok Agent Implementation

- [ ] **Research Grok MCP & Permission Support**
  - Investigate if Grok CLI supports MCP servers
  - Identify config file location and format
  - Find permission bypass mechanism (likely `--allow-all` or equivalent)
  - Check for environment variables or config overrides

- [ ] **Implement Grok MCP Configuration**
  - Add Grok detection to `Prepare-AgentConfigs` (PowerShell)
  - Create `~/.grok/mcp-config.json` in `prepare_agent_configs` (Bash)
  - Add a Grok case to the same command builders as above
  - Include permission flags in the launch command
  - Add a `grok-test` profile to `kiln/profiles.json`

- [ ] **Validate Grok Integration**
  - Launch a single Grok agent and verify MCP tool access
  - Test basic message passing in single-agent mode
  - Verify file operations (read/write) in worktree
  - Check terminal output and logging

### 1.3 Multi-Agent Mixed Testing

- [ ] **Create Mixed-Agent Test Profiles**
  - Profile with claude + copilot + codex
  - Profile with claude + copilot + grok
  - Profile with all four agents if feasible
  - Document any agents that can't coexist

- [ ] **Test Cross-Agent Communication**
  - Verify messages route correctly between different agent types
  - Test handoff chain: claude → copilot → codex → grok
  - Check message delivery times per agent type
  - Document any performance differences

- [ ] **Documentation Updates**
  - Update README with Codex/Grok setup instructions and "Platform Support" entries
  - Add example profiles showing mixed-agent setups
  - Document any agent-specific limitations or workarounds

---

## 2. Message Routing & Role Communication

- [ ] Verify message routing respects branch context under mixed-agent swarms specifically (single-backend swarms are already validated live)
- [ ] Test agent-to-agent handoff with different agent types once Codex/Grok land
- [ ] Validate specifier → coder → architect flow across mixed backends

---

## 3. Handoff Reliability Hardening

**Context:** Kiln runs persistent, always-on Claude agents communicating via the SQLite message queue. Historically ~10% of cycles failed — an agent would complete its work but never send (or never resume waiting for) the next handoff. Live multi-cycle testing against the LibraryHub example this session found and fixed one confirmed root cause: the loop templates' "not end-of-turn" guardrail only covered through the handoff-sent step, not the return to `/kiln-receive` — so a verified handoff looked like a valid stopping point. That's fixed (`kiln/templates/loop-*-claude.md`), along with the `/kiln-handoff` skill's own verify-and-retry on the INSERT.

The tracks below are further hardening layers — useful if stalls recur with a different root cause, or to make enforcement deterministic (code) rather than relying on prompt wording:

### Track A — Prompt Hardening (remaining piece)

- [ ] **Cycle-tracking summary**: at the top of each cycle (after `/kiln-receive`), have the agent emit a one-line internal status — `Cycle N: received from <sender>, handoff-name=<name>, commit=<hash>` — not a logbook write, just reasoning output that keeps "I must complete this full cycle" salient in context. (Self-verification after the handoff INSERT is already done — see `/kiln-handoff` Step 5.)

### Track B — Claude Code Hooks (deterministic enforcement, not yet implemented)

Hooks run shell code at fixed lifecycle points regardless of what the LLM decides — the highest-leverage option if prompt hardening alone isn't enough for a given stall pattern.

- [ ] **`Stop` hook** (`kiln/hooks/enforce-handoff.ps1`) — fires at end of every turn; checks whether a handoff was sent since the last `wait_for_message()` (e.g. query `messages` for a row from this role/branch in the last ~2 minutes). If missing *and* the agent did visible work this turn (git activity), return `{"decision": "block", "reason": "..."}` to force the agent to keep going. Needs the "did work happen" check to avoid blocking on legitimate idle waits.
- [ ] **`PostToolUse` hook** (`kiln/hooks/verify-write.ps1`) — after every `write_query` call, flag zero-row inserts so a failed INSERT surfaces immediately instead of silently.
- [ ] **Wire both into the generated `.claude/settings.json`** — `kiln/.claude/settings.json` template gets copied to every worktree; hook commands need absolute paths, resolved from `KILN_DIR`/`STATE_DIR` at generation time (same pattern as `.mcp.json`'s absolute DB path).

### Track C — Watcher/Orchestrator Process (near-100% reliability, high effort)

- [ ] Add a `workflow_state` table (`agent`, `branch`, `state`, `last_updated`; states `WAITING | EXECUTING | COMMITTED | HANDOFF_SENT`)
- [ ] `kiln/mcp-server/watcher.py` — polls `workflow_state` every ~10s; if an agent is stuck in `EXECUTING` past a timeout (default 15 min), INSERTs a corrective message into that agent's own inbox ("handoff not sent, complete it now")
- [ ] Infer state transitions from existing DB/git activity (delivered message → `EXECUTING`; new outgoing message → `HANDOFF_SENT`; next `wait_for_message()` → `WAITING`) rather than adding new tools agents must remember to call
- [ ] New optional `-Watcher` switch on `kiln.ps1`; extend `-Stop` to also kill `watcher.py` processes (same pattern as the existing `channel.py` kill list)
- [ ] **Escalation path if the watcher's nudge-based approach isn't enough**: a fuller orchestrator that owns *all* state transitions — agent only executes the task and signals completion; the orchestrator (deterministic code) does the squash/handoff/state-update itself. Bigger redesign (agents become "smart task executors" rather than full workflow owners); only worth it if Track C's lighter nudge approach proves insufficient in practice.

### Track D — Non-Claude Agent Messaging Compatibility

Distinct from Section 1 (launching Codex/Grok at all) — this is about letting *non-blocking* agents (Copilot today, Codex/Grok later) participate in the same message queue without a blocking channel.

- [ ] **`poll_for_message()` in `channel.py`** — non-blocking variant of the existing `_fetch_and_deliver()` used by `wait_for_message()`; single check, returns `{"received": false}` immediately if nothing's queued, so a non-Claude agent can call it in its own retry loop
- [ ] **Agent-type-aware receive instructions** — `runtime-copilot.md` / `loop-*-copilot.md` should reference `poll_for_message()` (once it exists) instead of raw `read_query`, if `kiln-channel` becomes available to Copilot (next bullet)
- [ ] **Per-role `kiln-channel` config for Copilot** — `Prepare-AgentConfigs` currently writes one global `~/.copilot/mcp-config.json` with only `kiln-db`; extend to include `kiln-channel` with per-role env vars, which requires Copilot to support per-worktree (not just global) MCP config — confirm this is possible before committing to the approach

---

## 4. Documentation MCP Server

**Goal:** Create an MCP server that indexes and serves documentation from multiple sources, enabling agents to reference external docs in real-time.

**Value Proposition:**
- **Specifier role**: Access API specs, requirements docs, design patterns without manual copy-paste
- **Architect role**: Query architecture patterns, design principles, tech decisions from centralized docs
- **All roles**: Consistent reference material without context switching

**Supported Sources:** PDF files (local), URLs, Markdown files (local/git), OpenAPI/GraphQL schemas, Confluence/Notion exports (if available)

**Implementation:**

- [ ] **Design MCP server schema**
  - Resource types: `documentation/pdf`, `documentation/url`, `documentation/markdown`, `documentation/schema`
  - Tool interface: `search_documentation(query, source?, max_results?)`, `get_document(id)`, `list_sources()`
  - Metadata: title, author, date, version, tags for filtering

- [ ] **Build documentation indexer**
  - PDF extraction: `PyPDF2`/`pdfplumber`, preserving structure
  - URL fetcher: HTTP client with caching, robots.txt respect
  - Markdown parser: extract headers, code blocks, maintain hierarchy
  - Schema parser: OpenAPI/GraphQL → readable docs
  - Semantic search: embeddings (Claude API or local)

- [ ] **Implement MCP server**
  - `kiln/mcp-server/doc-server.py`
  - Register alongside existing `kiln-db` server
  - Expose `search_documentation`, `get_document`, `list_sources` tools
  - Cache documents in SQLite (`.kiln/docs.db`)

- [ ] **Configuration in `kiln/profiles.json`**
  - Add optional `documentation` field per profile:

    ```json
    "documentation": [
      {"type": "pdf", "path": "./docs/api-reference.pdf"},
      {"type": "url", "url": "https://example.com/api"},
      {"type": "markdown", "path": "./docs/architecture/"}
    ]
    ```

- [ ] **Integration with agent roles**
  - Inject documentation server config into `CLAUDE.md` for specifier, architect
  - Document usage in `kiln/constitution/workflow.md`
  - Add to selftest: verify agents can query documentation

- [ ] **CLI utilities**
  - `kiln doc-index` — index docs without launching agents
  - `kiln doc-search <query>` — test search functionality
  - `kiln doc-sources` — list configured documentation sources

- [ ] **Testing & validation**
  - Various PDF formats (scanned, native, complex layouts)
  - URL fetching with rate limiting
  - Semantic search relevance
  - Performance with large doc collections (100+ pages)

---

## 5. Technical Slide Deck

**Goal:** Prepare a slide deck outline visualizing Kiln's architecture and workflow.

- [ ] Draft textual slide descriptions for:
  - Agent cycle and role handoff (now: `/kiln-receive` → work → `/kiln-handoff` → immediate return)
  - Worktree and merge strategy
  - Merged `CLAUDE.md` / `copilot-instructions.md` decision flow
  - Terminal layouts and launch workflows
  - Other architecture/highlight summary points
- [ ] Capture visual guidance per slide so it can be turned into graphics later
- [ ] Keep descriptions concise, technical, suitable for diagrams
- [ ] Note any non-obvious highlights worth calling out in a presentation
