//! awpy-python `lib.rs`'deki `*_to_frame` fonksiyonlarının kopyası.
//!
//! Sütun adları, sıraları ve Rust türleri kaynakla birebir aynı (awpy-python-v3.1.1);
//! tek fark çıktının Polars DataFrame yerine `Table` olması. Kaynak: `crates/awpy-python/
//! src/lib.rs` (`rounds_to_frame` ... `stats_to_frame`).

use awpy::{
    Blind, BombEvent, Damage, Fire, Grenade, Kill, Player, PlayerStats, Round, RoundEconomy, Shot,
    Smoke,
};

use crate::col;
use crate::columns::{Col, Table};

pub fn rounds_to_table(rounds: &[Round]) -> Table<'_> {
    Table::new(vec![
        col!("round_num", rounds, |r| r.round_num),
        col!("start_tick", rounds, |r| r.start_tick),
        col!("freeze_end_tick", rounds, |r| r.freeze_end_tick),
        col!("end_tick", rounds, |r| r.end_tick),
        col!("official_end_tick", rounds, |r| r.official_end_tick),
        col!("winner", rounds, |r| r.winner),
        col!("winner_side", rounds, |r| r.winner_side.as_str()),
        col!("reason", rounds, |r| r.reason),
        col!("reason_name", rounds, |r| r.reason_name.as_str()),
        col!("is_knife_round", rounds, |r| r.is_knife_round),
    ])
}

pub fn round_economy_to_table(econ: &[RoundEconomy]) -> Table<'_> {
    Table::new(vec![
        col!("round_num", econ, |e| e.round_num),
        col!("side", econ, |e| e.side),
        col!("equipment_value", econ, |e| e.equipment_value),
        col!("buy_type", econ, |e| e.buy_type),
        col!("n_players", econ, |e| e.n_players),
    ])
}

pub fn kills_to_table(kills: &[Kill]) -> Table<'_> {
    Table::new(vec![
        col!("attacker_entity_id", kills, |k| k.attacker_entity_id),
        col!("attacker_entity_serial", kills, |k| k.attacker_entity_serial),
        col!("attacker_steamid", kills, |k| k.attacker_steamid),
        col!("attacker_name", kills, |k| k.attacker_name.as_deref()),
        col!("attacker_side", kills, |k| k.attacker_side.as_deref()),
        col!("attacker_x", kills, |k| k.attacker_x),
        col!("attacker_y", kills, |k| k.attacker_y),
        col!("attacker_z", kills, |k| k.attacker_z),
        col!("victim_entity_id", kills, |k| k.victim_entity_id),
        col!("victim_entity_serial", kills, |k| k.victim_entity_serial),
        col!("victim_steamid", kills, |k| k.victim_steamid),
        col!("victim_name", kills, |k| k.victim_name.as_deref()),
        col!("victim_side", kills, |k| k.victim_side.as_deref()),
        col!("victim_x", kills, |k| k.victim_x),
        col!("victim_y", kills, |k| k.victim_y),
        col!("victim_z", kills, |k| k.victim_z),
        col!("assister_entity_id", kills, |k| k.assister_entity_id),
        col!("assister_entity_serial", kills, |k| k.assister_entity_serial),
        col!("assister_steamid", kills, |k| k.assister_steamid),
        col!("assister_name", kills, |k| k.assister_name.as_deref()),
        col!("assister_side", kills, |k| k.assister_side.as_deref()),
        col!("assister_x", kills, |k| k.assister_x),
        col!("assister_y", kills, |k| k.assister_y),
        col!("assister_z", kills, |k| k.assister_z),
        col!("weapon", kills, |k| k.weapon.as_str()),
        col!("headshot", kills, |k| k.headshot),
        col!("assist_flash", kills, |k| k.assist_flash),
        col!("dominated", kills, |k| k.dominated),
        col!("noscope", kills, |k| k.noscope),
        col!("penetrated", kills, |k| k.penetrated),
        col!("revenge", kills, |k| k.revenge),
        col!("thrusmoke", kills, |k| k.thrusmoke),
        col!("hitgroup", kills, |k| k.hitgroup),
        col!("hitgroup_name", kills, |k| k.hitgroup_name.as_str()),
        col!("is_trade", kills, |k| k.is_trade),
        col!("victim_traded", kills, |k| k.victim_traded),
        col!("tick", kills, |k| k.tick),
    ])
}

