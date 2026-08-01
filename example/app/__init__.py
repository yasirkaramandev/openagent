"""OpenAgent Task Board Example.

The package intentionally depends only on the Python standard library.  The
committed implementation is the fixed, production-like half of the training
scenario; ``scripts/setup_example.py`` can create an isolated buggy copy.
"""

from .models import Priority, Task, ValidationError
from .repository import TaskRepository
from .service import TaskBoardService

__all__ = [
    "Priority",
    "Task",
    "TaskBoardService",
    "TaskRepository",
    "ValidationError",
]
