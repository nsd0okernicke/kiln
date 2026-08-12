# Kiln — containers and message transport

**Question:** should Kiln run its agents inside containers, and should the SQLite message
queue be replaced with something like RabbitMQ?

**Verdict:** containers yes — eventually, and at one specific seam. RabbitMQ no, not for the
reason it appears to help. The two are separable decisions and should be decided separately.

---

## 1. The two axes are independent

They get bundled because "containers + message broker" is a familiar shape, but in Kiln they
touch different mechanisms:

| Axis | What it changes | Actually motivated by |
|---|---|---|
| **Transport** — SQLite → AMQP | how a *pointer* reaches the next role | multi-host delivery |
| **Execution isolation** — worktree → container | what an agent can reach on the machine | the permission posture |

Only the second one addresses a problem Kiln currently has.

---

## 2. Why RabbitMQ is the easy half of the problem

### Kiln does not move work through the queue — it moves a commit hash

The message carries `Sender`, `Handoff`, `Branch`, `Commit` and prose. The **code** moves
because every role's worktree shares one git object store, so `git merge <hash>` in the
receiving worktree resolves a hash produced in a different worktree. The object store is the
real transport; the queue is a pointer channel.

Swapping SQLite for AMQP upgrades the pointer channel while the thing it points into is still
a single local `.git`. Two containers with separate filesystems cannot merge each other's
hashes no matter how good the broker is.

**So the first question is not "which broker" but "how does code get between containers":**
- shared volume holding the repo (preserves merge-by-hash — see §4), or
- real remotes with push/fetch (note `workspace.PRE_PUSH_HOOK` currently *forbids* pushing
  Kiln sub-branches, deliberately: "orchestration-internal and ephemeral").

Answer that first. The transport decision follows from it, and may well not need to change.

### SQLite is nowhere near its limits here

Roughly 34 messages over 8+ cycles, WAL enabled, 2s poll interval, 4-6 consumers. That is
orders of magnitude below where SQLite becomes interesting. A broker would buy no throughput.
(README does list concurrent access by four schedulers as untested — that is worth *testing*,
not worth replacing.)

### What AMQP would genuinely buy

Two real wins, both mapping onto items already in the backlog:

- **Ack / redelivery** structurally fixes the stuck-in-`processing` bug (backlog issue 9): a
  dead consumer's unacked message returns to the queue automatically, instead of being
  silently lost as it is today.
- **Dead-letter queues** give escalations (backlog issue 5) a real destination rather than a
  `Kiln-Escalation: true` flag on an ordinary message.

Both are solvable in SQLite with far less machinery.

### What AMQP would cost — the decisive point

**A queue is not a queryable store.** Once a message is consumed it is gone. Kiln's entire
human-facing surface depends on querying message *history*:

- `dashboard.py` → `db.recent_messages`, `db.count_queued_by_role` (activity pane, escalation
  list, queue depth per role)
- `inbox.py` → reads and classifies past messages
- the `kiln-db` MCP server → exposes raw SQL `query` to agents
- `/kiln-handoff` → `INSERT` followed by a `SELECT` verification step, specifically because a
  silent insert failure was observed in practice

With AMQP you would need RabbitMQ **plus** a database to keep those working — more moving
parts, not fewer. You would also give up "the whole swarm state is one file openable in any
SQLite browser", which is a meaningful part of why the system is debuggable today.

And zero-infrastructure is currently a genuine selling point: `kiln --init` and go. A broker
is something to run, secure, version and keep alive before any agent starts.

---

## 3. Containers: the strong argument is security, not scale

### What is genuinely compelling

The permission posture is the case for containers, and it is a strong one. Every backend runs
fully unrestricted by design:

- codex — `--dangerously-bypass-approvals-and-sandbox`
- grok — `--always-approve`
- claude — `bypassPermissions`
- copilot — `--allow-all`

That is full rights on the developer's machine, and the README's own security note already
says "consider running Kiln in a sandbox/VM for untrusted code or high-security scenarios."
Containers are the honest answer to that note.

