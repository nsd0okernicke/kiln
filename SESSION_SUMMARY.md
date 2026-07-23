# Session Summary: Kiln System Analysis & Fixes

## Overview

This session addressed two major issues identified from the test run analysis:

1. **Subagent worktree discovery** (critical for Phase 6 implementation)
2. **Timestamp inconsistencies** (data integrity issue)

---

## Issue #1: Subagent Worktree Discovery ❌ → ✅

### What Was Wrong

The subagent worker definitions (`.claude/agents/<role>-worker.md`) were being generated in each **worktree's** `.claude/agents/` directory, but Claude Code's agent discovery only looks in the **main project's** `.claude/agents/` directory when spawning subagents via the `Agent` tool.

**Result**: Shell agents couldn't find their worker subagents, so subagent delegation didn't work.

### Root Cause

This was the empirical verification point #2 from `plan-subagent-shells.md`:

> Custom subagent discovery path/schema. No `.claude/agents/*.md` file exists anywhere in this repo today — this is new territory for Kiln. Confirm the exact frontmatter Claude Code expects and that a file placed at `<worktree>/.claude/agents/<role>-worker.md` is discovered correctly from that worktree's cwd before wiring both platform scripts.

The assumption that worktree-local agent definitions would be discovered was **incorrect**.

### The Fix (Commit 9b0c469)

**Changed both `kiln.ps1` and `kiln.sh`:**

```diff
- $agentsDir = Join-Path $WorktreePath ".claude" "agents"      # OLD: worktree location
+ $agentsDir = Join-Path $WorkingDir ".claude" "agents"        # NEW: main project location

- write_worker_agent_file "$role" "$role_worktree"             # OLD: pass worktree path
+ write_worker_agent_file "$role"                              # NEW: only pass role
```

**Rationale**:
- Shell agents run in worktrees and read `CLAUDE.md` from there ✅
- When they invoke `Agent(subagent_type: "coder-worker")`, Claude Code discovers the agent in main project's `.claude/agents/` ✅
- The spawned subagent still operates in its worktree context (correct working directory) ✅
- Agent definition location ≠ agent runtime location ✅

### Affected Files

- `bin/kiln.ps1` — Function `Write-GeneratedWorkerAgent` + 2 call sites
- `bin/kiln.sh` — Function `write_worker_agent_file` + 1 call site

### Documentation Created

- `SUBAGENT_FIX_SUMMARY.md` — Detailed explanation with architecture rationale

### Next Validation Steps

1. Run `selftest` profile to verify worker subagent dispatch works end-to-end
2. Run 4-5 complete LibraryHub cycles to confirm loop consistency
3. Check context size doesn't grow (confirm shell stays thin)

---

## Issue #2: Timestamp Inconsistencies ❌ → ✅ (Partial)

### What Was Wrong

The test run (`library-hub-testrun`) showed three categories of timestamp problems:

#### 2A: 2-Hour Timezone Offset (FIXED)

```
Message Example:
  created_at:   2026-07-23 17:22:44  (UTC)
  delivered_at: 2026-07-23 19:22:45  (UTC+2 local)
  Gap: exactly 2 hours ❌ WRONG
```

**Root Cause**: `channel.py` used SQLite's `'localtime'` modifier to set `delivered_at`, converting UTC to local time (UTC+2).

**Fix (Commit e8810d0)**:
```python
# BEFORE:
delivered_at=strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')

# AFTER:
delivered_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now', 'utc')
```

**Impact**: All new messages will have consistent UTC timestamps with Z suffix.

#### 2B: Format Inconsistency (NEEDS INVESTIGATION)

Three different formats found in test run database:

```
Format 1 (no timezone):      2026-07-23 17:22:44
Format 2 (ISO8601 no Z):     2026-07-23T19:41:25.677963
Format 3 (ISO8601 with Z):   2026-07-23T18:05:16.045272Z
```

**Status**: Partially addressed by the fix above (now uses Format 3 for `delivered_at`).

**Still Needs**: Investigation of where `created_at` comes from and ensuring it uses same format.

#### 2C: Temporal Impossibilities (NEEDS INVESTIGATION)

```
Message #14:
  created_at:   2026-07-23 21:55:00
  delivered_at: 2026-07-23 20:21:59
  Status: ❌ DELIVERED BEFORE CREATION (impossible!)
```

**Hypothesis**: Logbook may be written with hardcoded timestamps that don't match actual database insertion times.

**Status**: Requires end-to-end trace of one message lifecycle.

### Affected Files

- `kiln/mcp-server/channel.py` (line 86) — ✅ Fixed

### Documentation Created

- `TIMESTAMP_ANALYSIS.md` — Deep technical analysis of all three issues
- `TIMESTAMP_FIX_VISUAL.md` — Before/after comparison with examples
- `TESTRUN_FINDINGS_SUMMARY.md` — Comprehensive findings and recommendations

