"""Tarayıcıda (WASM) üretilen demo paketini okuyup awpy `Demo` gibi davranan nesneye çevirir.

İş akışı: kullanıcının tarayıcısı 200+ MB'lık demoyu `wasm/awpy-wasm` ile ayrıştırır ve
birkaç yüz KB'lık gzip'li bir JSON paketi sunucuya yollar. Burada paket `PackageDemo`
olur; `demo_parser.tables_from_demo` aynı tablo kurucularıyla bugünkü awpy yolunun
BİREBİR aynı DB tablolarını (`DemoTables`) üretir.

Paket biçimi (sürüm 1): `{format_version, awpy_version, producer, tick_rate, snapshot_seconds,
header, reductions, <tablo adı>: {columns: [{name, dtype, values}]}, events: {olay: tablo}}`.
dtype adları: i32 u32 i64 u64 f32 bool str (awpy-python'un Polars'a verdiği dtype'lar).
f32 sütunlarında NaN / sonsuz "nan" / "inf" / "-inf" metni olarak gelir.

Gerçek awpy `Demo`'dan tek fark (içerik sonucu değiştirmez; `parse_demo` yalnız bunları okur):
- `grenades`: her mermi için yalnız ilk ve son tick satırı (`resolve_projectiles` bu kadarını okur);
- `shots`: yalnız utility atışları (atanı bulmak için kullanılan satırlar);
- `ticks(...)`: tüm tick'ler yerine yalnız kill / hasar / anlık görüntü tick'lerinin satırları.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

import polars as pl

from squadmind.parse.demo_parser import DemoTables, tables_from_demo

# Desteklenen paket biçimi sürümü (WASM crate'indeki FORMAT_VERSION ile aynı)
FORMAT_VERSION = 1

# Açılmış paket JSON'u için üst sınır (gerçek paketler birkaç on MB; bomba koruması)
MAX_JSON_BYTES = 256 * 1024 * 1024

# Paket dtype adı → Polars dtype (awpy-python'un `Column::new` ile çıkardığı türler)
DTYPES: dict[str, Any] = {
    "i32": pl.Int32,
    "u32": pl.UInt32,
    "i64": pl.Int64,
    "u64": pl.UInt64,
    "f32": pl.Float32,
    "bool": pl.Boolean,
    "str": pl.String,
}

# Paketteki ana tablolar (awpy `Demo` öznitelik adlarıyla aynı)
TABLE_NAMES = (
    "rounds", "round_economy", "bomb", "kills", "damages", "smokes", "fires", "blinds",
    "grenades", "shots", "players", "stats",
)

# Gerçek awpy `Demo` ile satır satır karşılaştırılabilen (indirgenmemiş) tablolar
RAW_TABLES = (
    "rounds", "round_economy", "bomb", "kills", "damages", "smokes", "fires", "blinds",
    "players", "stats",
)

# f32 sütunlarında JSON'a metin olarak yazılan özel değerler
_SPECIAL_FLOATS = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


class PackageError(ValueError):
    """Paket okunamıyor ya da desteklenmeyen bir sürümde."""


def _series(col: dict[str, Any]) -> pl.Series:
    """Paket sütununu Polars serisine çevirir (dtype paketteki adla belirlenir)."""
    name, dtype_name, values = col["name"], col["dtype"], col["values"]
    dtype = DTYPES.get(dtype_name)
    if dtype is None:
        raise PackageError(f"bilinmeyen dtype: {dtype_name!r} (sütun {name!r})")
    if dtype_name == "f32" and any(isinstance(v, str) for v in values):
        values = [_SPECIAL_FLOATS[v] if isinstance(v, str) else v for v in values]
    return pl.Series(name, values, dtype=dtype)


def _frame(table: dict[str, Any]) -> pl.DataFrame:
    """Paket tablosunu DataFrame'e çevirir."""
    return pl.DataFrame([_series(c) for c in table["columns"]])


class PackageEvents(dict):
    """`Demo.events` benzeri: olay adı → DataFrame (`in` ve `[]` çalışır)."""

    @property
    def names(self) -> list[str]:
        """Pakette bulunan olay adları (sıralı)."""
        return sorted(self)


