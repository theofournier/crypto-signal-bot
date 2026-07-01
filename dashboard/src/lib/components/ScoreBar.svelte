<script lang="ts">
	import { scoreColor } from '$lib/format';

	interface Props {
		value: number | null | undefined;
		/** draw a marker at the gate threshold */
		threshold?: number | null;
	}
	let { value, threshold = null }: Props = $props();

	const pct = $derived(value === null || value === undefined ? 0 : Math.max(0, Math.min(100, value)));
	const label = $derived(value === null || value === undefined ? '—' : value.toFixed(1));
</script>

<div class="wrap" title={threshold ? `threshold ${threshold}` : ''}>
	<div class="track">
		<div class="fill" style:width="{pct}%" style:background={scoreColor(value)}></div>
		{#if threshold}
			<div class="thr" style:left="{Math.min(100, threshold)}%"></div>
		{/if}
	</div>
	<span class="num mono">{label}</span>
</div>

<style>
	.wrap {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-width: 120px;
	}
	.track {
		position: relative;
		flex: 1;
		height: 8px;
		background: var(--bg-elev2);
		border-radius: 999px;
		overflow: hidden;
	}
	.fill {
		height: 100%;
		border-radius: 999px;
	}
	.thr {
		position: absolute;
		top: -2px;
		bottom: -2px;
		width: 2px;
		background: var(--text);
		opacity: 0.5;
	}
	.num {
		width: 2.6rem;
		text-align: right;
		font-size: 0.8rem;
		color: var(--muted);
	}
</style>
