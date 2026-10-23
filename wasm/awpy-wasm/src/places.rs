//! `Demo.ticks(["m_szLastPlaceName"], players_only=True)` karşılığı (yalnız gereken tick'ler).
//!
//! `awpy-python/src/lib.rs` içindeki `ticks_by_player` / `run_tick_segment` /
//! `tick_controller` mantığının tek parçalı (iş parçacıksız) ve tek özellikli kopyası.
//! SquadMind yer adını yalnızca kill / hasar / anlık görüntü tick'lerinde okur; bu yüzden
//! tüm tick'lerin (milyonlarca satır) yerine yalnız `wanted` kümesindeki tick'lerin satırları
//! döndürülür. Satırlar, tüm tick'lere uygulanan aynı mantıkla (oyuncu kimliği = peşinden
//! çözülen steamid) üretilir; yani `ticks()` çıktısının o tick'lerle süzülmüş hâlidir.

use std::collections::{HashMap, HashSet};

use awpy::{Entity, EntityContainer, EntityId, FieldValue, Parser};

use crate::columns::{Col, Table};

/// Oyuncu sınıfları (awpy-python `PLAYER_CLASSES`)
const PAWN: &str = "CCSPlayerPawn";
const CONTROLLER: &str = "CCSPlayerController";

/// Yer adı alanı (demodaki `m_szLastPlaceName`)
const PLACE_PROP: &str = "m_szLastPlaceName";

/// Alanı varlıktan oku (anahtar yoksa None)
fn read_field(entity: &Entity, key: Option<u64>) -> Option<&FieldValue> {
    key.and_then(|k| entity.fields.get(&k))
}

/// Tamsayı alanı u64'e çevir (negatif / tamsayı olmayan → None)
fn field_u64(value: &FieldValue) -> Option<u64> {
    match value {
        FieldValue::U64(n) => Some(*n),
        FieldValue::U32(n) => Some(u64::from(*n)),
        FieldValue::I64(n) => u64::try_from(*n).ok(),
        FieldValue::I32(n) => u64::try_from(*n).ok(),
        _ => None,
    }
}

/// Şu anki ya da hatırlanan kontrolcüyü, o da yoksa orijinal kontrolcüyü çöz
/// (awpy-python `tick_controller` kopyası).
fn tick_controller<'a>(
    pawn: &Entity,
    entities: &'a EntityContainer,
    current_key: Option<u64>,
    original_key: Option<u64>,
    remembered: &mut HashMap<EntityId, EntityId>,
) -> Option<&'a Entity> {
    let resolve = |key| {
        pawn.get_handle(key)
            .and_then(|handle| entities.get_by_handle(handle))
            .filter(|entity| entity.class_name.as_ref() == CONTROLLER)
    };
    if let Some(controller) = pawn
        .get_handle(current_key)
        .and_then(|handle| entities.get_by_handle(handle))
    {
        remembered.insert(pawn.id(), controller.id());
        return Some(controller);
    }
    if let Some(controller) = remembered
        .get(&pawn.id())
        .and_then(|id| entities.get(id.index).filter(|entity| entity.id() == *id))
    {
        return Some(controller);
    }
    let controller = resolve(original_key)?;
    remembered.insert(pawn.id(), controller.id());
    Some(controller)
}

/// Alan değerini metne çevir (awpy-python `InternedStrings::push_value`)
fn value_text(value: &FieldValue) -> String {
    if let FieldValue::String(bytes) = value {
        String::from_utf8_lossy(bytes).into_owned()
    } else {
        format!("{value}")
    }
}

/// `wanted` tick'lerindeki oyuncu satırları: `tick` (i64), `steamid` (u64, null olabilir),
/// `m_szLastPlaceName` (metin, null olabilir). Sütun adları/dtype'ları `ticks()` ile aynı.
pub fn collect_places(parser: &Parser, wanted: &HashSet<i32>) -> Result<Table<'static>, awpy::Error> {
    let filter: HashSet<&str> = HashSet::from([PAWN, CONTROLLER]);

    // Anahtarlar ilk tick'te, serileştiriciler sabit olduğundan bir kez çözülür
    struct Keys {
        pawn_id: i32,
        controller: Option<u64>,
        original_controller: Option<u64>,
        steamid: Option<u64>,
        place_pawn: Option<u64>,
        place_ctrl: Option<u64>,
    }
    let mut keys: Option<Keys> = None;
    let mut pawn_to_ctrl: HashMap<EntityId, EntityId> = HashMap::new();
    // Her peyn kimliği için görülen steamid (tüm tick'lerden); satırlar sonradan buradan doldurulur
    let mut identity_steamid: HashMap<EntityId, u64> = HashMap::new();
    let mut ticks: Vec<i64> = Vec::new();
    let mut pawn_ids: Vec<EntityId> = Vec::new();
    let mut places: Vec<Option<String>> = Vec::new();

    parser.decode_segment(None, i32::MAX, &filter, |ctx| {
        let k = keys.get_or_insert_with(|| {
            let pawn_ser = ctx.serializers().get(PAWN);
            let ctrl_ser = ctx.serializers().get(CONTROLLER);
            Keys {
                pawn_id: ctx.class_info().id_of(PAWN).unwrap_or(-1),
                controller: pawn_ser.and_then(|s| s.resolve_field_key("m_hController")),
                original_controller: pawn_ser
                    .and_then(|s| s.resolve_field_key("m_hOriginalController")),
                steamid: ctrl_ser.and_then(|s| s.resolve_field_key("m_steamID")),
                place_pawn: pawn_ser.and_then(|s| s.resolve_field_key(PLACE_PROP)),
                place_ctrl: ctrl_ser.and_then(|s| s.resolve_field_key(PLACE_PROP)),
            }
        });
        let is_wanted = wanted.contains(&ctx.tick());
        for (_, pawn) in ctx.entities().iter() {
            if !pawn.active || pawn.class_id != k.pawn_id {
                continue;
            }
            let pawn_id = pawn.id();
            // Kontrolcü çözümü her tick'te çalışmalı (hatırlama tablosu güncellensin)
            let controller = tick_controller(
                pawn,
                ctx.entities(),
                k.controller,
                k.original_controller,
                &mut pawn_to_ctrl,
            );
            if let Some(sid) = controller
                .and_then(|c| read_field(c, k.steamid))
                .and_then(field_u64)
            {
                identity_steamid.insert(pawn_id, sid);
            }
            if is_wanted {
                let value = read_field(pawn, k.place_pawn)
                    .or_else(|| controller.and_then(|c| read_field(c, k.place_ctrl)));
                ticks.push(i64::from(ctx.tick()));
                pawn_ids.push(pawn_id);
                places.push(value.map(value_text));
            }
        }
    })?;

    let steamids: Vec<Option<u64>> = pawn_ids
        .iter()
        .map(|id| identity_steamid.get(id).copied())
        .collect();
    Ok(Table::new(vec![
        Col::new("tick", ticks),
        Col::new("steamid", steamids),
        Col::new(PLACE_PROP, places),
    ]))
}
