# Buggy training stage

`scripts/setup_example.py --scenario buggy` copies the committed application and
replaces the marked idempotence guard with `completion_guard.pyfrag`. The
generated copy increments `completed_count` every time an already-completed task
is completed again. Its regression test is expected to fail until the agent
repairs the generated copy.

The repository's committed `app/` is never modified by scenario setup.

