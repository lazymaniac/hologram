use crate::rust::types::{gold_rust_first, gold_rust_second};

pub fn gold_rust_ordered(value: i32) -> i32 {
    gold_rust_first(value);
    gold_rust_second(value);
    gold_rust_first(value)
}
