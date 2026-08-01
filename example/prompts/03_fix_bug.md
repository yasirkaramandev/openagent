# Turn 1 — diagnose the controlled completion bug

This prompt is for a workspace created with:

```text
python scripts/setup_example.py --scenario buggy
```

Read `OPENAGENT.md`. Reproduce the failure in
`test_completing_a_task_is_idempotent_and_counts_once`: completing one task a
second time incorrectly increments `completed_count`.

Trace the behavior through repository, service, and CLI layers. State the
invariant and propose the smallest regression-safe fix. Run only local/offline
tests. **Do not edit any file in this turn.** End with `BUG_IDENTIFIED` and wait
for the same OpenAgent run to be resumed with `prompts/04_resume.md`.

