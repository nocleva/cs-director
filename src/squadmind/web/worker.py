"""Web arka plan işçisi: FACEIT maç geçmişi çekme ve yüklenen demoyu işleme, sırayla.

Tek iş parçacığı: demo işleme ~4 sn ve DB'ye yazıyor; aynı anda iki iş DuckDB'yi
kilitlemesin diye işler kuyruktan teker teker alınır.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from squadmind.faceit.client import FaceitAPIError, FaceitClient
from squadmind.ingest.demos import (
    MAX_DEMO_BYTES,
    DemoIngestResult,
    ingest_demo,
    ingest_package,
)
from squadmind.ingest.matches import sync_player_matches
from squadmind.parse.demo_parser import DemoTables, parse_demo
from squadmind.web.store import DONE, FAILED, MISMATCH, PROCESSING, REJECTED, Store

log = logging.getLogger("squadmind.web")

# Katılan oyuncunun bu kadar son maçı çekilir (yüklediği demonun maçı DB'de olsun diye)
SYNC_MATCHES = 100


class DownloadError(Exception):
    """Sunucu yolunda demo indirilemedi / çok büyük."""


def download_file(url: str, dest: Path, max_bytes: int = MAX_DEMO_BYTES) -> int:
    """İmzalı linkten demoyu akışla dosyaya indirir (belleğe almaz); boyut sınırını aşarsa keser.

    Yetki başlığı GÖNDERİLMEZ: link kendi imzasını taşır. İndirilen bayt sayısını döner.
    """
    total = 0
    with httpx.stream("GET", url, timeout=httpx.Timeout(30.0, read=120.0),
                      follow_redirects=True) as r:
        if r.status_code >= 400:
            raise DownloadError(f"indirme başarısız (HTTP {r.status_code})")
        declared = int(r.headers.get("content-length") or 0)
        if declared > max_bytes:
            raise DownloadError(f"demo çok büyük ({declared / 1048576:.0f} MB)")
        with dest.open("wb") as f:
            for block in r.iter_bytes(1 << 20):
                total += len(block)
                if total > max_bytes:
                    raise DownloadError("demo çok büyük (2 GB sınırı)")
                f.write(block)
    return total


def demo_resource(store: Store, client: FaceitClient, match_id: str) -> str | None:
    """Maçın demo resource URL'si: önce DB, yoksa Data API maç detayı."""
    row = store.run(lambda con: con.execute(
        "SELECT demo_url FROM matches WHERE match_id = ?", [match_id]).fetchone())
    if row and row[0]:
        return row[0]
    urls = client.get_match_details(match_id).get("demo_url") or []
    if isinstance(urls, str):
        urls = [urls]
    return urls[0] if urls else None


def sniff_suffix(path: Path) -> str:
    """Dosya içeriğinden uzantı: zstd / gzip / ham .dem (sunucu yolunda adlandırma için)."""
    with path.open("rb") as f:
        head = f.read(4)
    if head == bytes([0x28, 0xB5, 0x2F, 0xFD]):  # zstd sihirli sayısı
        return ".dem.zst"
    if head[:2] == bytes([0x1F, 0x8B]):  # gzip sihirli sayısı
        return ".dem.gz"
    return ".dem"


@dataclass
class Job:
    kind: str      # "sync" / "ingest" (tam demo) / "package" (tarayıcı paketi) / "faceit" (sunucu indirir)
    key: str       # steam_id ya da upload_id
    path: Path | None = None
    # package işi: {"sha256", "dem_sha256", "orig_size", "file_name"} (tarayıcının bildirdiği)
    meta: dict = field(default_factory=dict)


