package gold.java;

public final class Calls {
    public static void goldOrderedCaller() {
        GoldJavaBase.goldFirst();
        GoldJavaBase.goldSecond();
        GoldJavaBase.goldFirst();
    }
}
