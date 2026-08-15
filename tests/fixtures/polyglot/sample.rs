pub struct Point { x: i64, y: i64 }

pub enum Axis { Horizontal, Vertical, Depth }

pub trait Locatable: Clone {
    fn locate(&self, id: u32) -> u64;
}

impl Locatable for Point {
    fn locate(&self, id: u32) -> u64 { self.translate(); 0 }
}

impl Point {
    pub fn new(x: i64, y: i64) -> Point {
        let point = Point { x, y };
        point
    }
    fn translate(&self) {}
}

pub fn collect(points: Vec<Point>) -> Vec<Point> { points }

pub const MAX_POINTS: usize = 10;

#[get("/points/{id}")]
pub fn find_point(id: u64) -> Point { Point { x: 1, y: 1 } }
