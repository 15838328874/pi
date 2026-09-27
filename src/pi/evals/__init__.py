"""pi-py eval: task sets, scoring, and A/B comparison (the data flywheel).

The eval package is a CONSUMER of the runtime's trajectory (P1): it runs tasks,
scores them, and reports. The loop knows nothing about it.
"""

from pi.evals.load import load_task, load_task_set
from pi.evals.report import diff, format_report, make_task_report, summarize
from pi.evals.runner import run_task
from pi.evals.scorers import score

__all__ = [
    "load_task",
    "load_task_set",
    "diff",
    "format_report",
    "make_task_report",
    "summarize",
    "run_task",
    "score",
]
