package shop.engine;

public final class Client {
  @Bean
  public static String fetch() { return "ok"; }

  @EventListener("fetch")
  public void listen() {
    String decoy = "fetch";
    // fetch in a comment is not a reference.
  }
}
