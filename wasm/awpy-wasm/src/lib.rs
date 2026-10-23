//! SquadMind WASM paketleyici.
//!
//! Tarayıcıda CS2 demosunu (awpy 3.1.1 çekirdeğiyle) ayrıştırır ve sunucuya gidecek
//! küçük bir "paket" üretir: `squadmind.parse.demo_parser.parse_demo`'nun okuduğu tüm
//! awpy veri setleri sütun tabanlı JSON olarak. Python tarafı (`squadmind.parse.package`)
//! paketi awpy `Demo` gibi davranan bir nesneye çevirir ve aynı tablo kurucularını kullanır.
//!
//! Tek dışa açık fonksiyon: [`parse_package`].

pub mod columns;
pub mod frames;
pub mod places;
pub mod snapshots;

use std::collections::{HashMap, HashSet};

use awpy::{EventDatasetSelection, Grenade, Parser, ProjectileSelection, Shot};
use serde::Serialize;
use wasm_bindgen::prelude::*;

use columns::{Col, Table};
use snapshots::SnapshotColumns;

/// Paket biçimi sürümü; Python tarafı bilmediği sürümü reddeder.
pub const FORMAT_VERSION: u32 = 1;
/// Ayrıştırıcının kaynağı olan awpy sürümü (Cargo.toml'daki etiketle aynı olmalı).
pub const AWPY_VERSION: &str = "3.1.1";
/// Anlık görüntü aralığı (saniye); `parse_demo` ile aynı (SNAPSHOT_SECONDS).
pub const SNAPSHOT_SECONDS: f32 = 2.0;

/// `parse_demo`'nun kullandığı ham olay adları (`_detonations`).
const RAW_EVENTS: [&str; 2] = ["flashbang_detonate", "hegrenade_detonate"];

/// `resolve_projectiles`'ın atan eşleştirmesinde kullandığı atış silahları
/// (`THROW_WEAPON_TO_TYPE` anahtarları). `shots` tablosu yalnız bu satırlara indirilir.
const THROW_WEAPONS: [&str; 6] = [
    "weapon_smokegrenade",
    "weapon_flashbang",
    "weapon_hegrenade",
    "weapon_molotov",
    "weapon_incgrenade",
    "weapon_decoy",
];

/// Paketin tamamı (serde ile JSON'a yazılır).
#[derive(Serialize)]
struct Package<'a> {
    format_version: u32,
    awpy_version: &'static str,
    /// Paketi üreten crate sürümü
    producer: String,
    /// `Demo.tick_rate` (f32'nin f64'e genişletilmiş hâli; Python float'ı ile aynı)
    tick_rate: f64,
    snapshot_seconds: f32,
    /// `Demo.header` sözlüğü
    header: serde_json::Value,
    /// Hangi tablolarda satır indirgemesi yapıldı (Python tarafı bilgi için okur)
    reductions: Vec<&'static str>,
    rounds: Table<'a>,
    round_economy: Table<'a>,
    bomb: Table<'a>,
    kills: Table<'a>,
    damages: Table<'a>,
    smokes: Table<'a>,
    fires: Table<'a>,
    blinds: Table<'a>,
    grenades: Table<'a>,
    shots: Table<'a>,
    players: Table<'a>,
    stats: Table<'a>,
    snapshots: Table<'a>,
    /// `ticks(["m_szLastPlaceName"], players_only=True)`, yalnız gereken tick'ler
    places: Table<'a>,
    /// Ham olaylar (yalnız `RAW_EVENTS`), olay adı → tablo
    events: std::collections::BTreeMap<String, Table<'a>>,
}

