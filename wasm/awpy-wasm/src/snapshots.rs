//! `awpy-python/src/snapshot_columns.rs` kopyası: anlık görüntü sütunları.
//!
//! Alan sırası ve türleri kaynakla birebir aynı; fark: çıktı Polars yerine `Table`.

use awpy::PlayerState;

use crate::columns::{Col, Table};

// Depolama ve çıktı sırası tek şema bildiriminde
macro_rules! snapshot_columns {
    ($($field:ident: $ty:ty),+ $(,)?) => {
        #[derive(Default)]
        pub struct SnapshotColumns { $(pub $field: Vec<$ty>),+ }

        impl SnapshotColumns {
            pub fn push(&mut self, state: PlayerState) {
                $(self.$field.push(state.$field);)+
            }

            /// Başka bir parçayı sona ekler (parçalar tick sırasıyla gelir).
            pub fn append(&mut self, other: Self) {
                $(self.$field.extend(other.$field);)+
            }

            pub fn into_table(self) -> Table<'static> {
                Table::new(vec![$(Col::new(stringify!($field), self.$field)),+])
            }
        }
    };
}

snapshot_columns! {
    tick: i32,
    steamid: Option<u64>,
    name: Option<String>,
    side: Option<&'static str>,
    x: Option<f32>,
    y: Option<f32>,
    z: Option<f32>,
    velocity_x: Option<f32>,
    velocity_y: Option<f32>,
    velocity_z: Option<f32>,
    velocity: Option<f32>,
    pitch: f32,
    yaw: f32,
    health: i32,
    armor: i32,
    has_helmet: bool,
    has_defuser: bool,
    has_bomb: bool,
    active_weapon: Option<&'static str>,
    primary_weapon: Option<&'static str>,
    secondary_weapon: Option<&'static str>,
    fire_grenades: i32,
    smoke_grenades: i32,
    he_grenades: i32,
    flashbangs: i32,
    decoy_grenades: i32,
    equipment_value: i32,
    equipment_value_round_start: i32,
    money: i32,
    is_crouched: bool,
    is_walking: bool,
    is_jumping: bool,
    is_in_bomb_zone: bool,
    is_scoped: bool,
    is_reloading: Option<bool>,
    is_silent_reloading: Option<bool>,
    is_defusing: bool,
    is_blinded: bool,
    flash_duration: f32,
    inventory: String,
}

impl Extend<PlayerState> for SnapshotColumns {
    fn extend<T: IntoIterator<Item = PlayerState>>(&mut self, rows: T) {
        for row in rows {
            self.push(row);
        }
    }
}
