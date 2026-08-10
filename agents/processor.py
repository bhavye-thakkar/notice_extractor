"""The multi-agent runner: one agent per (newspaper, edition, day).

Each agent is independent - its own extractor instance, its own downloader,
its own browser session where one is needed - so Divya Bhaskar signing in has
no bearing on Sandesh downloading pages, and an agent that dies takes only its
own edition with it.  Agents run in parallel and report in COMPLETION order,
so a fast paper's notices reach the gallery while a slow one is still working.

This lives outside the GUI because the same pipeline has to run without one:
`python -m notice_extractor.main --headless` uses exactly this function.

Concurrency, measured rather than guessed (see tools/benchmark_pipeline.py):
  * agent count is bounded by sockets, not cores - the CPU-heavy detect()
    calls are gated separately by core's detect gate,
  * OpenCV gets a fixed modest thread pool: dividing it by the agent count
    starves each detector to one thread and makes every page ~2.5x slower.
"""

from __future__ import annotations

import concurrent.futures
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .. import core
from ..utils import logger as run_logger

#: (extractor class, edition, day, url) - what the GUI's job builder produces.
Job = Tuple[type, str, "core.date", str]


@dataclass
class RunSummary:
    """What a whole run produced (the GUI shows it, a CLI prints it)."""
    total: int = 0
    pages: int = 0
    per_paper: Dict[str, int] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    seconds: float = 0.0
    cancelled: bool = False

    def text(self) -> str:
        summary = (f"Finished: {self.total} Public Notice"
                   f"{'s' if self.total != 1 else ''} total")
        breakdown = "   ".join(f"{name}: {n}"
                               for name, n in sorted(self.per_paper.items()))
        if breakdown:
            summary += f"   [{breakdown}]"
        if self.skipped:
            summary += f"   (skipped: {len(self.skipped)})"
        return summary


