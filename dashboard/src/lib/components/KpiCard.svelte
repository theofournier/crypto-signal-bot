<script lang="ts">
	interface Props {
		label: string;
		value: string;
		hint?: string;
		tone?: 'default' | 'pos' | 'neg' | 'muted';
	}
	let { label, value, hint = '', tone = 'default' }: Props = $props();
</script>

<div class="card kpi">
	<div class="label">{label}</div>
	<div class="value" class:pos={tone === 'pos'} class:neg={tone === 'neg'} class:muted={tone === 'muted'}>
		{value}
	</div>
	{#if hint}<div class="hint">{hint}</div>{/if}
</div>

<style>
	.kpi {
		padding: 0.85rem 1rem;
	}
	.label {
		color: var(--muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.value {
		font-size: 1.5rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		margin-top: 0.15rem;
	}
	.value.muted {
		color: var(--muted);
	}
	.hint {
		color: var(--muted);
		font-size: 0.75rem;
		margin-top: 0.15rem;
	}
</style>
