"""`squadmind watch`: indirme klasörünü izleyip yeni demoları otomatik işler.

FACEIT Downloads API onaylanana kadar demolar tarayıcıdan elle indiriliyor. Bu modül
indirme klasörünü (watchdog ile) izler:
1. Yeni demo dosyası gelir (.dem / .dem.gz / .dem.zst).
2. İndirmenin bitmesi beklenir: dosya boyutu STABLE_SECONDS boyunca değişmezse tamam.
3. Dosya inbox'a taşınır ve mevcut pipeline (`ingest_demo`) çalışır: açma, parse,
   maç eşleştirme (dosya adı ya da harita + kadro + tarih), DB, FACEIT doğrulaması.

Tarayıcılar indirirken geçici ad kullanır (.crdownload, .part) ve bitince yeniden
adlandırır; bu yüzden hem "oluşturuldu" hem "taşındı" olayları dinlenir.
"""

from __future__ import annotations

import queue
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import duckdb
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from squadmind.ingest.demos import DEMO_SUFFIXES, DemoIngestResult, ingest_demo
from squadmind.parse.demo_parser import DemoTables, parse_demo

# Dosya boyutu bu kadar saniye değişmezse indirme bitmiş sayılır
STABLE_SECONDS = 3.0
# Boyut kontrol aralığı
POLL_SECONDS = 0.5
# Bu süre içinde indirme bitmezse dosya atlanır (yavaş bağlantı için geniş tutuldu)
STABLE_TIMEOUT = 30 * 60.0


def is_demo_file(path: Path) -> bool:
    """Demo uzantılı mı (tarayıcının geçici .crdownload / .part dosyaları değil)."""
    return path.name.lower().endswith(DEMO_SUFFIXES)


def wait_until_stable(
    path: Path,
    quiet: float = STABLE_SECONDS,
    poll: float = POLL_SECONDS,
    timeout: float = STABLE_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Dosya boyutu `quiet` saniye boyunca değişmeyene kadar bekler.

    True: indirme bitti. False: dosya kayboldu (tarayıcı yeniden adlandırdı / iptal)
    ya da `timeout` doldu. Boş dosya bitmiş sayılmaz.
    """
    start = clock()
    last_size = -1
    last_change = start
    while clock() - start < timeout:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        now = clock()
        if size != last_size:
            last_size, last_change = size, now
        elif size > 0 and now - last_change >= quiet:
            return True
        sleep(poll)
    return False


def move_to_inbox(path: Path, inbox: Path) -> Path:
    """Dosyayı inbox'a taşır; aynı adda dosya varsa üzerine yazar (aynı maçın demosu)."""
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / path.name
    if dest.exists():
        dest.unlink()
    shutil.move(str(path), str(dest))
    return dest


def process_download(
    path: Path,
    connect: Callable[[], duckdb.DuckDBPyConnection],
    inbox: Path,
    processed: Path,
    tmp: Path,
    parser: Callable[[Path], DemoTables] = parse_demo,
    delete_after: bool = False,
    wait: Callable[[Path], bool] = wait_until_stable,
) -> DemoIngestResult | None:
    """İndirilen tek bir demoyu bekler, inbox'a taşır ve işler.

    None: dosya işlenmedi (demo değil ya da indirme tamamlanmadı). DB bağlantısı sadece
    işleme süresince açık kalır; `watch` çalışırken diğer komutlar DB'yi kullanabilsin.
    """
    if not is_demo_file(path) or not wait(path):
        return None
    demo = move_to_inbox(path, inbox)
    con = connect()
    try:
        return ingest_demo(con, demo, tmp, processed, parser, delete_after)
    finally:
        con.close()


class _DemoEventHandler(FileSystemEventHandler):
    """watchdog olaylarını (kendi thread'inde) kuyruğa atar; işleme ana thread'de."""

    def __init__(self, pending: queue.Queue[Path]) -> None:
        self.pending = pending

    def _push(self, raw: str | bytes) -> None:
        path = Path(raw.decode() if isinstance(raw, bytes) else raw)
        if is_demo_file(path):
            self.pending.put(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # .crdownload → .dem.zst yeniden adlandırması: tamamlanan indirme
        if not event.is_directory:
            self._push(event.dest_path)


class DemoWatcher:
    """İndirme klasörünü izler; yeni demoları sırayla `handle` fonksiyonuna verir."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.pending: queue.Queue[Path] = queue.Queue()
        self._observer = Observer()
        self._observer.schedule(_DemoEventHandler(self.pending), str(folder), recursive=False)

    def enqueue_existing(self) -> int:
        """Klasörde zaten duran demoları kuyruğa ekler (`--existing`)."""
        found = sorted(p for p in self.folder.iterdir() if p.is_file() and is_demo_file(p))
        for p in found:
            self.pending.put(p)
        return len(found)

    def run(self, handle: Callable[[Path], None], poll: float = 1.0) -> None:
        """Ctrl+C'ye kadar çalışır. Aynı dosya için gelen tekrar olaylar bir kez işlenir."""
        self._observer.start()
        seen: set[Path] = set()
        try:
            while True:
                try:
                    path = self.pending.get(timeout=poll)
                except queue.Empty:
                    continue
                # Oluşturma + değişiklik olayları aynı dosyayı iki kez getirebilir;
                # dosya taşındıktan sonra artık yoktur, o yüzden yalnızca var olanı işle
                if path in seen and not path.exists():
                    continue
                if not path.exists():
                    continue
                seen.add(path)
                handle(path)
        finally:
            self._observer.stop()
            self._observer.join()
