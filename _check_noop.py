"""Add auto-dispatch calls after handoff insertion."""
path = "src/kiln/scheduler/application/process_next_message.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

# 1. In _hand_off - after _insert_verified(ctx, target, outbound, ...)
old = """    _insert_verified(ctx, target, outbound, work_item=work_item_of(work_item))
    _queue(ctx).mark_processed(message_id)"""
new = """    _insert_verified(ctx, target, outbound, work_item=work_item_of(work_item))
    _auto_dispatch_next(ctx, work_item)
    _queue(ctx).mark_processed(message_id)"""
if old in content:
    content = content.replace(old, new, 1)
    print("_hand_off: OK")
else:
    print("_hand_off: NOT FOUND")

# 2. In _no_op - after _insert_verified(ctx, routed_target, ...)
# Find the current _no_op pattern
old = """    _insert_verified(
        ctx,
        routed_target,\"\"\"
# We need to find the end of this _insert_verified call and add _auto_dispatch after
# Let me use a different approach - find the full _no_op function

# Read the current _no_op function
import re
m = re.search(r'def _no_op\(.*?(?=def _forward|def _escalate|def _insert_verified|def _log_cycle|\Z)', content, re.DOTALL)
if m:
    noop_func = m.group()
    print(f"_no_op found: {len(noop_func)} chars")
    # Check if auto_dispatch is already in it
    if '_auto_dispatch_next' in noop_func:
        print("  Already has auto_dispatch")
    else:
        # Find the return statement and add before it
        old_noop_end = """    return CycleResult(
        NO_OP,
        message_id=message_id,
        target=ESCALATION_TARGET,
        detail=summary,
    )"""
        # Check what the current return looks like
        # It might use routed_target instead of ESCALATION_TARGET
        return_m = re.search(r'return CycleResult\([^)]+\)', noop_func)
        if return_m:
            print(f"  Return: {return_m.group()[:80]}...")
else:
    print("_no_op: function not found")

print("Done")
