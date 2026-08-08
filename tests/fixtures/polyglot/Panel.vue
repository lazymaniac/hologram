<template><div id="panel">{{ title }}</div></template>
<script lang="ts">
export function usePanel(title: string): string {
  return title.trim();
}
</script>
