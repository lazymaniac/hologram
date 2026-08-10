import { goldJsFirst, goldJsSecond } from "./types.js";

export function goldJsOrdered(value) {
  goldJsFirst(value);
  goldJsSecond(value);
  return goldJsFirst(value);
}
