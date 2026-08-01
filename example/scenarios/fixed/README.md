# Fixed reference stage

The fixed stage uses `example/app/repository.py` exactly as committed. Its marked
completion guard returns an unchanged result for an already-completed task, so
`completed_count` remains stable and the complete test suite passes.

