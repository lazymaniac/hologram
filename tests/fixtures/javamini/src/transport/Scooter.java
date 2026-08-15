package shop.transport;

public record Scooter(String serial) implements Vehicle {
    public int wheels() {
        return 2;
    }
}
