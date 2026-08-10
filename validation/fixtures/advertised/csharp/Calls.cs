namespace Gold.CSharp;

public class GoldCSharpCalls {
    public static void GoldOrderedCaller() {
        GoldCSharpBase.GoldFirst();
        GoldCSharpBase.GoldSecond();
        GoldCSharpBase.GoldFirst();
    }
}