def run_jobs(jobs: Sequence[Job], reporter: "core.ProgressReporter", *,
             broad: bool = False) -> RunSummary:
    """Run every job into one gallery and report per-newspaper totals.

    Returns when every agent has finished, been skipped, or timed out; the
    notices themselves were already streamed to `reporter` as they appeared.
    """
    summary = RunSummary()
    started = time.perf_counter()
    workers = core.resolve_job_workers(len(jobs))
    try:
        core.cv2.setNumThreads(core.CV2_THREADS_PER_DETECT)
    except Exception:
        pass

    def _run_one(job: Job, section_title: str):
        """One edition agent: download + detect, isolated from every other
        agent.  Retries transient failures; credentials are never retried."""
        cls, edition, day, url = job
        label = f"{cls.display_name} {day.strftime('%d-%m')}"
        agent_started = time.perf_counter()
        last_error: Optional[Exception] = None

        for attempt in range(1, core.AGENT_RETRIES + 2):
            reporter.check_cancel()
            buffered = core.BufferedJobReporter(reporter, label, section_title)
            extractor = cls(broad=broad)
            extractor.current_issue_date = day.isoformat()
            try:
                extractor.extract_all([(edition, url)], buffered,
                                      finalize=False, start_result_id=0)
                elapsed = time.perf_counter() - agent_started
                reporter.log(f"[Agent] {label} -> Completed in {elapsed:.1f}s "
                             f"({len(buffered.collected)} notice(s))", "dim")
                return buffered.collected
            except core.ExtractionCancelled:
                raise
            except core.ExtractionError as exc:
                # Credentials will not fix themselves - do not retry.
                if str(exc).startswith("AUTH:") or attempt > core.AGENT_RETRIES:
                    raise
                last_error = exc
            except Exception as exc:
                if attempt > core.AGENT_RETRIES:
                    raise
                last_error = exc
            reporter.log(f"[Agent] {label} -> attempt {attempt} failed "
                         f"({last_error}); retrying", "warn")
        raise core.ExtractionError(str(last_error))

    try:
        runnable = [(index, job) for index, job in enumerate(jobs, 1)
                    if job[3]]
        for index, (cls, edition, day, _url) in [
                (i, j) for i, j in enumerate(jobs, 1) if not j[3]]:
            stamp = day.strftime("%d-%m-%Y")
            reporter.log(f"##  [{index}/{len(jobs)}]  {cls.display_name} "
                         f"- {edition} - {stamp}: skipped (needs its reader "
                         "URL pasted once).", "warn")
            summary.skipped.append(f"{cls.display_name} {stamp}")

        if workers > 1 and len(runnable) > 1:
            reporter.log(f"[Agents] Starting {min(workers, len(runnable))} "
                         f"edition agents in parallel "
                         f"({core.DETECT_CONCURRENCY} detectors x "
                         f"{core.CV2_THREADS_PER_DETECT} cv2 threads)...",
                         "info")
        reporter.progress(0, len(runnable))

        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="job")
        futures: Dict["concurrent.futures.Future", Tuple[Job, str]] = {}
        # Every section heading is created up front, in submission order, so
        # the gallery's page order stays predictable even though notices
        # arrive from all agents at once.
        titles = {}
        for index, job in runnable:
            cls, edition, day, _url = job
            titles[index] = (f"{cls.display_name}  -  {edition}  -  "
                             f"{day.strftime('%d-%m-%Y')}")
            reporter.heading(titles[index])
        try:
            for index, job in runnable:
                futures[pool.submit(_run_one, job, titles[index])] = \
                    (job, titles[index])

            # Handled in COMPLETION order: a paper that finishes early reports
            # right away instead of waiting behind a slow one.
            done = 0
            deadline = time.monotonic() + core.AGENT_TIMEOUT_SECONDS
            pending = set(futures)
            while pending:
                reporter.check_cancel()
                finished, pending = concurrent.futures.wait(
                    pending, timeout=1.0, return_when="FIRST_COMPLETED")
                if not finished:
                    if time.monotonic() > deadline:
                        for future in pending:
                            _job, title = futures[future]
                            future.cancel()
                            reporter.log(
                                f"[Agent] {title} -> TIMEOUT after "
                                f"{core.AGENT_TIMEOUT_SECONDS}s, abandoned",
                                "error")
                            summary.skipped.append(title)
                        break
                    continue

                for future in finished:
                    job, title = futures[future]
                    cls = job[0]
                    done += 1
                    reporter.phase(f"[{done}/{len(runnable)}] "
                                   f"{cls.display_name}")
                    try:
                        collected = future.result()
                    except core.ExtractionCancelled:
                        raise
                    except core.ExtractionError as exc:
                        reporter.log(f"[Agent] {title} -> failed: {exc}",
                                     "error")
                        summary.skipped.append(title)
                        reporter.progress(done, len(runnable))
                        continue
                    except Exception:
                        reporter.log("Unexpected error:\n"
                                     + traceback.format_exc(), "error")
                        summary.skipped.append(title)
                        reporter.progress(done, len(runnable))
                        continue
                    found = len(collected)
                    summary.total += found
                    summary.per_paper[cls.display_name] = \
                        summary.per_paper.get(cls.display_name, 0) + found
                    reporter.log(f"##  {title}  ->  {found} notice(s)",
                                 "success" if found else "dim")
                    reporter.progress(done, len(runnable))
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False)

        summary.seconds = time.perf_counter() - started
        reporter.separator()
        text = summary.text()
        reporter.log(text, "success" if summary.total else "info")
        for name, n in sorted(summary.per_paper.items()):
            reporter.log(f"    {name}: {n} notice(s)", "info")
        reporter.log(f"    wall clock: {summary.seconds:.1f}s", "dim")
        reporter.done(text)
    except core.ExtractionCancelled:
        summary.cancelled = True
        reporter.log("Extraction cancelled by user.", "warn")
        reporter.cancelled()
    except Exception:
        reporter.log("Unexpected error:\n" + traceback.format_exc(), "error")
        reporter.failed("An unexpected error occurred - see the log.")
    finally:
        summary.seconds = summary.seconds or (time.perf_counter() - started)
        try:
            core.cv2.setNumThreads(-1)      # restore OpenCV's default
        except Exception:
            pass
        run_logger.log(f"run finished: {summary.text()} "
                       f"in {summary.seconds:.1f}s", "info")
    return summary
