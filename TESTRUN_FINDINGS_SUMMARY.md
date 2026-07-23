# Test Run Analysis Summary

## Issues Found

### 1. ✅ FIXED: Timestamp Timezone Mismatch (Critical)

**Problem**: Timestamps in the database showed a consistent ~2-hour offset between `created_at` and `delivered_at`.

**Root Cause**: `channel.py` was using SQLite's `'localtime'` modifier to set `delivered_at`, converting UTC to UTC+2 local time, while `created_at` remained in UTC.

**Evidence**:
```
Message 1:
  created_at:   2026-07-23 17:22:44  (UTC)
  delivered_at: 2026-07-23 19:22:45  (UTC+2)
  Gap: exactly 2 hours
```

**Fix Applied** (commit e8810d0):
- Changed `kiln/mcp-server/channel.py` line 86
- From: `strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')`
- To: `strftime('%Y-%m-%dT%H:%M:%SZ', 'now', 'utc')`
- Now uses UTC for all timestamps with consistent ISO8601 format

### 2. ⚠️ FORMAT INCONSISTENCY: Multiple Timestamp Formats (Needs Investigation)

**Problem**: The database contains timestamps in three different formats:

1. **Simple format** (no timezone info):
   ```
   2026-07-23 17:22:44
   ```

2. **ISO8601 without Z** (ambiguous):
   ```
   2026-07-23T19:41:25.677963
   ```

3. **ISO8601 with Z** (UTC indicator):
   ```
   2026-07-23T18:05:16.045272Z
   ```

**Current Status**: 
- The channel.py fix now uses format #3 (ISO8601 with Z) for `delivered_at`
- The `created_at` format still needs investigation — likely coming from different sources (Claude prompt, shell scripts, Python code)

**Recommendation**: 
- All timestamp insertion code should standardize on ISO8601 with Z
- The kiln-handoff skill INSERT statement should explicitly set `created_at` to ensure format consistency

### 3. ❓ UNCLEAR: Impossible Temporal Sequences

**Problem**: Some messages show `delivered_at` BEFORE `created_at`:

```
Message (id: 8593df3...):
  created_at:   2026-07-23 21:55:00
  delivered_at: 2026-07-23 20:21:59
  Impossible gap: -1.5 hours (delivered BEFORE creation!)
```

**Hypothesis**: 
- The logbook is being written with hardcoded 5-minute increments (21:55, 22:00, 22:05, etc.)
- But actual message delivery happens earlier in real time
- The logbook entries may not be synchronized with actual message creation times

**Next Steps**:
- Trace one message through the complete cycle (creation → logbook write → delivery)
- Verify that logbook timestamps match database creation times
- Check if there's a race condition between message insertion and logbook writing

---

## Test Run Statistics

**Test Configuration**: `library-hub-testrun`
- Duration: Multiple cycles (user stopped after "a few cycles")
- Agents: Specifier, Coder, Refactorer, Architect
- Total messages: 26 in database
- Message delivery states:
  - Queued: 7
  - Delivered: 18
  - Processed: 1

**Observations**:
- Early messages (rows 1-7) show consistent 2-hour offset (now fixed)
- Later messages (rows 8+) show various formats and some impossible timestamps
- Specifier seems to be batch-sending messages with hardcoded timestamps

---

## Recommended Next Steps

### Immediate (Test with Fresh Run)

1. **Verify the channel.py fix**:
   ```bash
   # Check that delivered_at now uses UTC with Z suffix
   sqlite3 .kiln/messages.db "SELECT delivered_at FROM messages LIMIT 5"
   # Should output: 2026-07-23T19:22:45Z, etc.
   ```

2. **Run a fresh test cycle**:
   - Stop the current testrun
   - Start a new test with the fixed channel.py
   - Monitor 2-3 complete cycles (specifier → coder → refactorer → architect → specifier)
   - Verify all timestamps are UTC and in ISO8601 format
   - Confirm created_at < delivered_at for all messages

### Short-term (Standardization)

1. **Establish timestamp standard**:
   - Add to `kiln/constitution.md` or new document:
     ```markdown
     ## Timestamp Standard
     - Format: ISO8601 with UTC timezone indicator (Z suffix)
     - Example: 2026-07-23T19:22:45.123456Z
     - Always UTC — never use local time
     - All timestamps (created_at, delivered_at, acked_at, processed_at) must follow this format
     ```

2. **Update kiln-handoff skill**:
   - Document that `created_at` should NOT be manually set in the INSERT
   - SQLite should handle it with a proper DEFAULT clause (if not already)
   - Or, ensure Claude explicitly generates ISO8601 UTC timestamps when inserting

3. **Audit other timestamp sources**:
   - Check shell scripts for any hardcoded timestamps
   - Review any Python code that generates timestamps
   - Verify logbook writing doesn't use local system time

### Long-term (Robustness)

1. **Add timestamp validation**:
   - Reject messages with `delivered_at < created_at`
   - Reject messages with future timestamps (> current UTC)
   - Validate format compliance (ISO8601 with Z)

2. **Documentation**:
   - Add section to CLAUDE.md templates about timestamp expectations
   - Update README.md with timestamp handling explanation

3. **Monitoring**:
   - Add query to logbook or dashboard to detect timestamp anomalies
   - Alert on messages delivered out of order or with impossible timestamps

---

## Files Modified

- ✅ `kiln/mcp-server/channel.py` (commit e8810d0) — Fixed UTC timezone issue
- 📄 `TIMESTAMP_ANALYSIS.md` — Detailed technical analysis
- 📄 `TESTRUN_FINDINGS_SUMMARY.md` — This file

## Related Issues

- Subagent worktree discovery fix (commit 9b0c469) — separate, unrelated issue
- Timestamp format inconsistency — deeper issue requiring more investigation

---

## Validation Checklist for Next Test Run

- [ ] Fresh database (no old messages with mixed formats)
- [ ] channel.py using UTC for delivered_at
- [ ] First message created_at timestamp captured
- [ ] All delivered_at timestamps show as ISO8601 with Z
- [ ] created_at < delivered_at for all delivered messages
- [ ] No messages with future timestamps
- [ ] Logbook entries match database timestamps
- [ ] 3+ complete cycles without timestamp anomalies

