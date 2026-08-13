package shop

case class OrderId(value: String)

trait Pricer {
  def quote(id: OrderId): Long
}

class PricingEngine(prices: Map[String, Long]) extends Pricer {
  def quote(id: OrderId): Long = {
    val total = compute(id)
    total
  }
  private def compute(id: OrderId): Long = prices.size.toLong
}

object Registry {
  def demo(prices: Map[String, Long]): Long = {
    val engine = new PricingEngine(prices)
    engine.quote(OrderId("x"))
  }
}
