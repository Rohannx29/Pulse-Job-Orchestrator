"""Multi-Parent Directed Acyclic Graph (DAG) Workflow Engine.

Provides arbitrary dependency topology support:
- Multi-parent fan-in (a job waits for N parents to finish).
- Multi-child fan-out (a job completion unblocks M downstream tasks).
- Real-time cycle detection via Depth-First Search (DFS) preventing deadlocks.
- Non-blocking join resolution: dispatches child only when ALL parents complete.
- Recursive cascading failure propagation: if any prerequisite permanently fails (DLQ),
  all downstream dependents transition to DEAD_LETTER with detailed provenance.
- Topological DAG inspection & graph extraction for operator visualization.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.dispatch import dispatch_job
from app.events import get_job_org_id, publish_event
from app.models import DeadLetterJob, Job, JobDependency, JobStatus, Queue
from app.outbox import record_event_outbox

logger = logging.getLogger(__name__)


def detect_cycle_dfs(
    db: Session,
    proposed_child_id: str,
    proposed_parent_ids: List[str],
) -> Optional[List[str]]:
    """Detects whether adding edges from `proposed_parent_ids` -> `proposed_child_id`
    would create a directed cycle in the job dependency graph.
    
    Returns the cycle path as a list of job IDs if a cycle is detected, or None if acyclic.
    """
    parent_set = set(proposed_parent_ids)

    # 1. Self-dependency check
    if proposed_child_id in parent_set:
        return [proposed_child_id, proposed_child_id]

    # 2. DFS traversal from proposed_child_id following downstream child edges.
    # If any node in parent_set is reachable from proposed_child_id, adding
    # parent -> proposed_child_id would close a directed loop.
    visited: Set[str] = set()
    parent_tracker: Dict[str, str] = {}
    queue = deque([proposed_child_id])
    visited.add(proposed_child_id)

    target_parent = None
    while queue:
        curr_id = queue.popleft()

        if curr_id in parent_set:
            target_parent = curr_id
            break

        # Query all downstream children of curr_id
        # Join table edges
        dep_children = (
            db.query(JobDependency.child_job_id)
            .filter(JobDependency.parent_job_id == curr_id)
            .all()
        )
        child_ids = [r[0] for r in dep_children]

        # Also check legacy single-parent depends_on_job_id
        legacy_children = (
            db.query(Job.id)
            .filter(Job.depends_on_job_id == curr_id)
            .all()
        )
        child_ids.extend([r[0] for r in legacy_children])

        for nxt_id in set(child_ids):
            if nxt_id not in visited:
                visited.add(nxt_id)
                parent_tracker[nxt_id] = curr_id
                queue.append(nxt_id)

    if target_parent is not None:
        # Reconstruct path
        path = [target_parent]
        curr = target_parent
        while curr in parent_tracker:
            curr = parent_tracker[curr]
            path.append(curr)
        path.reverse()
        # Close the cycle loop
        path.append(proposed_child_id)
        return path

    return None


def link_dag_dependencies(
    db: Session,
    child_job: Job,
    parent_job_ids: List[str],
) -> JobStatus:
    """Validates, cycle-checks, and links multi-parent dependencies for a job.
    
    Returns the determined initial JobStatus for the child (QUEUED, BLOCKED, or DEAD_LETTER).
    """
    clean_parent_ids = list(dict.fromkeys(pid for pid in parent_job_ids if pid))
    if not clean_parent_ids:
        return JobStatus.QUEUED

    if not child_job.id:
        import uuid
        child_job.id = str(uuid.uuid4())

    # 1. Enforce cycle detection
    cycle = detect_cycle_dfs(db, child_job.id, clean_parent_ids)
    if cycle:
        cycle_str = " -> ".join(str(node) for node in cycle)
        raise ValueError(f"Cyclic dependency detected: {cycle_str}")

    # 2. Verify all parent jobs exist
    parents = db.query(Job).filter(Job.id.in_(clean_parent_ids)).all()
    found_ids = {p.id for p in parents}
    missing_ids = set(clean_parent_ids) - found_ids
    if missing_ids:
        raise ValueError(f"Referenced prerequisite parent jobs do not exist: {list(missing_ids)}")

    # 3. Insert JobDependency rows
    for pid in clean_parent_ids:
        dep = JobDependency(parent_job_id=pid, child_job_id=child_job.id)
        db.add(dep)

    # Set legacy field for backward compatibility
    if not child_job.depends_on_job_id:
        child_job.depends_on_job_id = clean_parent_ids[0]

    # 4. Determine child status
    # If ANY parent is DEAD_LETTER, fail fast
    dead_parents = [p for p in parents if p.status == JobStatus.DEAD_LETTER]
    if dead_parents:
        reason = f"Upstream prerequisite {dead_parents[0].id} is already in DEAD_LETTER"
        child_job.status = JobStatus.DEAD_LETTER
        db.add(
            DeadLetterJob(
                job_id=child_job.id,
                reason=reason,
                original_payload=child_job.payload,
                retry_count_at_failure=0,
            )
        )
        return JobStatus.DEAD_LETTER

    # If ANY parent is not COMPLETED, block child
    uncompleted = [p for p in parents if p.status != JobStatus.COMPLETED]
    if uncompleted:
        child_job.status = JobStatus.BLOCKED
        return JobStatus.BLOCKED

    # All parents already completed
    child_job.status = JobStatus.QUEUED
    return JobStatus.QUEUED


def release_dag_dependents(db: Session, completed_job: Job, dispatcher=None) -> List[Job]:
    """Evaluates all downstream dependents when a parent finishes.
    Only dispatches a child job when ALL its prerequisite parents have completed."""
    # Find all direct children
    dep_rows = (
        db.query(JobDependency.child_job_id)
        .filter(JobDependency.parent_job_id == completed_job.id)
        .all()
    )
    child_ids = {r[0] for r in dep_rows}

    # Also check legacy single-parent dependents
    legacy_rows = (
        db.query(Job.id)
        .filter(Job.depends_on_job_id == completed_job.id)
        .all()
    )
    for r in legacy_rows:
        child_ids.add(r[0])

    if not child_ids:
        return []

    blocked_children = (
        db.query(Job)
        .filter(Job.id.in_(list(child_ids)), Job.status == JobStatus.BLOCKED)
        .all()
    )

    unblocked_jobs: List[Job] = []
    for child in blocked_children:
        # Check all parents of this child
        parent_rows = (
            db.query(JobDependency.parent_job_id)
            .filter(JobDependency.child_job_id == child.id)
            .all()
        )
        required_parent_ids = {r[0] for r in parent_rows}
        if child.depends_on_job_id:
            required_parent_ids.add(child.depends_on_job_id)

        # Check if any parent is NOT completed
        uncompleted_count = (
            db.query(Job)
            .filter(
                Job.id.in_(list(required_parent_ids)),
                Job.status != JobStatus.COMPLETED,
            )
            .count()
        )

        if uncompleted_count == 0:
            # Atomic conditional update: transition BLOCKED -> QUEUED.
            # Only the single winning thread where rowcount == 1 creates the dispatch event.
            updated_count = (
                db.query(Job)
                .filter(
                    Job.id == child.id,
                    Job.status == JobStatus.BLOCKED,
                )
                .update({Job.status: JobStatus.QUEUED}, synchronize_session="fetch")
            )

            if updated_count == 1:
                # Winner! Dispatch child job through caller-owned session
                run_dispatch = dispatcher if dispatcher is not None else dispatch_job
                try:
                    run_dispatch(child, db=db)
                except TypeError:
                    run_dispatch(child)
                unblocked_jobs.append(child)

                logger.info(
                    "All %d prerequisites completed. Unblocked dependent job %s (atomic winner)",
                    len(required_parent_ids),
                    child.id,
                )
            else:
                logger.debug(
                    "Child job %s already unblocked by a concurrent prerequisite completion (lost race)",
                    child.id,
                )
        else:
            logger.debug(
                "Child job %s waiting on %d remaining prerequisites",
                child.id,
                uncompleted_count,
            )

    if unblocked_jobs:
        db.commit()

    return unblocked_jobs


def cascade_fail_dag_dependents(db: Session, dead_job: Job, reason: str) -> List[Job]:
    """Recursively marks all downstream DAG dependents as DEAD_LETTER when an upstream
    prerequisite permanently fails, because their dependency chain can never be satisfied."""
    dep_rows = (
        db.query(JobDependency.child_job_id)
        .filter(JobDependency.parent_job_id == dead_job.id)
        .all()
    )
    child_ids = {r[0] for r in dep_rows}

    legacy_rows = (
        db.query(Job.id)
        .filter(Job.depends_on_job_id == dead_job.id)
        .all()
    )
    for r in legacy_rows:
        child_ids.add(r[0])

    if not child_ids:
        return []

    blocked_children = (
        db.query(Job)
        .filter(Job.id.in_(list(child_ids)), Job.status == JobStatus.BLOCKED)
        .all()
    )

    cascaded_jobs: List[Job] = []
    for child in blocked_children:
        child.status = JobStatus.DEAD_LETTER
        cascade_reason = f"Upstream prerequisite {dead_job.id} failed: {reason}"
        db.add(
            DeadLetterJob(
                job_id=child.id,
                reason=cascade_reason,
                original_payload=child.payload,
                retry_count_at_failure=0,
            )
        )
        cascaded_jobs.append(child)

        dep_org_id = get_job_org_id(db, child)
        if dep_org_id:
            record_event_outbox(db, dep_org_id, "job.dead_letter", {
                "job_id": child.id,
                "queue_id": child.queue_id,
                "reason": cascade_reason,
                "status": JobStatus.DEAD_LETTER.value,
            })
        logger.warning("Cascading DLQ from %s to blocked dependent %s", dead_job.id, child.id)

    if cascaded_jobs:
        db.commit()
        for child in cascaded_jobs:
            cascade_fail_dag_dependents(db, child, f"upstream prerequisite {dead_job.id} failed")

    return cascaded_jobs


def build_dag_graph(
    db: Session,
    root_job_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Constructs a complete topological node-and-edge graph for frontend DAG rendering."""
    job_ids: Set[str] = set()

    if root_job_id:
        # Traverse both upstream (ancestors) and downstream (descendants)
        to_visit = deque([root_job_id])
        job_ids.add(root_job_id)

        while to_visit:
            curr = to_visit.popleft()
            # Ancestors (parents)
            parents = (
                db.query(JobDependency.parent_job_id)
                .filter(JobDependency.child_job_id == curr)
                .all()
            )
            for p in parents:
                if p[0] not in job_ids:
                    job_ids.add(p[0])
                    to_visit.append(p[0])

            # Check legacy parent
            leg_parent = db.query(Job.depends_on_job_id).filter(Job.id == curr).first()
            if leg_parent and leg_parent[0] and leg_parent[0] not in job_ids:
                job_ids.add(leg_parent[0])
                to_visit.append(leg_parent[0])

            # Descendants (children)
            children = (
                db.query(JobDependency.child_job_id)
                .filter(JobDependency.parent_job_id == curr)
                .all()
            )
            for c in children:
                if c[0] not in job_ids:
                    job_ids.add(c[0])
                    to_visit.append(c[0])

            leg_children = db.query(Job.id).filter(Job.depends_on_job_id == curr).all()
            for c in leg_children:
                if c[0] not in job_ids:
                    job_ids.add(c[0])
                    to_visit.append(c[0])

        jobs = db.query(Job).filter(Job.id.in_(list(job_ids))).all()
    elif project_id:
        jobs = (
            db.query(Job)
            .join(Queue, Job.queue_id == Queue.id)
            .filter(Queue.project_id == project_id)
            .all()
        )
        job_ids = {j.id for j in jobs}
    else:
        jobs = []

    # Build nodes
    nodes = []
    for j in jobs:
        nodes.append({
            "id": j.id,
            "name": j.name,
            "queue_id": j.queue_id,
            "queue_name": j.queue.name if j.queue else "",
            "status": j.status,
            "priority": j.priority,
            "current_retry_count": j.current_retry_count,
            "max_retries": j.max_retries,
            "parent_job_ids": j.parent_job_ids,
            "run_at": j.run_at,
            "created_at": j.created_at,
        })

    # Build edges
    edges = []
    if job_ids:
        deps = (
            db.query(JobDependency)
            .filter(
                JobDependency.parent_job_id.in_(list(job_ids)),
                JobDependency.child_job_id.in_(list(job_ids)),
            )
            .all()
        )
        seen_edges = set()
        for d in deps:
            edge_key = (d.parent_job_id, d.child_job_id)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({"parent_id": d.parent_job_id, "child_id": d.child_job_id})

        # Add legacy edges if not already in seen_edges
        for j in jobs:
            if j.depends_on_job_id and j.depends_on_job_id in job_ids:
                edge_key = (j.depends_on_job_id, j.id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({"parent_id": j.depends_on_job_id, "child_id": j.id})

    return {
        "root_job_id": root_job_id,
        "nodes": nodes,
        "edges": edges,
        "is_acyclic": True,
    }
