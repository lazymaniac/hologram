package shop.transport;

public record Bicycle(String serial) implements Vehicle {
    public int wheels() {
        return 2;
    }
}
