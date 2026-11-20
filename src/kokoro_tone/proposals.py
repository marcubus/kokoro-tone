"""The proposal store: a decision between a caller's guess and the lexicon.

ADR-0017 splits the write from the decision. In `review` mode a `POST` records
a proposal here and answers `202`; nothing reaches the learned overlay until an
admin key approves the row. The store is a second JSON file beside
`learned.json`, with the same atomic write, byte cap and symlink refusal, so a
corrupt queue cannot take the gateway down and a forged link cannot redirect the
write.

A proposal is a row, not a queue entry: it keeps its `status`, its timestamps
and the reason for a rejection after the decision, so "we already said no to
this" survives the request that asked. Rows sort by id, and the id is a UUIDv7,
so creation order is lexical order and the oldest undecided row leads the queue.

Boundaries - these are the behaviour the API promises, not implementation
details that may drift:

- **Nothing decides itself.** There is no timer, no timeout and no background
  task; ADR-0017 rejects approve-by-timeout because it turns inattention into
  policy. A pending row stays pending until an admin calls `decide`, and an
  undrained queue is safe but silent (see "Visibility" below).
- **Pending rows are never pruned.** At `limits.proposals` rows, `_trim` drops
  the oldest *decided* rows first; if the *pending* rows alone exceed the cap,
  `propose` raises `MarkupError` (a caller-visible 400) instead of discarding
  undecided work. History is therefore lossy only in the direction that has
  already been decided.
- **The cap is checked on write, not on read.** A file that grew past
  `limits.proposals` (hand-edited, or a lowered limit) is still readable and
  listable; it only refuses new proposals until rows are deleted.
- **One file, one lock, read-modify-write.** `list`, `decide` and `delete` load
  the whole file under a process lock. There is no index and no cross-process
  coordination: two gateway processes sharing the path can lose a write, which
  is why the file sits beside `learned.json` in a single-service deployment.
- **An approval is not transactional with the lexicon.** `routes` applies the
  rule first and records the decision second, so a crash can leave an applied
  rule whose row is still `pending`; re-approving is safe because the learned
  overlay replaces by `match` (idempotent per rule). A decided row is immutable:
  a second decision is a `409 invalid_transition`.
- **Visibility is pull-only.** The only readers are `list()` and `counts()`;
  `/health` publishes `counts()[pending]` and there is no push, webhook or alert
  on a growing queue. Watching that count is the operator's job.
- **Corruption is loud.** A malformed file, a bad version or a non-list
  `proposals` key raises `StoreError` (a 500 naming the file), never an empty
  queue, so a proposal is not silently forgotten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .limits import Limits
from .log import event, warning
from .parse import MarkupError

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUSES = frozenset({PENDING, APPROVED, REJECTED})

DEFAULT_PAGE = 50
MAX_PAGE = 200

# An uppercase run is the signal that a proposer is guessing stress, which is
# the shape of policy this record exists to keep out (ADR-0017 rule 10).
_UPPERCASE_RUN = re.compile(r"[A-Z]{2,}")

_uuid_lock = threading.Lock()
_last_ms = 0
_sequence = 0


class StoreError(RuntimeError):
    """The proposal file cannot be read or written; a caller-visible 500."""


class StoreConflict(RuntimeError):
    """The row was decided by another caller between the read and the write."""


Rows = list[dict[str, object]]


def has_uppercase_run(value: str) -> bool:
    return bool(_UPPERCASE_RUN.search(value))


def uuid7() -> str:
    """A UUIDv7: a 48-bit millisecond timestamp, then a counter and randomness.

    Python 3.12 has no `uuid.uuid7`, and creation order is queue order, so the
    timestamp has to lead *and* ties inside one millisecond have to break in
    creation order. The 12-bit `rand_a` field is used as a per-millisecond
    sequence for that reason; the rest stays random.
    """
    global _last_ms, _sequence
    with _uuid_lock:
        millis = int(time.time() * 1000)
        if millis < _last_ms:
            # The wall clock stepped back; keep the queue monotonic anyway.
            millis = _last_ms
        if millis == _last_ms:
            _sequence += 1
        else:
            _last_ms = millis
            _sequence = 0
        if _sequence > 0x0FFF:
            _last_ms += 1
            millis = _last_ms
            _sequence = 0
        rand_a = _sequence
        rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (
        ((millis & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    )
    return str(uuid.UUID(int=value))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rule_id(source: str, match: str, say: str) -> str:
    """A stable handle for a rule that has no id of its own.

    The learned file stores `{match, say}` and the shipped defaults are read
    only, so an id is derived from what identifies a rule: its layer, its match
    and its say. The same rule in two layers is two ids, which is what a
    deletion needs to be unambiguous about.
    """
    digest = hashlib.sha256(f"{source}\x00{match.casefold()}\x00{say}".encode()).hexdigest()
    return f"{source}-{digest[:24]}"


@dataclass(frozen=True)
class Proposal:
    id: str
    status: str
    word: str
    say: str
    sense: str | None
    context: str | None
    created_at: str
    created_by_role: str
    decided_at: str | None = None
    decided_by_role: str | None = None
    reason: str | None = None
    applied: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_context(value: object, limits: Limits) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarkupError("context must be a string")
    text = value.strip()
    if not text:
        return None
    if "[" in text or "]" in text:
        raise MarkupError("context must not contain brackets")
    if len(text) > limits.context_chars:
        raise MarkupError(f"context exceeds {limits.context_chars} characters")
    return text


class ProposalStore:
    """The queue and its history: one file, one lock, atomic writes.

    Every method re-reads the file rather than caching, so an operator editing
    `proposals.json` by hand takes effect on the next call and two readers never
    disagree about a row. The cost is a full parse per request, which the row cap
    bounds. The lock serialises writers within this process only; see the module
    docstring for what that does not cover.
    """

    def __init__(self, path: str, limits: Limits) -> None:
        self.path = path
        self.limits = limits
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reading

    def _load(self) -> list[dict[str, object]]:
        if not self.path:
            return []
        path = Path(self.path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError(f"proposal store is not readable: {exc}") from exc
        if path.is_symlink():
            raise StoreError("proposal store must not be a symlink")
        if size > self.limits.proposals_bytes:
            raise StoreError(f"proposal store is {size} bytes, over {self.limits.proposals_bytes}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(f"proposal store is not readable: {exc}") from exc
        if not isinstance(raw, dict):
            raise StoreError("proposal store top level must be an object")
        if raw.get("version") != 1:
            raise StoreError(f"proposal store has unsupported version {raw.get('version')!r}")
        rows = raw.get("proposals")
        if not isinstance(rows, list):
            raise StoreError("proposal store's `proposals` must be a list")
        kept: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                warning("proposal store: skipping a row that is not an object")
                continue
            if not isinstance(row.get("id"), str) or not isinstance(row.get("status"), str):
                warning("proposal store: skipping a row without an id and status")
                continue
            kept.append(row)
        return kept

    def _write(self, rows: list[dict[str, object]]) -> None:
        if not self.path:
            raise StoreError("proposal store path is not configured")
        path = Path(self.path)
        text = json.dumps({"version": 1, "proposals": rows}, ensure_ascii=False, indent=2) + "\n"
        if len(text.encode("utf-8")) > self.limits.proposals_bytes:
            raise MarkupError(f"proposal store would exceed {self.limits.proposals_bytes} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".proposals-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            # The queue is not a secret; it is the operator's inbox.
            os.chmod(path, 0o644)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _row(proposal: Proposal) -> dict[str, object]:
        return proposal.as_dict()

    @staticmethod
    def _optional(row: dict[str, object], key: str) -> str | None:
        value = row.get(key)
        return value if isinstance(value, str) else None

    @classmethod
    def _from_row(cls, row: dict[str, object]) -> Proposal:
        return Proposal(
            id=str(row.get("id", "")),
            status=str(row.get("status", PENDING)),
            word=str(row.get("word", "")),
            say=str(row.get("say", "")),
            sense=cls._optional(row, "sense"),
            context=cls._optional(row, "context"),
            created_at=str(row.get("created_at", "")),
            created_by_role=str(row.get("created_by_role", "")),
            decided_at=cls._optional(row, "decided_at"),
            decided_by_role=cls._optional(row, "decided_by_role"),
            reason=cls._optional(row, "reason"),
            applied=bool(row.get("applied", False)),
        )

    # ------------------------------------------------------------- queries

    def get(self, proposal_id: str) -> Proposal | None:
        """One row by id, or `None`. A decided row is returned, not hidden."""
        with self._lock:
            for row in self._load():
                if row.get("id") == proposal_id:
                    return self._from_row(row)
        return None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = DEFAULT_PAGE,
        after: str | None = None,
    ) -> list[Proposal]:
        """Oldest first: the queue is a work queue, not a feed.

        `after` is an id cursor, exclusive, so paging cannot repeat or skip a row
        even when rows are added between calls. `limit` is clamped to
        `MAX_PAGE`; there is no "return everything" mode. With no `status` the
        decided rows are included - the history is part of the resource, and the
        pending queue is the filtered view. A file over `limits.proposals` is
        still listed; only writing refuses.
        """
        limit = max(1, min(MAX_PAGE, limit))
        with self._lock:
            rows = sorted(self._load(), key=lambda row: str(row.get("id", "")))
        chosen = [
            self._from_row(row)
            for row in rows
            if (status is None or row.get("status") == status)
            and (after is None or str(row.get("id", "")) > after)
        ]
        return chosen[:limit]

    def counts(self) -> dict[str, int]:
        """Rows per status. `/health` publishes the pending count from here.

        This is the only signal that a queue is growing: nothing pushes, alerts
        or expires a pending row, so a deployment that turns review on and never
        reads this stops learning new names silently.
        """
        with self._lock:
            rows = self._load()
        counts = {state: 0 for state in sorted(STATUSES)}
        for row in rows:
            state = str(row.get("status", ""))
            if state in counts:
                counts[state] += 1
        return counts

    # ------------------------------------------------------------- writes

    def propose(
        self,
        *,
        word: str,
        say: str,
        sense: str | None,
        context: str | None,
        role: str,
    ) -> Proposal:
        """Append a pending row and return it.

        Raises `MarkupError` (a caller-visible 400) when the *pending* rows alone
        would exceed `limits.proposals`; decided rows are trimmed first, so the
        cap never discards an undecided proposal. The row is written before it is
        returned, so a `202` means the proposal is durable.
        """
        proposal = Proposal(
            id=uuid7(),
            status=PENDING,
            word=word,
            say=say,
            sense=sense,
            context=_validate_context(context, self.limits),
            created_at=_now(),
            created_by_role=role,
        )
        with self._lock:
            rows = self._load()
            rows.append(self._row(proposal))
            self._trim(rows)
            try:
                self._write(rows)
            except OSError as exc:
                raise StoreError(f"proposal store is not writable: {exc}") from exc
        event(
            "pronunciation_propose",
            chars=len(word),
            sense=sense or "",
            role=role,
            pending=len(rows),
        )
        return proposal

    def _trim(self, rows: Rows) -> None:
        """Keep the file bounded without losing undecided work.

        Called on the row list before a write. Decided rows are dropped oldest
        first once the cap is passed; if the pending rows alone exceed it, this
        raises instead, because discarding a proposal nobody has judged is the
        silent loss the store exists to prevent.
        """
        if len(rows) <= self.limits.proposals:
            return
        pending = [row for row in rows if row.get("status") == PENDING]
        if len(pending) > self.limits.proposals:
            raise MarkupError(f"proposal store would exceed {self.limits.proposals} rows")
        # Decided rows are the audit trail; drop the oldest of them first.
        for row in list(rows):
            if len(rows) <= self.limits.proposals:
                break
            if row.get("status") != PENDING:
                rows.remove(row)

    def decide(
        self,
        proposal_id: str,
        *,
        status: str,
        role: str,
        reason: str | None,
        applied: bool = False,
    ) -> Proposal:
        """Record the decision on one row and return the updated row.

        A decided row is immutable: a second decision raises `StoreConflict`,
        which the route turns into `409 invalid_transition`. That is also the
        guard against two callers racing the same row. `applied` is passed by the
        route after the learned overlay was written; this method does not touch
        the lexicon, so the two writes are not transactional and a crash between
        them leaves an applied rule with a still-pending row (re-approving is
        idempotent, because the overlay replaces by `match`).
        """
        with self._lock:
            rows = self._load()
            for index, row in enumerate(rows):
                if row.get("id") != proposal_id:
                    continue
                if row.get("status") != PENDING:
                    raise StoreConflict(f"proposal {proposal_id} is already decided")
                updated = replace(
                    self._from_row(row),
                    status=status,
                    decided_at=_now(),
                    decided_by_role=role,
                    reason=reason,
                    applied=applied,
                )
                rows[index] = self._row(updated)
                self._write(rows)
                event(
                    "pronunciation_decide",
                    status=status,
                    role=role,
                    applied=applied,
                    reason="yes" if reason else "no",
                )
                return updated
        raise StoreError(f"proposal {proposal_id} disappeared while deciding it")

    def delete(self, proposal_id: str) -> bool:
        """Remove one row, decided or pending. `False` means unknown id.

        Deleting a decided row discards its audit trail and its rejection reason;
        the learned rule it produced, if any, lives in `learned.json` and is
        removed separately by rule id.
        """
        with self._lock:
            rows = self._load()
            kept = [row for row in rows if row.get("id") != proposal_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
        event("pronunciation_proposal_delete", status="deleted")
        return True