class PackageDemo:
    """awpy `Demo` gibi davranan, paketten kurulan salt-okunur nesne.

    `parse_demo`'nun eriştiği her şey burada: header, tick_rate, rounds, round_economy,
    bomb, kills, damages, smokes, fires, blinds, grenades, shots, players, stats,
    events, snapshots(seconds=...) ve ticks(["m_szLastPlaceName"], players_only=True).
    """

    def __init__(self, package: dict[str, Any]) -> None:
        version = package.get("format_version")
        if version != FORMAT_VERSION:
            raise PackageError(
                f"desteklenmeyen paket sürümü: {version!r} (beklenen {FORMAT_VERSION})"
            )
        self.format_version: int = version
        self.awpy_version: str = package.get("awpy_version", "")
        self.producer: str = package.get("producer", "")
        self.reductions: list[str] = list(package.get("reductions", []))
        self.header: dict[str, Any] = dict(package["header"])
        # Python float'ı (f32'nin genişletilmiş hâli) awpy'deki `Demo.tick_rate` ile aynı
        self.tick_rate: float = float(package["tick_rate"])
        self._snapshot_seconds: float = float(package["snapshot_seconds"])
        for name in TABLE_NAMES:
            setattr(self, name, _frame(package[name]))
        # awpy'de `snapshots` bir yöntemdir (DataFrame döndürür); tablo ayrı saklanır
        self._snapshots_df: pl.DataFrame = _frame(package["snapshots"])
        self._places: pl.DataFrame = _frame(package["places"])
        self.events = PackageEvents(
            {name: _frame(table) for name, table in package.get("events", {}).items()}
        )

    def snapshots(self, *, seconds: float | None = None, **kwargs: Any) -> pl.DataFrame:
        """Paketlenmiş anlık görüntüler; yalnız paketin üretildiği aralık desteklenir."""
        if kwargs or seconds is None or abs(seconds - self._snapshot_seconds) > 1e-9:
            raise NotImplementedError(
                f"pakette yalnız snapshots(seconds={self._snapshot_seconds}) var"
            )
        return self._snapshots_df

    def ticks(self, props: list[str] | None = None, players_only: bool = True) -> pl.DataFrame:
        """Yer adı tick'leri (yalnız ["m_szLastPlaceName"], players_only=True)."""
        if props != ["m_szLastPlaceName"] or not players_only:
            raise NotImplementedError("pakette yalnız ticks(['m_szLastPlaceName']) var")
        return self._places


def load_package(source: Path | str | bytes) -> dict[str, Any]:
    """Paketi (gzip'li ya da düz JSON; yol ya da bayt) sözlüğe çevirir."""
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        raw = Path(source).read_bytes()
    try:
        if raw[:2] == b"\x1f\x8b":  # gzip sihirli baytları
            # Sıkıştırma bombasına karşı: açılmış boyut sınırlı (wbits=31: gzip başlığı)
            dec = zlib.decompressobj(wbits=31)
            raw = dec.decompress(raw, MAX_JSON_BYTES)
            if dec.unconsumed_tail:
                raise PackageError("paket açılınca beklenenden çok büyük")
        return json.loads(raw)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise PackageError(f"paket açılamadı: {exc}") from exc


def package_demo(source: Path | str | bytes) -> PackageDemo:
    """Yoldan ya da baytlardan `PackageDemo` kurar."""
    return PackageDemo(load_package(source))


def parse_package(source: Path | str | bytes) -> DemoTables:
    """Tarayıcı paketinden `parse_demo` ile aynı tabloları üretir."""
    return tables_from_demo(package_demo(source))


def installed_awpy_version() -> str | None:
    """Sunucudaki awpy sürümü (kurulu değilse None)."""
    from importlib import metadata

    try:
        return metadata.version("awpy")
    except metadata.PackageNotFoundError:
        return None


def check_compatibility(package: dict[str, Any], awpy_version: str | None = None) -> None:
    """Paketi üreten ayrıştırıcı sunucununkiyle aynı sürümde değilse PackageError atar.

    Farklı awpy sürümü farklı sütun / hesap demektir; sessizce karıştırmak yerine reddedilir.
    `awpy_version` verilmezse kurulu sürüm kullanılır (bulunamazsa kontrol atlanır).
    """
    version = package.get("format_version")
    if version != FORMAT_VERSION:
        raise PackageError(
            f"paket biçimi sürümü uyuşmuyor (pakette {version!r}, sunucuda {FORMAT_VERSION}); "
            "sayfayı yenileyip tekrar dene"
        )
    expected = awpy_version if awpy_version is not None else installed_awpy_version()
    got = package.get("awpy_version")
    if expected is not None and got != expected:
        raise PackageError(
            f"ayrıştırıcı sürümü uyuşmuyor (tarayıcı {got!r}, sunucu {expected!r}); "
            "tam demoyu yükle"
        )


def parse_package_checked(source: Path | str | bytes) -> DemoTables:
    """`parse_package` + sürüm uyumu kontrolü (web yüklemesinde kullanılır)."""
    package = load_package(source)
    check_compatibility(package)
    return tables_from_demo(PackageDemo(package))
