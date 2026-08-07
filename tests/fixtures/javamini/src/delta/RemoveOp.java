package shop.delta;

public record RemoveOp(String nodeId) implements DeltaOp {
    public int weight() {
        return 2;
    }
}
