<script lang="ts">
	import { fmtDate, fmtPrice } from '$lib/format';

	interface Candle {
		ts: number;
		open: number;
		high: number;
		low: number;
		close: number;
	}
	interface Props {
		data: Candle[];
		height?: number;
	}
	let { data, height = 260 }: Props = $props();

	const W = 1000;
	const H = $derived(height);
	const pad = { t: 10, b: 10 };

	const highs = $derived(data.map((d) => d.high));
	const lows = $derived(data.map((d) => d.low));
	const hi = $derived(Math.max(...highs));
	const lo = $derived(Math.min(...lows));
	const n = $derived(data.length);
	const slot = $derived(n > 0 ? W / n : W);
	const bodyW = $derived(Math.max(1, slot * 0.6));

	function cx(i: number): number {
		return i * slot + slot / 2;
	}
	function py(p: number): number {
		if (hi === lo) return H / 2;
		return pad.t + (1 - (p - lo) / (hi - lo)) * (H - pad.t - pad.b);
	}

	let hoverIdx = $state<number | null>(null);
	const hover = $derived(hoverIdx !== null ? data[hoverIdx] : null);
	function onMove(e: MouseEvent) {
		if (!n) return;
		const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
		const px = ((e.clientX - rect.left) / rect.width) * W;
		hoverIdx = Math.max(0, Math.min(n - 1, Math.floor(px / slot)));
	}
</script>

<div class="chart">
	{#if n === 0}
		<div class="empty">no candles</div>
	{:else}
		<svg
			viewBox="0 0 {W} {H}"
			preserveAspectRatio="none"
			role="img"
			aria-label="candlestick chart"
			onmousemove={onMove}
			onmouseleave={() => (hoverIdx = null)}
		>
			{#each data as c, i (c.ts)}
				{@const up = c.close >= c.open}
				{@const col = up ? 'var(--green)' : 'var(--red)'}
				{@const bodyTop = py(Math.max(c.open, c.close))}
				{@const bodyBot = py(Math.min(c.open, c.close))}
				<line x1={cx(i)} x2={cx(i)} y1={py(c.high)} y2={py(c.low)} stroke={col} stroke-width="1" vector-effect="non-scaling-stroke" />
				<rect
					x={cx(i) - bodyW / 2}
					y={bodyTop}
					width={bodyW}
					height={Math.max(1, bodyBot - bodyTop)}
					fill={col}
				/>
			{/each}
			{#if hoverIdx !== null}
				<line x1={cx(hoverIdx)} x2={cx(hoverIdx)} y1={pad.t} y2={H - pad.b} class="cursor" vector-effect="non-scaling-stroke" />
			{/if}
		</svg>
		{#if hover}
			<div class="readout mono">
				<span class="muted">{fmtDate(hover.ts)}</span>
				<span>O {fmtPrice(hover.open)}</span>
				<span>H {fmtPrice(hover.high)}</span>
				<span>L {fmtPrice(hover.low)}</span>
				<span class:pos={hover.close >= hover.open} class:neg={hover.close < hover.open}>C {fmtPrice(hover.close)}</span>
			</div>
		{/if}
	{/if}
</div>

<style>
	.chart {
		position: relative;
		width: 100%;
	}
	svg {
		width: 100%;
		display: block;
	}
	.cursor {
		stroke: var(--muted);
		stroke-width: 1;
		opacity: 0.6;
	}
	.readout {
		position: absolute;
		top: 4px;
		left: 6px;
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		font-size: 0.78rem;
		background: color-mix(in srgb, var(--bg-elev) 85%, transparent);
		padding: 0.15rem 0.5rem;
		border-radius: 6px;
	}
	.readout .muted {
		color: var(--muted);
	}
	.empty {
		color: var(--muted);
		font-style: italic;
		padding: 2rem;
		text-align: center;
	}
</style>