/// Mermi yörüngesini (tick başına satır) `resolve_projectiles`'ın okuduğu kadarına indirger.
///
/// `resolve_projectiles` her (entity_id, entity_serial, type) grubundan yalnız ilk ve son
/// tick'in konumunu / tick'ini ve atan alanlarının ilk dolu değerini okur. Burada her
/// grup için ilk satır (atan alanları = grubun ilk dolu değerleri) ve son satır tutulur;
/// ara satırlar atılır. Sonuç `resolve_projectiles` çıktısı bakımından aynıdır.
fn reduce_grenades(grenades: &[Grenade]) -> Vec<Grenade> {
    // Grupları ilk görülme sırasıyla topla
    let mut order: Vec<(i32, u32, &str)> = Vec::new();
    let mut groups: HashMap<(i32, u32, &str), Vec<&Grenade>> = HashMap::new();
    for g in grenades {
        let key = (g.entity_id, g.entity_serial, g.grenade_type.as_str());
        groups
            .entry(key)
            .or_insert_with(|| {
                order.push(key);
                Vec::new()
            })
            .push(g);
    }
    let mut out = Vec::new();
    for key in order {
        let mut rows = groups.remove(&key).unwrap_or_default();
        // Python tarafında grup tick'e göre sıralanır (grup içinde tick'ler benzersiz)
        rows.sort_by_key(|g| g.tick);
        let mut first = rows[0].clone();
        // Atan alanları: her biri bağımsız olarak ilk dolu değer (drop_nulls().first())
        first.thrower_name = rows.iter().find_map(|g| g.thrower_name.clone());
        first.thrower_steamid = rows.iter().find_map(|g| g.thrower_steamid);
        first.thrower_side = rows.iter().find_map(|g| g.thrower_side.clone());
        out.push(first);
        if rows.len() > 1 {
            let mut last = rows[rows.len() - 1].clone();
            last.thrower_name = None;
            last.thrower_steamid = None;
            last.thrower_side = None;
            out.push(last);
        }
    }
    out
}

/// Atışları yalnız utility fırlatmalarına indirger (`resolve_projectiles` bunları okur).
fn reduce_shots(shots: &[Shot]) -> Vec<Shot> {
    shots
        .iter()
        .filter(|s| THROW_WEAPONS.contains(&s.weapon.as_str()))
        .cloned()
        .collect()
}

/// `Demo.header` sözlüğünün JSON karşılığı.
fn header_json(parser: &Parser) -> Result<serde_json::Value, awpy::Error> {
    let h = parser.file_header()?;
    let mut v = serde_json::json!({
        "map_name": h.map_name,
        "server_name": h.server_name,
        "client_name": h.client_name,
        "build_num": h.build_num,
        "demo_version_name": h.demo_version_name,
        "game_directory": h.game_directory,
    });
    if let Ok(info) = parser.file_info() {
        v["playback_ticks"] = serde_json::json!(info.playback_ticks);
        v["playback_frames"] = serde_json::json!(info.playback_frames);
        v["playback_time"] = serde_json::json!(info.playback_time);
    }
    Ok(v)
}