pub fn damages_to_table(damages: &[Damage]) -> Table<'_> {
    Table::new(vec![
        col!("attacker_entity_id", damages, |d| d.attacker_entity_id),
        col!("attacker_entity_serial", damages, |d| d.attacker_entity_serial),
        col!("attacker_steamid", damages, |d| d.attacker_steamid),
        col!("attacker_name", damages, |d| d.attacker_name.as_deref()),
        col!("attacker_side", damages, |d| d.attacker_side.as_deref()),
        col!("attacker_x", damages, |d| d.attacker_x),
        col!("attacker_y", damages, |d| d.attacker_y),
        col!("attacker_z", damages, |d| d.attacker_z),
        col!("victim_entity_id", damages, |d| d.victim_entity_id),
        col!("victim_entity_serial", damages, |d| d.victim_entity_serial),
        col!("victim_steamid", damages, |d| d.victim_steamid),
        col!("victim_name", damages, |d| d.victim_name.as_deref()),
        col!("victim_side", damages, |d| d.victim_side.as_deref()),
        col!("victim_x", damages, |d| d.victim_x),
        col!("victim_y", damages, |d| d.victim_y),
        col!("victim_z", damages, |d| d.victim_z),
        col!("weapon", damages, |d| d.weapon.as_str()),
        col!("dmg_health", damages, |d| d.dmg_health),
        col!("dmg_armor", damages, |d| d.dmg_armor),
        col!("hitgroup", damages, |d| d.hitgroup),
        col!("hitgroup_name", damages, |d| d.hitgroup_name.as_str()),
        col!("health_pre", damages, |d| d.health_pre),
        col!("health_post", damages, |d| d.health_post),
        col!("armor_pre", damages, |d| d.armor_pre),
        col!("armor_post", damages, |d| d.armor_post),
        col!("tick", damages, |d| d.tick),
    ])
}

pub fn bomb_to_table(bomb: &[BombEvent]) -> Table<'_> {
    Table::new(vec![
        col!("tick", bomb, |b| b.tick),
        col!("event", bomb, |b| b.event.as_str()),
        col!("entity_id", bomb, |b| b.entity_id),
        col!("entity_serial", bomb, |b| b.entity_serial),
        col!("steamid", bomb, |b| b.steamid),
        col!("name", bomb, |b| b.name.as_deref()),
        col!("bombsite", bomb, |b| b.bombsite.as_deref()),
        col!("x", bomb, |b| b.x),
        col!("y", bomb, |b| b.y),
        col!("z", bomb, |b| b.z),
    ])
}

pub fn grenades_to_table(grenades: &[Grenade]) -> Table<'_> {
    Table::new(vec![
        col!("tick", grenades, |g| g.tick),
        col!("thrower_name", grenades, |g| g.thrower_name.as_deref()),
        col!("thrower_steamid", grenades, |g| g.thrower_steamid),
        col!("thrower_side", grenades, |g| g.thrower_side.as_deref()),
        col!("type", grenades, |g| g.grenade_type.as_str()),
        col!("entity_id", grenades, |g| g.entity_id),
        col!("entity_serial", grenades, |g| g.entity_serial),
        col!("x", grenades, |g| g.x),
        col!("y", grenades, |g| g.y),
        col!("z", grenades, |g| g.z),
    ])
}

pub fn fires_to_table(fires: &[Fire]) -> Table<'_> {
    Table::new(vec![
        col!("start_tick", fires, |f| f.start_tick),
        col!("end_tick", fires, |f| f.end_tick),
        col!("thrower_name", fires, |f| f.thrower_name.as_deref()),
        col!("thrower_steamid", fires, |f| f.thrower_steamid),
        col!("thrower_side", fires, |f| f.thrower_side.as_deref()),
        col!("type", fires, |f| f.fire_type.as_str()),
        col!("entity_id", fires, |f| f.entity_id),
        col!("entity_serial", fires, |f| f.entity_serial),
        col!("x", fires, |f| f.x),
        col!("y", fires, |f| f.y),
        col!("z", fires, |f| f.z),
    ])
}

pub fn smokes_to_table(smokes: &[Smoke]) -> Table<'_> {
    Table::new(vec![
        col!("start_tick", smokes, |s| s.start_tick),
        col!("end_tick", smokes, |s| s.end_tick),
        col!("thrower_name", smokes, |s| s.thrower_name.as_deref()),
        col!("thrower_steamid", smokes, |s| s.thrower_steamid),
        col!("thrower_side", smokes, |s| s.thrower_side.as_deref()),
        col!("entity_id", smokes, |s| s.entity_id),
        col!("entity_serial", smokes, |s| s.entity_serial),
        col!("x", smokes, |s| s.x),
        col!("y", smokes, |s| s.y),
        col!("z", smokes, |s| s.z),
    ])
}

pub fn shots_to_table(shots: &[Shot]) -> Table<'_> {
    Table::new(vec![
        col!("tick", shots, |s| s.tick),
        col!("entity_id", shots, |s| s.entity_id),
        col!("entity_serial", shots, |s| s.entity_serial),
        col!("steamid", shots, |s| s.steamid),
        col!("name", shots, |s| s.name.as_deref()),
        col!("side", shots, |s| s.side.as_deref()),
        col!("x", shots, |s| s.x),
        col!("y", shots, |s| s.y),
        col!("z", shots, |s| s.z),
        col!("pitch", shots, |s| s.pitch),
        col!("yaw", shots, |s| s.yaw),
        col!("weapon", shots, |s| s.weapon.as_str()),
        col!("scoped", shots, |s| s.scoped),
        col!("inaccuracy", shots, |s| s.inaccuracy),
        col!("num_bullets_remaining", shots, |s| s.num_bullets_remaining),
    ])
}

