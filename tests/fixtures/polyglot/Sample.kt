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
