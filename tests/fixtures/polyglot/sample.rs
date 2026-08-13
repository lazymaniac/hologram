pub struct Rational { num: i64, den: i64 }

pub enum Force { Asserted, Entailed, Supported }

pub trait Pricer: Clone {
    fn quote(&self, id: u32) -> u64;
}

impl Pricer for Rational {
    fn quote(&self, id: u32) -> u64 { self.reduce(); 0 }
}

impl Rational {
    pub fn of(num: i64, den: i64) -> Rational {
        let r = Rational { num, den };
        r
    }
    fn reduce(&self) {}
}

pub fn normalize(items: Vec<Rational>) -> Vec<Rational> { items }
