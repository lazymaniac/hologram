export { fetch as load } from "./api";
import { onReady } from "./api";

configure({ callback: "onReady" });
const decoy = "onReady";
// onReady in a comment is not a reference.
