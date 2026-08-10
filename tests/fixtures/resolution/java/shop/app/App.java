package shop.app;

import static shop.engine.Client.fetch;

public final class App {
  @Override
  public String toString() { return fetch(); }

  public static void main(String[] args) { fetch(); }
}
