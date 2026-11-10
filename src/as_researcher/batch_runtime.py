"""Single-writer batch recovery, outside Agent.reply.

Resume starts only pristine not_run trials. A started request may have incurred
cost even if no receipt exists: never retry it implicitly or promise exactly-once.
"""

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .benchmark import digest, load_bound_batch, load_corpus, source_digest
from .review import fingerprint


def atomic_json(path: Path, value: dict | list) -> None:
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def batch_lock(batch: Path, name: str = "execution.lock"):
    """OS advisory lock: a crashed owner releases it; file existence is not a lock."""
    with (batch / name).open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Batch is already executing; do not remove its lock file") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def model_binding(mode: str, root: Path) -> dict:
    """Match live_model alias precedence without mutating env or persisting secrets."""
    if mode == "fixture":
        return {"kind": "scripted", "configuration_sha256": digest(b"ScriptedModel")}
    from dotenv import dotenv_values

    settings = {**dotenv_values(root / ".env", encoding="utf-8-sig"), **os.environ}
    values = {
        "model": settings.get("MODEL_NAME") or settings.get("LLM_MODEL"),
        "endpoint": settings.get("MODEL_BASE_URL") or settings.get("LLM_BASE_URL"),
    }
    return {"kind": "openai-compatible", "configuration_sha256": fingerprint(values)}


def seal_execution(batch: Path, plan: dict, root: Path) -> None:
    atomic_json(
        batch / "execution.json",
        {
            "schema_version": 1,
            "plan_sha256": digest((batch / "plan.json").read_bytes()),
            "suite_sha256": digest((batch / "suite.json").read_bytes()),
            "model_binding": model_binding(plan["mode"], root),
        },
    )


def infrastructure_failure(run: dict) -> bool:
    m = run["metrics"]
    return (
        m["status"] != "completed"
        or m.get("stop_reason") == "search_error"
        or m.get("search_stop_reason") == "search_error"
        or (m.get("page_read_attempts", 0) > 0 and m.get("pages_read", 0) == 0)
    )


def validate_receipt(batch: Path, row: dict, plan: dict) -> dict | None:
    # Windows may spell the same temporary directory with 8.3 or long names.
    # Compare resolved children against a resolved root, without dropping bounds.
    batch = batch.resolve()
    path = batch / (row["id"] + ".result.json")
    if not path.exists():
        return None
    receipt = json.loads(path.read_bytes())
    reservation = json.loads((batch / "attempts" / (row["id"] + ".json")).read_bytes())
    if not row.get("attempt_id") or receipt.get("attempt_id") != row["attempt_id"]:
        raise ValueError("Receipt attempt identity mismatch")
    if (
        reservation.get("attempt_id") != row["attempt_id"]
        or reservation.get("trial_id") != row["id"]
        or reservation.get("plan_sha256") != digest((batch / "plan.json").read_bytes())
    ):
        raise ValueError("Receipt reservation binding mismatch")
    folder = (batch / receipt["run_file"]).resolve()
    expected = batch / row["id"]
    if (
        expected.is_symlink()
        or not folder.is_relative_to(expected)
        or not folder.is_relative_to(batch)
    ):
        raise ValueError("Receipt run outside trial folder")
    run = json.loads((folder / "run.json").read_bytes())
    if (
        fingerprint(run) != receipt["run_sha256"]
        or run.get("benchmark", {}).get("trial_id") != row["id"]
        or run.get("benchmark", {}).get("suite_sha256") != plan["suite_sha256"]
        or run.get("benchmark", {}).get("corpus_manifest_sha256")
        != plan.get("corpus_manifest_sha256")
        or run.get("provenance", {}).get("source_sha256") != plan["source_sha256"]
        or run.get("excerpt_strategy") != row["strategy"]
        or run.get("config", {}).get("answer_style", "standard")
        != row.get("answer_style", "standard")
        or run.get("config", {}).get("revise_answer", False) != plan.get("revise_answer", False)
        or receipt.get("metrics") != run["metrics"]
        or receipt.get("infrastructure_failure") is not infrastructure_failure(run)
    ):
        raise ValueError("Receipt/run does not match execution")
    # A receipt is issued only after the draft review is durable.
    from .benchmark import check_task_review, load_suite

    task = next(t for t in load_suite(batch / "suite.json").tasks if t.id == row["task_id"])
    packet = json.loads((folder / "task-review.draft.json").read_bytes())
    if not check_task_review(run, task, packet)["valid"]:
        raise ValueError("Receipt review binding invalid")
    return {
        key: receipt[key]
        for key in ("run_file", "run_sha256", "metrics", "infrastructure_failure", "attempt_id")
    }