### Remaining Action Items

**Immediate (for next test run)**:
- [ ] Verify delivered_at now uses UTC with Z suffix
- [ ] Run fresh test cycle and monitor timestamps
- [ ] Confirm created_at < delivered_at for all messages

**Short-term (standardization)**:
- [ ] Document timestamp standard in `kiln/constitution.md`
- [ ] Ensure all code generating timestamps uses ISO8601 UTC with Z
- [ ] Update `kiln-handoff` skill docs to clarify created_at expectations

**Long-term (robustness)**:
- [ ] Add timestamp validation to reject impossible sequences
- [ ] Add monitoring for temporal anomalies
- [ ] Document timestamp handling in all relevant places

---

## Test Run Data

The test run (`library-hub-testrun`) provided valuable diagnostic data:

**Database Statistics**:
- Total messages: 26
- Queued: 7
- Delivered: 18
- Processed: 1

**Issues Identified**:
- 18 messages with 2-hour offset (fixed)
- Multiple format inconsistencies (identified)
- 1 message with impossible temporal sequence (needs investigation)

---

## Commits Made This Session

| Commit | Message | Status |
|--------|---------|--------|
| `9b0c469` | Fix subagent discovery: generate worker agent files in main project directory | ✅ |
| `e8810d0` | Fix timestamp timezone: use UTC for delivered_at instead of localtime | ✅ |

---

## Files Changed

### Code Changes
- `bin/kiln.ps1` (2 functions + 2 call sites)
- `bin/kiln.sh` (1 function + 1 call site)
- `kiln/mcp-server/channel.py` (1 line)

### Documentation Created
- `SUBAGENT_FIX_SUMMARY.md`
- `TIMESTAMP_ANALYSIS.md`
- `TIMESTAMP_FIX_VISUAL.md`
- `TESTRUN_FINDINGS_SUMMARY.md`
- `SESSION_SUMMARY.md` (this file)

---

## Next Steps for User

### To Test These Fixes

1. **Verify subagent fix**:
   ```bash
   # After launching a new instance, check that worker files exist:
   ls -la .claude/agents/
   # Should see: coder-worker.md, refactorer-worker.md, etc.
   ```

2. **Test with fresh run**:
   ```bash
   # Start a new test run from scratch
   ./bin/kiln.ps1 --profile dev /path/to/new/test/project
   
   # Monitor:
   # - Subagents are discovered and invoked (check debug logs)
   # - Timestamps are in UTC with Z suffix
   # - created_at < delivered_at for all messages
   # - No messages delivered before creation
   ```

3. **Validate database**:
   ```sql
   -- Query to check timestamp health:
   SELECT 
     COUNT(*) as total,
     SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END) as undelivered,
     SUM(CASE WHEN datetime(created_at) > datetime(delivered_at) THEN 1 ELSE 0 END) as impossible,
     AVG(CAST((julianday(delivered_at) - julianday(created_at)) * 86400 AS FLOAT)) as avg_delivery_seconds
   FROM messages;
   ```
   
   **Expected output** after fixes:
   ```
   total: (N)
   undelivered: (some number, ok)
   impossible: 0  ✅ (should be zero)
   avg_delivery_seconds: 0.5-2.0  ✅ (should be subseconds to few seconds)
   ```

### To Further Investigate

1. **Trace one complete message lifecycle** (created → queued → delivered → received → acked)
2. **Check if logbook timestamps match database** (investigate temporal impossibilities)
3. **Identify all places that generate timestamps** (ensure consistency)

---

## Technical References

- **Subagent Architecture**: `plan-subagent-shells.md` (section "Custom subagent discovery path/schema")
- **Message Flow**: `kiln/skills/kiln-receive/SKILL.md` and `kiln/skills/kiln-handoff/SKILL.md`
- **Timestamp Handling**: `kiln/mcp-server/channel.py` (now fixed)
- **Database Schema**: `.kiln/messages.db` (SQLite)

---

## Key Learnings

1. **Agent discovery is project-global**: Custom agent definitions must be in the main project's `.claude/agents/`, not in subdirectories or worktrees
2. **Timezone consistency matters**: Even a small mismatch (UTC vs localtime) propagates through logs and makes debugging hard
3. **Test data is diagnostic gold**: The `library-hub-testrun` revealed multiple issues that wouldn't be obvious from code review alone
4. **Format standardization prevents surprises**: Using ISO8601 UTC with Z everywhere eliminates ambiguity

---

## Session Conclusion

✅ **Two critical issues identified and fixed**:
- Subagent discovery architecture corrected
- Timezone handling standardized for new timestamps

⚠️ **One issue partially addressed**:
- Timestamp format inconsistency identified, fix applied to new timestamps
- Remaining issue: investigate why some `created_at` values use old formats

📋 **Comprehensive documentation created** for future reference and validation

**Ready for**: Fresh test run validation with the fixes applied




