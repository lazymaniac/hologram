package advertised

func GoldGoOrdered(value int) int {
	GoldGoFirst(value)
	GoldGoSecond(value)
	return GoldGoFirst(value)
}
