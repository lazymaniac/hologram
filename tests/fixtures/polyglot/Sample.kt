package shop

data class OrderId(val value: String)

enum class Status { NEW, PAID }

interface Pricer {
    fun quote(id: OrderId): Long
}

class PricingEngine(private val prices: Map<String, Long>) : Pricer {
    override fun quote(id: OrderId): Long {
        val total = compute(id)
        return total
    }
    private fun compute(id: OrderId): Long = prices.size.toLong()
}

fun normalize(items: List<Long>): List<Long> = items

fun demo(prices: Map<String, Long>): Long {
    val engine = PricingEngine(prices)
    val backup: Pricer = engine
    return engine.quote(OrderId("x")) + backup.quote(OrderId("y"))
}
