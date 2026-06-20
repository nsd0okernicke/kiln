# Kiln Development Plan

## Notes

- MCP server implementation: `.Kiln/mcp-server/` (SQLite-based)
- Current working: Claude and Copilot agents with MCP
- Socket location: `.Kiln/mcp.sock` (shared across worktrees)
- Testing: Use `selftest` profile with `Kiln_SELFTEST_MODE=true` and `/doctor`

---

## 1. Extend to Other Agent Types (Codex, Grok)

**Goal:** Enable MCP and full swarm integration for Codex and Grok backends

**Subtasks:**

### 1.1 Codex Agent Implementation

- [ ] **Research Codex MCP & Permission Support**
  - Investigate if Codex CLI supports MCP servers
  - Identify config file location and format (likely `~/.codex/config.json` or similar)
  - Find equivalent to Claude's `--permission-mode bypassPermissions` or Copilot's `--allow-all`
  - Document any authentication requirements or setup steps

- [ ] **Implement Codex MCP Configuration**
  - Add Codex detection to Prepare-AgentConfigs (PowerShell)
  - Create ~/.codex/mcp-config.json in prepare_agent_configs (Bash)
  - Add Codex case to Build-AgentCommand (wezterm.ps1, Kiln.ps1, Kiln.sh)
  - Include permission flags in launch command (--allow-all or equivalent)
  - Test with codex-test profile

- [ ] **Validate Codex Integration**
  - Launch single Codex agent with `/doctor` to verify MCP
  - Test message send/receive from other agents
  - Verify Codex can read/write files in worktree
  - Check for any session/timeout issues

### 1.2 Grok Agent Implementation

- [ ] **Research Grok MCP & Permission Support**
  - Investigate if Grok CLI supports MCP servers
  - Identify config file location and format
  - Find permission bypass mechanism (likely `--allow-all` or equivalent)
  - Check for environment variables or config overrides

- [ ] **Implement Grok MCP Configuration**
  - Add Grok detection to Prepare-AgentConfigs (PowerShell)
  - Create ~/.grok/mcp-config.json in prepare_agent_configs (Bash)
  - Add Grok case to Build-AgentCommand in all three launchers
  - Include permission flags in launch command
  - Add grok-test profile to profiles.yaml

- [ ] **Validate Grok Integration**
  - Launch single Grok agent with `/doctor` to verify MCP
  - Test basic message passing in single-agent mode
  - Verify file operations (read/write) in worktree
  - Check terminal output and logging

### 1.3 Multi-Agent Mixed Testing

- [ ] **Create Mixed-Agent Test Profiles**
  - Create profile with claude + copilot + codex
  - Create profile with claude + copilot + grok
  - Create profile with all four agents if feasible
  - Document any agents that can't coexist

- [ ] **Test Cross-Agent Communication**
  - Verify messages route correctly between different agent types
  - Test handoff chain: claude → copilot → codex → grok
  - Check message delivery times for each agent type
  - Document any performance differences

- [ ] **Documentation Updates**
  - Update README.md with Codex and Grok setup instructions
  - Add Codex and Grok to "Platform Support" section
  - Create example profiles showing mixed-agent setups
  - Document any agent-specific limitations or workarounds

---

## 2. Message Routing & Role Communication

- [ ] Verify message routing respects branch context (existing work)
- [ ] Test agent-to-agent handoff with different agent types
- [ ] Validate specifier → coder → architect flow across mixed backends

---

## 3. Git Strategy & Commit Management

- [ ] **Agent Commit Consolidation**
  - Research best approach: squash commits before handoff vs. interactive rebase
  - Implement automatic commit consolidation before agent handoff
  - Ensure each agent's work is represented as a single logical commit
  - Preserve commit history for debugging while maintaining clean main branch

- [ ] **Uniform Commit Messages**
  - Define commit message convention for agent-generated commits
  - Format: `[ROLE] Brief description - what was done`
  - Include context: issue number, changes scope, reason for changes
  - Implement template or validation to enforce consistency
  - Consider adding footer with agent metadata (agent type, worktree, branch)

- [ ] **Git History Cleanup**
  - Decide on merge strategy: squash vs. rebase vs. merge commits
  - Configure git hooks to validate agent commit messages
  - Document commit message standards in constitution/workflow.md
  - Create example commits from selftest runs

---

## 4. MCP Push Notifications (Hybrid Model)

**Current Implementation:** Agents poll SQLite inbox (pull model only)

**Goal:** Add push notifications for immediate delivery while keeping pull as fallback/alternative

**Alternative Push Mechanisms to Evaluate:**

### Option 1: Notifications (JSON-RPC Notifications)

- Servers send unsolicited messages to connected client (Claude Code/Copilot)
- No response expected from client
- MCP native, simple to implement
- **Pros:** Standard MCP pattern, low overhead
- **Cons:** Depends on persistent MCP connection; client must handle incoming messages

### Option 2: Subscriptions (Resource Subscriptions)

- Clients subscribe to resources via `resources/subscribe`
- Server pushes `resources/updated` notifications on changes
- Already in MCP spec; used by mcp-observer-server (file watching)
- **Pros:** Standard pattern, client can choose what to subscribe to
- **Cons:** Still requires client-side subscription setup; may not wake idle agents

### Option 3: Server-Sent Events (SSE) Transport

