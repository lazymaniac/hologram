<script context="module" lang="ts">
import { goldSvelteFirst } from "./Component.svelte";
export function goldSvelteCall(value: number): number { return goldSvelteFirst(value); }
</script>

<button>Gold</button>