/// Demoyu ayrıştırıp paketi (JSON, sıkıştırılmamış) üretir.
///
/// Hata durumunda metin döner (JS'e `JsError` olarak gider).
pub fn build_package(bytes: Vec<u8>) -> Result<Vec<u8>, String> {
    let e = |err: awpy::Error| format!("{err}");
    let parser = Parser::from_bytes(bytes);
    parser.verify().map_err(e)?;

    let tick_rate = parser.tickrate();
    let header = header_json(&parser).map_err(e)?;

    // Python'daki `Demo` ile aynı veri yolları (ayrı geçişler, aynı sonuç)
    let rounds = parser.rounds().map_err(e)?;
    let datasets = parser
        .event_datasets_selected(EventDatasetSelection::ALL)
        .map_err(e)?;
    let projectiles = parser
        .projectiles_selected(ProjectileSelection::ALL)
        .map_err(e)?;
    let stats = parser.player_stats_from_parts(
        &datasets.kills,
        &datasets.damages,
        &datasets.blinds,
        &datasets.shots,
        &rounds,
        true,
    );
    let players = parser.players().map_err(e)?;
    let economy = parser.round_economy().map_err(e)?;

    // Ham olaylar
    let all_events = parser.events_shared().map_err(e)?;
    let mut by_name: std::collections::BTreeMap<String, Vec<&awpy::GameEvent>> = Default::default();
    for ev in all_events.iter() {
        if RAW_EVENTS.contains(&ev.name.as_str()) {
            by_name.entry(ev.name.clone()).or_default().push(ev);
        }
    }
    let events: std::collections::BTreeMap<String, Table> = by_name
        .iter()
        .map(|(name, evs)| (name.clone(), frames::events_to_table(evs)))
        .collect();

    // Anlık görüntüler (her 2 sn): `Demo.snapshots(seconds=2.0)` ile aynı adım hesabı
    let stride = ((SNAPSHOT_SECONDS * tick_rate).round() as i32).max(1);
    let chunks = parser
        .snapshots_query_chunks::<SnapshotColumns>(Some(stride), &HashSet::new(), i32::MIN, i32::MAX)
        .map_err(e)?;
    let mut snap = SnapshotColumns::default();
    for chunk in chunks {
        snap.append(chunk);
    }

    // Yer adı gereken tick'ler: kill + hasar + anlık görüntü tick'leri
    let mut wanted: HashSet<i32> = HashSet::new();
    wanted.extend(datasets.kills.iter().map(|k| k.tick));
    wanted.extend(datasets.damages.iter().map(|d| d.tick));
    wanted.extend(snap.tick.iter().copied());
    let places = places::collect_places(&parser, &wanted).map_err(e)?;

    let grenades = reduce_grenades(&projectiles.grenades);
    let shots = reduce_shots(&datasets.shots);

    let package = Package {
        format_version: FORMAT_VERSION,
        awpy_version: AWPY_VERSION,
        producer: format!("awpy-wasm {}", env!("CARGO_PKG_VERSION")),
        tick_rate: f64::from(tick_rate),
        snapshot_seconds: SNAPSHOT_SECONDS,
        header,
        reductions: vec!["grenades:first_last_per_projectile", "shots:throw_weapons_only", "ticks:wanted_ticks_only"],
        rounds: frames::rounds_to_table(&rounds),
        round_economy: frames::round_economy_to_table(&economy),
        bomb: frames::bomb_to_table(&datasets.bomb),
        kills: frames::kills_to_table(&datasets.kills),
        damages: frames::damages_to_table(&datasets.damages),
        smokes: frames::smokes_to_table(&projectiles.smokes),
        fires: frames::fires_to_table(&projectiles.fires),
        blinds: frames::blinds_to_table(&datasets.blinds),
        grenades: frames::grenades_to_table(&grenades),
        shots: frames::shots_to_table(&shots),
        players: frames::players_to_table(&players),
        stats: frames::stats_to_table(&stats),
        snapshots: snap.into_table(),
        places,
        events,
    };
    serde_json::to_vec(&package).map_err(|err| format!("paket yazılamadı: {err}"))
}

/// Tarayıcıdan çağrılan tek giriş: açılmış `.dem` baytları → paket (JSON baytları).
///
/// Çağıran tarafın gzip'lemesi beklenir (`CompressionStream('gzip')`).
#[wasm_bindgen]
pub fn parse_package(bytes: Vec<u8>) -> Result<Vec<u8>, JsError> {
    console_error_panic_hook::set_once();
    build_package(bytes).map_err(|msg| JsError::new(&msg))
}

/// Paketi üreten sürüm bilgisi (hata ayıklama / uyumluluk denetimi için).
#[wasm_bindgen]
pub fn package_info() -> String {
    format!(
        "format_version={FORMAT_VERSION} awpy={AWPY_VERSION} crate={}",
        env!("CARGO_PKG_VERSION")
    )
}

// Kullanılmayan içe aktarma uyarısını önle (Col, tür çıkarımı için macro içinde kullanılır)
#[allow(dead_code)]
type _Col<'a> = Col<'a>;
