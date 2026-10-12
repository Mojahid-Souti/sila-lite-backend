"""Sila document-processing worker.

Drains the ``kb_jobs`` queue out-of-process so heavy parse/embed work never runs
on the API event loop. Started as its own container (``python -m sila.worker``).
"""
