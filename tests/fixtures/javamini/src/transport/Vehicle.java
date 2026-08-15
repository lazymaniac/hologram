package shop.transport;

public sealed interface Vehicle permits Bicycle, Scooter {
    int wheels();
}
