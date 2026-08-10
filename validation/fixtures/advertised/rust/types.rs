pub trait GoldRustBase { fn gold_value(&self) -> i32; }
pub struct GoldRustDerived;
impl GoldRustBase for GoldRustDerived {
    fn gold_value(&self) -> i32 { 1 }
}
pub fn gold_rust_first(value: i32) -> i32 { value + 1 }
pub fn gold_rust_second(value: i32) -> i32 { value * 2 }
