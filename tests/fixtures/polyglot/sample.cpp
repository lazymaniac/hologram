class Engine {
public:
    Engine(int n);
    int evaluate(int id) { return compute(id); }
private:
    int compute(int id);
    int prices;
};

int Engine::compute(int id) { return prices * id; }
