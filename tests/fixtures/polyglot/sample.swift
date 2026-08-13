protocol Pricer {
    func quote(id: String) -> Int
}

class PricingEngine: Pricer {
    let prices: [String: Int]

    init(prices: [String: Int]) {
        self.prices = prices
    }

    func quote(id: String) -> Int {
        return compute(id: id)
    }

    private func compute(id: String) -> Int {
        return prices.count
    }
}

struct OrderId {
    let value: String
}

func demo(prices: [String: Int]) -> Int {
    let engine = PricingEngine(prices: prices)
    return engine.quote(id: "x")
}