- For remote servers; persistent HTTP connection
- Server can push updates in real-time without polling
- **Pros:** True push over HTTP; works across networks
- **Cons:** Adds complexity (HTTP server); not suitable for local socket-based setup initially

### Option 4: Channels (Claude-Specific)

- Claude Code supports `claude/channel` capability declaration
- Server pushes messages directly into agent's session context
- "New task for Agent B arrived" → agent context updated
- **Pros:** Native Claude Code feature; seamless integration
- **Cons:** Claude-specific; may not work with Copilot or other backends

### Option 5: Triggers & Events Working Group (Future)

- Standardized webhook/callback mechanism in development
- Proactive server-to-client notifications with ordering guarantees
- **Pros:** Future-proof; standardized across MCP implementations
- **Cons:** Not yet finalized; may change; requires waiting

### Recommended Architecture (Push + Pull Hybrid)

1. **Central Orchestrator MCP Server** (enhance current mcp-server)
   - Owns SQLite database
   - Watches for new messages via SQLite triggers, file watcher, or low-frequency polling
   - Maintains connected clients per agent
   - Can emit both push notifications AND serve pull requests

2. **Push Notifications (Primary Path)**
   - When task written for Agent B, server sends JSON-RPC notification
   - Target agent's MCP client receives notification immediately
   - Host (Claude Code/Copilot) injects into agent's context
   - Agent wakes up and processes task without delay

3. **Pull/Polling (Secondary/Fallback Path)**
   - Keep current poll mechanism as always-available alternative
   - Agents can actively check inbox periodically (current behavior)
   - Works when push notifications are missed or agent was offline
   - Ensures messages aren't lost even if push fails

**Implementation Strategy:**

- [ ] Test JSON-RPC notifications with current SQLite server (keep polling enabled)
- [ ] Investigate Claude Code channel support (for context injection)
- [ ] Prototype Orchestrator enhancement to emit notifications alongside poll responses
- [ ] Validate hybrid behavior with mixed-agent swarm (Claude + Copilot)
- [ ] Document dual push/pull architecture in MCP architecture docs
- [ ] Measure performance: notification latency vs. polling interval trade-offs

---

## 5. Documentation MCP Server

**Goal:** Create an MCP server that indexes and serves documentation from multiple sources, enabling agents to reference external docs in real-time.

**Value Proposition:**
- **Specifier role**: Access API specs, requirements docs, design patterns without manual copy-paste
- **Architect role**: Query architecture patterns, design principles, tech decisions from centralized docs
- **All roles**: Consistent reference material without context switching

**Supported Sources:**

---

## 6. Create Technical Slide Deck

**Goal:** Prepare a slide deck outline that visualizes the Kiln project technical architecture and workflow.

- [ ] Draft textual slide descriptions for:
  - agent cycle and role handoff
  - worktree and merge strategy
  - merged `claude.md` / `copilot-instructions.md` decision flow
  - terminal layouts and launch workflows
  - other architecture/highlight summary points
- [ ] Capture visual guidance for each slide so the deck can be turned into graphics later
- [ ] Keep descriptions concise, technical, and suitable for conversion into diagrams or slide content
- [ ] Note any non-obvious highlights worth calling out in a presentation


- PDF files (local)
- URLs (web pages, APIs)
- Markdown files (local/git)
- OpenAPI/GraphQL schemas
- Confluence/Notion exports (if available)

**Implementation:**

- [ ] **Design MCP server schema**
  - Define resource types: `documentation/pdf`, `documentation/url`, `documentation/markdown`, `documentation/schema`
  - Define tool interface: `search_documentation(query, source?, max_results?)`, `get_document(id)`, `list_sources()`
  - Support metadata: title, author, date, version, tags for filtering

- [ ] **Build documentation indexer**
  - PDF extraction: use PyPDF2 or pdfplumber to extract text + preserve structure
  - URL fetcher: HTTP client with caching, robots.txt respect
  - Markdown parser: extract headers, code blocks, maintain hierarchy
  - Schema parser: OpenAPI/GraphQL to readable docs
  - Semantic search: embed docs using Claude API embeddings (or local embeddings)

- [ ] **Implement MCP server**
  - Create `.Kiln/mcp-server/doc-server.py` (or `.ts` if TypeScript preferred)
  - Register as named MCP server alongside existing `Kiln-db` server
  - Expose `search_documentation`, `get_document`, `list_sources` tools
  - Cache documents in SQLite for performance (`.Kiln/docs.db`)

- [ ] **Configuration in Kiln.profiles.yaml**
  - Add optional `documentation` field to profiles
  - Example:

    ```yaml
    profiles:
      dev:
        documentation:
          - type: pdf
            path: ./docs/api-reference.pdf
          - type: url
            url: https://example.com/api
          - type: markdown
            path: ./docs/architecture/
        terminals: [...]
    ```

- [ ] **Integration with agent roles**
  - Inject documentation server config into CLAUDE.md for specifier, architect
  - Document usage examples in constitution/workflow.md
  - Add to selftest: verify agents can query documentation

- [ ] **CLI utilities**
  - `Kiln doc-index` — index docs without launching agents
  - `Kiln doc-search <query>` — test search functionality
  - `Kiln doc-sources` — list configured documentation sources

- [ ] **Testing & validation**
  - Test with various PDF formats (scanned, native, complex layouts)
  - Test URL fetching with rate limiting
  - Test semantic search relevance
  - Verify performance with large doc collections (100+ pages)

