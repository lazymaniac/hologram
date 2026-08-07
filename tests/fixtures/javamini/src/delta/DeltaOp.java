package shop.delta;

public sealed interface DeltaOp permits AddOp, RemoveOp {
    int weight();
}
