package shop.delta;

public record AddOp(String nodeId) implements DeltaOp {
    public int weight() {
        return 1;
    }
}
