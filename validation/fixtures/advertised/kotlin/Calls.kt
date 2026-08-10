package gold.kotlinfixture

fun goldKotlinOrdered(value: Int): Int {
    goldKotlinFirst(value)
    goldKotlinSecond(value)
    return goldKotlinFirst(value)
}
