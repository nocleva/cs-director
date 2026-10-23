//! Sütun tabanlı tablo gösterimi (isim + dtype + değer dizisi).
//!
//! awpy-python'daki `col!` / `nullable_u64_column` / `df_from_columns` üçlüsünün
//! Polars'sız karşılığı: sütun adları ve sırası kaynakla aynı, dtype'lar Polars'ın
//! Rust türlerinden çıkardığı dtype'larla aynı (`i32` → Int32, `Option<u64>` → UInt64 ...).
//! Python tarafı (`squadmind/parse/package.py`) bu dtype adlarını Polars dtype'ına çevirir.

use std::borrow::Cow;

use serde::ser::{SerializeSeq, Serializer};
use serde::Serialize;

/// Bir sütunun değerleri; `None` = null.
pub enum Vals<'a> {
    I32(Vec<Option<i32>>),
    U32(Vec<Option<u32>>),
    I64(Vec<Option<i64>>),
    U64(Vec<Option<u64>>),
    F32(Vec<Option<f32>>),
    Bool(Vec<Option<bool>>),
    Str(Vec<Option<Cow<'a, str>>>),
}

impl Vals<'_> {
    /// Satır sayısı.
    pub fn len(&self) -> usize {
        match self {
            Vals::I32(v) => v.len(),
            Vals::U32(v) => v.len(),
            Vals::I64(v) => v.len(),
            Vals::U64(v) => v.len(),
            Vals::F32(v) => v.len(),
            Vals::Bool(v) => v.len(),
            Vals::Str(v) => v.len(),
        }
    }

    /// Aynı türdeki başka bir sütunu sona ekler (parça birleştirme).
    pub fn append(&mut self, other: Self) {
        match (self, other) {
            (Vals::I32(a), Vals::I32(b)) => a.extend(b),
            (Vals::U32(a), Vals::U32(b)) => a.extend(b),
            (Vals::I64(a), Vals::I64(b)) => a.extend(b),
            (Vals::U64(a), Vals::U64(b)) => a.extend(b),
            (Vals::F32(a), Vals::F32(b)) => a.extend(b),
            (Vals::Bool(a), Vals::Bool(b)) => a.extend(b),
            (Vals::Str(a), Vals::Str(b)) => a.extend(b),
            _ => panic!("farklı türde sütunlar birleştirilemez"),
        }
    }
}

impl Serialize for Vals<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            Vals::I32(v) => v.serialize(s),
            Vals::U32(v) => v.serialize(s),
            Vals::I64(v) => v.serialize(s),
            Vals::U64(v) => v.serialize(s),
            Vals::Bool(v) => v.serialize(s),
            Vals::Str(v) => v.serialize(s),
            Vals::F32(v) => {
                // JSON NaN / sonsuzu taşıyamaz: metin olarak yaz, Python'da geri çevrilir
                let mut seq = s.serialize_seq(Some(v.len()))?;
                for x in v {
                    match x {
                        None => seq.serialize_element(&None::<f32>)?,
                        Some(f) if f.is_finite() => seq.serialize_element(f)?,
                        Some(f) if f.is_nan() => seq.serialize_element("nan")?,
                        Some(f) if *f > 0.0 => seq.serialize_element("inf")?,
                        Some(_) => seq.serialize_element("-inf")?,
                    }
                }
                seq.end()
            }
        }
    }
}

/// Adı ve dtype'ı olan tek sütun.
#[derive(Serialize)]
pub struct Col<'a> {
    pub name: &'static str,
    pub dtype: &'static str,
    pub values: Vals<'a>,
}

impl<'a> Col<'a> {
    /// Rust türünden dtype çıkararak sütun kurar (Polars'ın `Column::new`'u gibi).
    pub fn new<T: IntoVals<'a>>(name: &'static str, values: Vec<T>) -> Self {
        let (dtype, values) = T::into_vals(values);
        Col { name, dtype, values }
    }
}

/// Bir Rust değer türünün sütun karşılığı.
pub trait IntoVals<'a>: Sized {
    fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>);
}

// Null olmayan ve null olabilen sayısal/bool türler
macro_rules! impl_plain {
    ($ty:ty, $variant:ident, $dtype:literal) => {
        impl<'a> IntoVals<'a> for $ty {
            fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
                ($dtype, Vals::$variant(v.into_iter().map(Some).collect()))
            }
        }
        impl<'a> IntoVals<'a> for Option<$ty> {
            fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
                ($dtype, Vals::$variant(v))
            }
        }
    };
}
impl_plain!(i32, I32, "i32");
impl_plain!(u32, U32, "u32");
impl_plain!(i64, I64, "i64");
impl_plain!(u64, U64, "u64");
impl_plain!(f32, F32, "f32");
impl_plain!(bool, Bool, "bool");

impl<'a> IntoVals<'a> for &'a str {
    fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
        ("str", Vals::Str(v.into_iter().map(|s| Some(Cow::Borrowed(s))).collect()))
    }
}
impl<'a> IntoVals<'a> for Option<&'a str> {
    fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
        ("str", Vals::Str(v.into_iter().map(|s| s.map(Cow::Borrowed)).collect()))
    }
}
impl<'a> IntoVals<'a> for String {
    fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
        ("str", Vals::Str(v.into_iter().map(|s| Some(Cow::Owned(s))).collect()))
    }
}
impl<'a> IntoVals<'a> for Option<String> {
    fn into_vals(v: Vec<Self>) -> (&'static str, Vals<'a>) {
        ("str", Vals::Str(v.into_iter().map(|s| s.map(Cow::Owned)).collect()))
    }
}

/// Tablo: sütunlar aynı uzunlukta.
#[derive(Serialize, Default)]
pub struct Table<'a> {
    pub columns: Vec<Col<'a>>,
}

impl<'a> Table<'a> {
    pub fn new(columns: Vec<Col<'a>>) -> Self {
        Table { columns }
    }

    /// Satır sayısı (ilk sütundan, awpy-python'daki `df_from_columns` gibi).
    pub fn height(&self) -> usize {
        self.columns.first().map_or(0, |c| c.values.len())
    }
}

/// `col!("ad", öğeler, |x| x.alan)`: awpy-python'daki makroyla aynı kullanım.
#[macro_export]
macro_rules! col {
    ($name:literal, $items:expr, $f:expr) => {
        $crate::columns::Col::new($name, $items.iter().map($f).collect::<Vec<_>>())
    };
}