Secondary benefits:
- **Reproducible per-role toolchains.** `engineering.md`'s "acquire language tooling on
  startup" becomes an image layer instead of a runtime gamble repeated every cycle.
- **Sidesteps the Windows symlink limitation** (`WinError 1314` / Developer Mode), since bind
  mounts replace the `.kiln` symlink.

### What breaks

- **The pane UX, which is Kiln's whole observability story.** One WezTerm pane per role
  streaming a live worker is the product's most distinctive feature. Containerizing whole
  roles turns that into log tailing, and would force the dashboard to become the primary
  interface rather than a third tab.
- **Auth per container.** There is already a scar here: the codex adapter deliberately reuses
  the *ambient* authenticated `CODEX_HOME` because a per-role isolated one has no `auth.json`
  and 401s. Containers multiply that problem across four CLIs, each with its own credential
  location and refresh behavior.
- **Windows + bind-mounted git.** Docker Desktop / WSL2 over a bind mount performs git
  operations slowly, and Kiln performs a great many of them (merge, squash, status, rev-list
  per cycle per role).

---

## 4. Recommendation: containerize the worker, not the role

**The seam already exists and is exactly the right one.**

Each worker invocation is already a one-shot subprocess against a directory:
- `build_command(...)` is pure and returns argv (`adapters/claude_adapter.py:72-112`)
- `run_worker` does `subprocess.Popen(command, cwd=str(cwd), ...)`
  (`adapters/claude_adapter.py:275-285`), with a `threading.Timer` watchdog

Wrapping that single call is close to mechanical:

    ["docker", "run", "--rm",
     "-v", f"{repo}:/repo",
     "-w", f"/repo/.worktrees/{role}",
     image] + command

**What stays exactly as it is:** the scheduler, the panes (stdout still streams into the
WezTerm pane), SQLite, the git worktree topology, merge-by-hash, the dashboard, the inbox.

**What you gain:** blast-radius containment — the agent cannot reach `~/.ssh`, other projects,
or anything outside the repo — plus per-role images and pinned toolchains.

**The isolation is deliberately partial:** containers still share the repo volume, which is
precisely what preserves merge-by-hash. Given the risk actually being mitigated is "an agent
running with bypassed permissions does something unbounded on my machine", repo-scoped
containment captures most of the value at a small fraction of the cost.

### Sequencing if this is pursued

1. Container wrapper behind an opt-in per-role profile field (e.g. `"container": "<image>"`),
   defaulting off — no behavior change for existing profiles.
2. Solve credential mounting per backend. Start with one backend (claude is the
   most-exercised); codex's ambient-`CODEX_HOME` workaround will need rethinking.
3. Measure git performance over the bind mount on Windows before committing further.
4. Only then consider whether whole-role containers (and therefore a different transport) are
   worth the loss of the pane model.

---

## 5. Rejected alternatives (recorded so they are not re-litigated)

**Replace SQLite with RabbitMQ now.** Rejected: solves a throughput problem that does not
exist, does not address the git topology that actually blocks distribution, removes the
queryable history that the dashboard, inbox and `kiln-db` MCP server all depend on, and
requires running a broker *plus* a database to restore what SQLite already provides alone.

**Introduce a queue abstraction/port speculatively.** Rejected for now: with only one
implementation it buys nothing and complicates backlog issues 1, 5 and 9, all of which want
to touch the schema directly. Introduce the port when a second implementation is real — the
natural trigger is a genuine multi-host requirement.

**Containerize whole roles (one long-lived container per role).** Deferred: costs the pane
observability model, multiplies the credential problem, and forces the git-topology question
immediately. Revisit only if worker-level containers prove insufficient.

---

## 6. Open questions

1. Is the driver here **security** (untrusted code, unbounded agents) or **scale**
   (parallel roles across hosts)? They lead to different designs, and only the first is a
   problem Kiln has today.
2. Is the per-pane live view something to preserve, or is the dashboard intended to become
   the primary interface over time? Whole-role containers are only viable under the latter.
3. Windows-first, or is Linux/WSL2 an acceptable requirement for containerized runs?