class Worker:
    def __init__(self, store: Store, processed: Path, tmp: Path,
                 client_factory: Callable[[], FaceitClient] = FaceitClient,
                 parser: Callable[[Path], DemoTables] = parse_demo,
                 package_parser: Callable[[bytes], DemoTables] | None = None,
                 delete_after: bool = False, team_owner: str | None = None,
                 downloader: Callable[[str, Path], int] = download_file) -> None:
        self.store = store
        self.processed, self.tmp = processed, tmp
        self.client_factory = client_factory
        self.parser = parser
        self.package_parser = package_parser  # None: paket sürümünü de denetleyen varsayılan
        self.delete_after = delete_after
        self.downloader = downloader  # testlerde sahte indirme
        # Takım sahibi: katılan hesabın onunla ortak maçı yoksa yanlış hesap uyarısı verilir
        self.team_owner = team_owner
        self.jobs: queue.Queue[Job | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="squadmind-worker", daemon=True)

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
        elif job.kind == "package":
            self._ingest_package(job.key, job.path, job.meta)
        elif job.kind == "faceit":
            self._faceit_server(job.key, job.meta)

    def _sync(self, steam_id: str) -> None:
        user = self.store.user(steam_id)
        if user is None:
            return
        self.store.set_sync_status(steam_id, "alınıyor")
        try:
            with self.client_factory() as client:
                res = self.store.run(lambda con: sync_player_matches(
                    client, con, user.nickname, limit=SYNC_MATCHES), lock=False)
        except FaceitAPIError as exc:
            self.store.set_sync_status(steam_id, f"hata: {exc}")
            return
        status = f"tamam ({res.matches_written} maç)"
        # Benzer adlı başka bir hesaba katılma (ör. _squ1rtle_ yerine squ1rtle) yaygın bir
        # hata: takım sahibiyle hiç ortak maç yoksa oyuncuyu uyar
        if self.team_owner and user.nickname.lower() != self.team_owner.lower() \
                and self.shared_matches(user.faceit_id) == 0:
            status += f"; uyarı: {self.team_owner} ile ortak maç yok, doğru hesap mı?"
        self.store.set_sync_status(steam_id, status)

    def shared_matches(self, faceit_id: str) -> int:
        """Bu oyuncunun takım sahibiyle DB'deki ortak maç sayısı."""
        def count(con):
            return con.execute(
                """
                SELECT count(DISTINCT a.match_id)
                FROM match_player_stats a
                JOIN match_player_stats b USING (match_id)
                JOIN players p ON p.faceit_id = b.player_id
                WHERE a.player_id = ? AND lower(p.nickname) = lower(?)
                """,
                [faceit_id, self.team_owner],
            ).fetchone()[0]
        return self.store.run(count)

    def _ingest(self, upload_id: str, path: Path | None) -> None:
        up = self.store.upload(upload_id)
        if up is None or path is None or not path.exists():
            self.store.set_upload(upload_id, FAILED, "dosya bulunamadı")
            return
        self.store.set_upload(upload_id, PROCESSING)
        r = self.store.run(lambda con: ingest_demo(
            con, path, self.tmp, self.processed, self.parser,
            delete_after=self.delete_after, uploader=up["steam_id"],
            expected_map=up.get("map_hint")), lock=False)
        status, msg = self._outcome(r)
        self.store.set_upload(upload_id, status, msg, r.match_id)
        # Reddedilen / hatalı dosya sunucuda yer kaplamasın (kopya ya da bozuk)
        if status in (REJECTED, FAILED) and path.exists():
            path.unlink()

    @staticmethod
    def _outcome(r: DemoIngestResult) -> tuple[str, str | None]:
        """İşleme sonucunu yükleme durumuna ve kullanıcıya gösterilecek nota çevirir."""
        if r.rejected:
            return REJECTED, r.error
        if r.error:
            return FAILED, r.error
        if r.mismatch_reason:
            return MISMATCH, f"paket FACEIT kaydıyla tutmadı ({r.mismatch_reason}); analize alınmadı"
        if r.validated is False:
            return MISMATCH, "FACEIT skor tablosuyla tutmadı; analize alınmadı"
        return DONE, f"{r.rounds} round, {r.kills} kill"

    def _ingest_package(self, upload_id: str, path: Path | None, meta: dict) -> None:
        """Tarayıcı paketini işler (demo dosyası yok); paket dosyası işlenince silinir."""
        up = self.store.upload(upload_id)
        if up is None or path is None or not path.exists():
            self.store.set_upload(upload_id, FAILED, "paket bulunamadı")
            return
        self.store.set_upload(upload_id, PROCESSING)
        try:
            data = path.read_bytes()
            r = self.store.run(lambda con: ingest_package(
                con, data, meta.get("file_name", up["file_name"]), meta["sha256"],
                meta.get("dem_sha256", ""), int(meta.get("orig_size", 0)),
                parser=self.package_parser, uploader=up["steam_id"],
                expected_map=up.get("map_hint"),
                source=meta.get("source", "paket")), lock=False)
            status, msg = self._outcome(r)
            self.store.set_upload(upload_id, status, msg, r.match_id)
        finally:
            path.unlink(missing_ok=True)  # paket işlenince sunucuda hiçbir şey kalmaz

    def _faceit_server(self, upload_id: str, meta: dict) -> None:
        """FACEIT Downloads API ile demoyu SUNUCU indirir, işler ve dosyayı SİLER.

        FACEIT başvurusundaki taahhüt: demo dosyası saklanmaz. Bu yüzden indirilen dosya özel
        bir klasöre iner, `ingest_demo` kendi 'processed' klasörüne değil buraya taşır
        (tutmayan demo bile) ve iş bitince klasör topyekün silinir; kayıt `faceit-sunucu`.
        """
        up = self.store.upload(upload_id)
        match_id = str(meta.get("match_id", ""))
        if up is None or not match_id:
            self.store.set_upload(upload_id, FAILED, "iş bilgisi eksik")
            return
        self.store.set_upload(upload_id, PROCESSING)
        folder = self.tmp / f"faceit-{upload_id}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            status, msg, mid = self._faceit_run(up, match_id, folder)
        except (FaceitAPIError, DownloadError) as exc:
            status, msg, mid = FAILED, str(exc), None
        except Exception as exc:  # noqa: BLE001 - işçi durmasın
            log.exception("FACEIT sunucu işi başarısız: %s", match_id)
            status, msg, mid = FAILED, f"{type(exc).__name__}: {exc}", None
        finally:
            shutil.rmtree(folder, ignore_errors=True)  # indirilen demo hiçbir durumda kalmaz
        self.store.set_upload(upload_id, status, msg, mid)
        self.store.set_event_status(match_id, "tamam" if status == DONE else "hata", msg)

    def _faceit_run(self, up: dict, match_id: str, folder: Path) -> tuple[str, str | None, str | None]:
        # Skor tablosu DB'de yoksa (webhook maç bitince hemen gelir) kullanıcının geçmişini çek
        have = self.store.run(lambda con: con.execute(
            "SELECT count(*) FROM match_player_stats WHERE match_id = ?", [match_id]).fetchone()[0])
        user = self.store.user(up["steam_id"])
        if not have and user is not None:
            with self.client_factory() as client:
                self.store.run(lambda con: sync_player_matches(
                    client, con, user.nickname, limit=20), lock=False)
        with self.client_factory() as client:
            resource = demo_resource(self.store, client, match_id)
            if not resource:
                return FAILED, "bu maçın demosu henüz yok", None
            url = client.get_demo_download_url(resource)
        raw = folder / "indirilen"
        self.downloader(url, raw)
        path = folder / f"{match_id}{sniff_suffix(raw)}"
        raw.rename(path)
        r = self.store.run(lambda con: ingest_demo(
            con, path, self.tmp, folder / "kept", self.parser, delete_after=True,
            uploader=up["steam_id"], expected_map=up.get("map_hint"),
            source="faceit-sunucu"), lock=False)
        status, msg = self._outcome(r)
        return status, msg, r.match_id
