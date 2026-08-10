import { goldFirst, goldSecond } from "./types";

export function goldOrderedCaller(value: number): number {
  goldFirst(value);
  goldSecond(value);
  return goldFirst(value);
}