pub fn players_to_table(players: &[Player]) -> Table<'_> {
    Table::new(vec![
        col!("steamid", players, |p| p.steamid),
        col!("name", players, |p| p.name.as_deref()),
        col!("side", players, |p| p.side.as_deref()),
        col!("team_clan_name", players, |p| p.team_clan_name.as_deref()),
    ])
}

pub fn blinds_to_table(blinds: &[Blind]) -> Table<'_> {
    Table::new(vec![
        col!("tick", blinds, |b| b.tick),
        col!("attacker_entity_id", blinds, |b| b.attacker_entity_id),
        col!("attacker_entity_serial", blinds, |b| b.attacker_entity_serial),
        col!("attacker_steamid", blinds, |b| b.attacker_steamid),
        col!("attacker_name", blinds, |b| b.attacker_name.as_deref()),
        col!("attacker_side", blinds, |b| b.attacker_side.as_deref()),
        col!("attacker_x", blinds, |b| b.attacker_x),
        col!("attacker_y", blinds, |b| b.attacker_y),
        col!("attacker_z", blinds, |b| b.attacker_z),
        col!("victim_entity_id", blinds, |b| b.victim_entity_id),
        col!("victim_entity_serial", blinds, |b| b.victim_entity_serial),
        col!("victim_steamid", blinds, |b| b.victim_steamid),
        col!("victim_name", blinds, |b| b.victim_name.as_deref()),
        col!("victim_side", blinds, |b| b.victim_side.as_deref()),
        col!("victim_x", blinds, |b| b.victim_x),
        col!("victim_y", blinds, |b| b.victim_y),
        col!("victim_z", blinds, |b| b.victim_z),
        col!("duration", blinds, |b| b.duration),
    ])
}

pub fn stats_to_table(stats: &[PlayerStats]) -> Table<'_> {
    Table::new(vec![
        col!("steamid", stats, |s| s.steamid),
        col!("name", stats, |s| s.name.as_str()),
        col!("rounds_played", stats, |s| s.rounds_played),
        col!("kills", stats, |s| s.kills),
        col!("deaths", stats, |s| s.deaths),
        col!("assists", stats, |s| s.assists),
        col!("flash_assists", stats, |s| s.flash_assists),
        col!("headshot_kills", stats, |s| s.headshot_kills),
        col!("headshot_pct", stats, |s| s.headshot_pct),
        col!("opening_kills", stats, |s| s.opening_kills),
        col!("opening_deaths", stats, |s| s.opening_deaths),
        col!("traded_deaths", stats, |s| s.traded_deaths),
        col!("multikill_2k", stats, |s| s.multikill_2k),
        col!("multikill_3k", stats, |s| s.multikill_3k),
        col!("multikill_4k", stats, |s| s.multikill_4k),
        col!("multikill_5k", stats, |s| s.multikill_5k),
        col!("kast", stats, |s| s.kast),
        col!("adr", stats, |s| s.adr),
        col!("clutches_played", stats, |s| s.clutches_played),
        col!("clutches_won", stats, |s| s.clutches_won),
        col!("clutch_1v1", stats, |s| s.clutch_1v1),
        col!("clutch_1v2", stats, |s| s.clutch_1v2),
        col!("clutch_1v3", stats, |s| s.clutch_1v3),
        col!("clutch_1v4", stats, |s| s.clutch_1v4),
        col!("clutch_1v5", stats, |s| s.clutch_1v5),
        col!("utility_damage", stats, |s| s.utility_damage),
        col!("flashes_thrown", stats, |s| s.flashes_thrown),
        col!("enemies_flashed", stats, |s| s.enemies_flashed),
        col!("flash_duration_dealt", stats, |s| s.flash_duration_dealt),
    ])
}

/// Ham olay tablosu (`events_to_frame` kopyası): `tick` + olayın her anahtarı için metin sütunu.
/// Anahtar adları dinamik olduğu için sütun adları sızdırılmış (`Box::leak`) `'static` metin;
/// paket başına birkaç düzine küçük metin.
pub fn events_to_table<'a>(events: &[&'a awpy::GameEvent]) -> Table<'a> {
    // Anahtarları ilk göründükleri sırayla topla
    let mut seen = std::collections::HashSet::new();
    let mut key_order: Vec<&str> = Vec::new();
    for event in events {
        for (key, _) in &event.keys {
            if seen.insert(key.as_str()) {
                key_order.push(key);
            }
        }
    }
    let ticks: Vec<i64> = events.iter().map(|e| i64::from(e.tick)).collect();
    let mut columns: Vec<Col<'a>> = vec![Col::new("tick", ticks)];
    for key in key_order {
        let values: Vec<Option<&str>> = events
            .iter()
            .map(|e| e.keys.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str()))
            .collect();
        let name: &'static str = Box::leak(key.to_owned().into_boxed_str());
        columns.push(Col::new(name, values));
    }
    Table::new(columns)
}
