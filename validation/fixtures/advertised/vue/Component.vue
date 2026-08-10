<script lang="ts">
export class GoldVueBase {}
export class GoldVueDerived extends GoldVueBase {}
export function goldVueFirst(value: number): number { return value + 1; }
</script>

<template><gold-vue-card id="gold-vue" /></template>
