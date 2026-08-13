package cache

type Store struct {
	items map[string]Item
	ttl   int
}

type Pricer interface {
	Quote(id string) (int, error)
}

func NewStore(ttl int) *Store {
	s := &Store{ttl: ttl}
	return s
}

func (s *Store) Get(id string) (Item, error) {
	v := s.lookup(id)
	return v, nil
}

func (s *Store) lookup(id string) Item { return s.items[id] }

const MaxItems = 10

const (
	Topic    = "items.changed"
	internal = 1
	First    = iota
)
