"""Web arka plan işçisi: FACEIT maç geçmişi çekme ve yüklenen demoyu işleme, sırayla.

Tek iş parçacığı: demo işleme ~4 sn ve DB'ye yazıyor; aynı anda iki iş DuckDB'yi
kilitlemesin diye işler kuyruktan teker teker alınır.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from director.faceit.client import FaceitAPIError, FaceitClient
from director.ingest.demos import ingest_demo
from director.ingest.matches import sync_player_matches
from director.parse.demo_parser import DemoTables, parse_demo
from director.web.store import DONE, FAILED, MISMATCH, PROCESSING, REJECTED, Store

log = logging.getLogger("director.web")

# Katılan oyuncunun bu kadar son maçı çekilir (yüklediği demonun maçı DB'de olsun diye)
SYNC_MATCHES = 100


@dataclass
class Job:
    kind: str      # "sync" (maç geçmişi) / "ingest" (demo)
    key: str       # steam_id ya da upload_id
    path: Path | None = None


class Worker:
    def __init__(self, store: Store, processed: Path, tmp: Path,
                 client_factory: Callable[[], FaceitClient] = FaceitClient,
                 parser: Callable[[Path], DemoTables] = parse_demo,
                 delete_after: bool = False) -> None:
        self.store = store
        self.processed, self.tmp = processed, tmp
        self.client_factory = client_factory
        self.parser = parser
        self.delete_after = delete_after
        self.jobs: queue.Queue[Job | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="director-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.jobs.put(None)
        self._thread.join(timeout=30)

    def submit(self, job: Job) -> None:
        self.jobs.put(job)

    def _loop(self) -> None:
        while (job := self.jobs.get()) is not None:
            try:
                self.handle(job)
            except Exception:  # bir işin hatası işçiyi durdurmasın
                log.exception("iş başarısız: %s", job)
            finally:
                self.jobs.task_done()

    # --- işler ----------------------------------------------------------------------

    def handle(self, job: Job) -> None:
        if job.kind == "sync":
            self._sync(job.key)
        elif job.kind == "ingest":
            self._ingest(job.key, job.path)

    def _sync(self, steam_id: str) -> None:
        user = self.store.user(steam_id)
        if user is None:
            return
        self.store.set_sync_status(steam_id, "alınıyor")
        try:
            with self.client_factory() as client:
                res = self.store.run(lambda con: sync_player_matches(
                    client, con, user.nickname, limit=SYNC_MATCHES), lock=False)
            self.store.set_sync_status(steam_id, f"tamam ({res.matches_written} maç)")
        except FaceitAPIError as exc:
            self.store.set_sync_status(steam_id, f"hata: {exc}")

    def _ingest(self, upload_id: str, path: Path | None) -> None:
        up = self.store.upload(upload_id)
        if up is None or path is None or not path.exists():
            self.store.set_upload(upload_id, FAILED, "dosya bulunamadı")
            return
        self.store.set_upload(upload_id, PROCESSING)
        r = self.store.run(lambda con: ingest_demo(
            con, path, self.tmp, self.processed, self.parser,
            delete_after=self.delete_after, uploader=up["steam_id"]), lock=False)
        if r.rejected:
            status, msg = REJECTED, r.error
        elif r.error:
            status, msg = FAILED, r.error
        elif r.validated is False:
            status, msg = MISMATCH, "FACEIT skor tablosuyla tutmadı; analize alınmadı"
        else:
            status, msg = DONE, f"{r.rounds} round, {r.kills} kill"
        self.store.set_upload(upload_id, status, msg, r.match_id)
        # Reddedilen / hatalı dosya sunucuda yer kaplamasın (kopya ya da bozuk)
        if status in (REJECTED, FAILED) and path.exists():
            path.unlink()
