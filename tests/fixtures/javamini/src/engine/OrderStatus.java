package shop.engine;

public enum OrderStatus {
    NEW,
    PAID,
    SHIPPED;

    public boolean isTerminal() {
        return this == SHIPPED;
    }
}