def recovery_preview(batch: Path, entry: Path) -> dict:
    """Pure read: no env, lock, writes, network, model or old-batch upgrade."""
    batch = batch.resolve()
    context = load_bound_batch(batch)
    plan = context["plan"]
    if plan.get("execution_protocol") != 2:
        raise ValueError("Legacy batch is read-only; create a new experiment to use recovery")
    seal = json.loads((batch / "execution.json").read_bytes())
    if (
        seal.get("schema_version") != 1
        or seal["plan_sha256"] != digest((batch / "plan.json").read_bytes())
        or seal["suite_sha256"] != plan["suite_sha256"]
        or plan["source_sha256"] != source_digest()
        or plan["entry_sha256"] != digest(entry.read_bytes())
    ):
        raise ValueError("Execution plan/source/entry/suite changed; recovery refused")
    if plan.get("corpus"):
        corpus = Path(plan["corpus"])
        if digest((corpus / "manifest.json").read_bytes()) != plan["corpus_manifest_sha256"]:
            raise ValueError("Corpus manifest changed")
        load_corpus(corpus, plan["documents"])
    recoverable, pending, uncertain, saved = {}, [], [], []
    for row in context["rows"]:
        if row["status"] == "saved":
            saved.append(row["id"])
            continue
        receipt = validate_receipt(batch, row, plan)
        if receipt:
            recoverable[row["id"]] = receipt
        elif (
            row["status"] == "not_run"
            and not row.get("attempt_id")
            and not (batch / "attempts" / (row["id"] + ".json")).exists()
            and not (batch / row["id"]).exists()
        ):
            pending.append(row["id"])
        else:
            uncertain.append(row["id"])
    return {
        "context": context,
        "recoverable": recoverable,
        "pending": pending,
        "uncertain": uncertain,
        "saved": saved,
        "mode": plan["mode"],
        "note": "Only pristine not_run trials start. Receipts are adopted without a model call. Started/failed/deadline trials without a valid receipt are never auto-retried; cost may be unknown. Not Agent-state resume or exactly-once.",
    }


def reserve_trial(batch: Path, row: dict, plan: dict) -> None:
    attempt = uuid4().hex
    folder = batch / "attempts"
    folder.mkdir(exist_ok=True)
    # Reservation is never overwritten, even if a worker failed to start.
    with (folder / (row["id"] + ".json")).open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "trial_id": row["id"],
                "attempt_id": attempt,
                "started_at": datetime.now(UTC).isoformat(),
                "plan_sha256": digest((batch / "plan.json").read_bytes()),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    row.update(status="running", attempt_id=attempt)


def worker_claim(batch: Path, row: dict, plan: dict, root: Path) -> str:
    seal = json.loads((batch / "execution.json").read_bytes())
    record = json.loads((batch / "attempts" / (row["id"] + ".json")).read_bytes())
    if (
        row["status"] != "running"
        or record["attempt_id"] != row.get("attempt_id")
        or record["plan_sha256"] != seal["plan_sha256"]
        or seal["plan_sha256"] != digest((batch / "plan.json").read_bytes())
        or seal["model_binding"] != model_binding(plan["mode"], root)
    ):
        raise ValueError("Worker execution claim/model configuration mismatch")
    with (batch / "attempts" / (row["id"] + ".claimed")).open("x"):
        pass
    return record["attempt_id"]
