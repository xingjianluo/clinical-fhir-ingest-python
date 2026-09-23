"""本地文件存储：患者数据（当前态+历史版本）与审计记录。

目录布局::

    data_dir/
      patients/<patient_id>.json          当前态
      history/<patient_id>/<rev>.json     不可变历史修订
      idempotency/<patient_id>/<req>.json 幂等请求快照
      audit/audit.log                     仅追加的审计 JSONL

并发语义：进程内按患者 ID 加串行锁；写盘采用临时文件 + os.replace 原子替换，
崩溃或重试不会产生半写文件。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.models import AuditEntry, Patient


def _atomic_write_json(path: Path, obj: dict) -> None:
    """同目录临时文件落盘后原子替换，保证读取方不会看到半写内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class LocalStore:
    """以本地目录为后端的存储。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.patients_dir = self.data_dir / "patients"
        self.history_dir = self.data_dir / "history"
        self.idem_dir = self.data_dir / "idempotency"
        self.audit_dir = self.data_dir / "audit"
        self.audit_file = self.audit_dir / "audit.log"
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._audit_lock = threading.Lock()

    def setup(self) -> None:
        for d in (self.patients_dir, self.history_dir, self.idem_dir, self.audit_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _lock_for(self, patient_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(patient_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[patient_id] = lock
            return lock

    @contextmanager
    def patient_lock(self, patient_id: str) -> Iterator[None]:
        """同一患者的写操作串行化；不同患者互不阻塞。"""
        lock = self._lock_for(patient_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    # ---- 患者当前态与历史 -------------------------------------------------

    def _patient_path(self, patient_id: str) -> Path:
        return self.patients_dir / f"{patient_id}.json"

    def get_patient(self, patient_id: str) -> Patient | None:
        path = self._patient_path(patient_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return Patient.from_dict(json.load(fh))

    def save_patient(self, patient: Patient) -> None:
        """原子写当前态，同时把该修订写入不可变历史。"""
        _atomic_write_json(self._patient_path(patient.patient_id), patient.to_dict())
        hist_path = self.history_dir / patient.patient_id / f"{patient.rev}.json"
        _atomic_write_json(hist_path, patient.to_dict())

    def get_revision(self, patient_id: str, rev: int) -> Patient | None:
        path = self.history_dir / patient_id / f"{rev}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return Patient.from_dict(json.load(fh))

    def list_history_revs(self, patient_id: str) -> list[int]:
        d = self.history_dir / patient_id
        if not d.exists():
            return []
        revs = [int(p.stem) for p in d.glob("*.json") if p.stem.isdigit()]
        return sorted(revs)

    # ---- 幂等键 -----------------------------------------------------------

    def _idem_path(self, patient_id: str, request_id: str) -> Path:
        return self.idem_dir / patient_id / f"{request_id}.json"

    def get_idempotency(self, patient_id: str, request_id: str) -> Patient | None:
        path = self._idem_path(patient_id, request_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return Patient.from_dict(json.load(fh))

    def put_idempotency(self, patient_id: str, request_id: str, patient: Patient) -> None:
        _atomic_write_json(self._idem_path(patient_id, request_id), patient.to_dict())

    # ---- 审计 -------------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> None:
        """以 O_APPEND 方式追加一行 JSON；锁保证多线程行不交错。"""
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self._audit_lock:
            with self.audit_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def read_audit(self, patient_id: str | None = None) -> list[AuditEntry]:
        if not self.audit_file.exists():
            return []
        entries: list[AuditEntry] = []
        with self.audit_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = AuditEntry.from_dict(json.loads(line))
                if patient_id is None or entry.patient_id == patient_id:
                    entries.append(entry)
        return entries
