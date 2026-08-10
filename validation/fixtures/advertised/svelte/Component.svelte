<script context="module" lang="ts">
export class GoldSvelteBase {}
export class GoldSvelteDerived extends GoldSvelteBase {}
export function goldSvelteFirst(value: number): number { return value + 1; }
</script>

<gold-svelte-card id="gold-svelte" />
