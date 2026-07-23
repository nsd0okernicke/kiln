# Timestamp Inconsistency Analysis

## Problem Summary

The test run shows three distinct timestamp problems:

1. **2-hour timezone offset**: Some `created_at` timestamps are UTC, but `delivered_at` timestamps use local time (UTC+2), creating a ~2-hour gap
2. **Format inconsistencies**: Timestamps use three different formats:
   - `2026-07-23 17:22:44` (no timezone info)
   - `2026-07-23T19:41:25.677963` (ISO8601, no Z, no timezone)
   - `2026-07-23T18:05:16.045272Z` (ISO8601, with Z for UTC)
3. **Temporal impossibilities**: Some messages show `delivered_at` BEFORE `created_at` (e.g., created at 21:55:00, delivered at 20:21:59)

## Root Cause Analysis

### Issue 1: The 2-Hour Timezone Offset

**Location**: `kiln/mcp-server/channel.py` line 86

```python
"UPDATE messages SET status='delivered', delivered_at=strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') WHERE id=?",
```

The problem:
- `created_at` is inserted by Claude/shell agents, often in UTC
- `delivered_at` is set by SQLite using `'localtime'` modifier, which converts to UTC+2
- This creates a consistent 2-hour gap in the database

Example from test run:
```
Message 1:
  created_at:   2026-07-23 17:22:44  (likely UTC)
  delivered_at: 2026-07-23 19:22:45  (UTC+2 local time)
  Gap: exactly 2 hours

Message 9:
  created_at:   2026-07-23T18:05:16.045272Z  (explicitly UTC, has Z)
  delivered_at: 2026-07-23 20:05:16          (UTC+2 local time)
  Gap: exactly 2 hours
```

### Issue 2: Timestamp Format Inconsistency

The database stores `created_at` in three different formats:

1. **Simple format** (rows 1-2, 6-7, 11-16):
   ```
   2026-07-23 17:22:44
   ```
   - No timezone indicator
   - No microseconds
   - Space separator instead of T

2. **ISO8601 without Z** (rows 3-5):
   ```
   2026-07-23T19:41:25.677963
   ```
   - ISO8601 format with T
   - Microseconds included
   - NO timezone indicator (ambiguous!)

3. **ISO8601 with Z** (rows 9-10):
   ```
   2026-07-23T18:05:16.045272Z
   ```
   - ISO8601 format with T and Z
   - Microseconds included
   - Z indicates UTC (correct)

### Issue 3: Temporal Impossibilities

Row 14 in the database:
```
created_at: 2026-07-23 21:55:00
delivered_at: 2026-07-23 20:21:59
```

This message was "delivered" 1.5 hours BEFORE it was created — impossible.

This suggests someone manually inserted timestamps (possibly from the logbook) that don't match actual event times.

## Evidence: Logbook vs Database Mismatch

Looking at the logbook.md:
```
[SENT] 2026-07-23 21:55:00
To: coder
Branch: run_13
Summary: CAT-5 specification complete - Retrieve single book details by ISBN
```

This logbook entry shows `21:55:00`, but the corresponding database message shows:
```
created_at: 2026-07-23 21:55:00
delivered_at: 2026-07-23 20:21:59 (earlier!)
```

**Hypothesis**: The logbook is being written with hardcoded 5-minute increments (`21:55`, `22:00`, `22:05`, etc.), but the actual messages in the database were created earlier. Then when they're "delivered" (20:21:59), they show up as delivered before creation.

## Solutions

### Immediate Fix (Critical)

Change `channel.py` line 86 to use UTC instead of localtime:

**Before:**
```python
"UPDATE messages SET status='delivered', delivered_at=strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') WHERE id=?",
```

**After (use UTC):**
```python
"UPDATE messages SET status='delivered', delivered_at=strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc') WHERE id=?",
```

Or better, use ISO8601 format to match `created_at`:
```python
"UPDATE messages SET status='delivered', delivered_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now', 'utc') WHERE id=?",
```

### Medium-term Fix (Standardization)

1. **Establish a single timestamp format** — ISO8601 with UTC (always ending in 'Z')
   ```
   2026-07-23T19:41:25.677963Z
   ```

2. **Update all timestamp generation** to use this format:
   - `channel.py`: for `delivered_at`, `acked_at`, `processed_at`
   - Shell scripts: when writing to logbook
   - Any other code inserting timestamps

3. **Validate incoming `created_at`**: The skill should ensure it's in ISO8601 UTC format when inserting messages

### Long-term Fix (Architecture)

1. **Never use 'localtime' in SQLite** — always use UTC for all timestamps
2. **Enforce ISO8601 UTC format** across all tools and languages
3. **Document timestamp handling** in a project standard (could be in constitution.md)
4. **Add timestamp validation** — reject messages with ambiguous or future timestamps

## Testing the Fix

After applying the immediate fix:

1. Stop the current test run
2. Backup and inspect the database to understand the damage
3. Run a fresh test cycle with the fixed channel.py
4. Verify all timestamps are in UTC
5. Verify created_at < delivered_at for all messages
6. Verify timestamp format consistency

## Files to Change

- `kiln/mcp-server/channel.py` (line 86) - ✅ Fix this first
- `kiln/skills/kiln-handoff/SKILL.md` - Document UTC requirement for created_at
- `kiln/constitution.md` (new section) - Add timestamp standard

