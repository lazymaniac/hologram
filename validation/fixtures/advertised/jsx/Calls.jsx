import { goldJsxFirst } from "./Component.jsx";

export function goldJsxCall(value) {
  return <button onClick={() => goldJsxFirst(value)}>Gold</button>;
}
