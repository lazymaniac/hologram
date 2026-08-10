package advertised

type GoldGoBase interface { GoldValue() int }
type GoldGoDerived interface {
	GoldGoBase
	GoldOther() int
}
func GoldGoFirst(value int) int { return value + 1 }
func GoldGoSecond(value int) int { return value * 2 }
