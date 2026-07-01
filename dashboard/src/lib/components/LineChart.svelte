<script lang="ts">
	import { fmtDate, fmtNum } from '$lib/format';

	interface Point {
		x: number;
		y: number;
	}
	interface Props {
		data: Point[];
		height?: number;
		color?: string;
		fill?: boolean;
		/** force the y-axis range (e.g. [0,100] for scores) */
		yMin?: number | null;
		yMax?: number | null;
		/** draw a horizontal reference line at this y value */
		baseline?: number | null;
		/** how the hovered y value is formatted in the readout */
		format?: (y: number) => string;
	}
	let {
		data,
		height = 180,
		color = 'var(--accent)',
		fill = true,
		yMin = null,
		yMax = null,
		baseline = null,
		format = (y) => fmtNum(y)
	}: Props = $props();

	const W = 1000; // viewBox width; SVG scales to container
	const H = $derived(height);
	const pad = { t: 8, r: 8, b: 8, l: 8 };

	const xs = $derived(data.map((d) => d.x));
	const ys = $derived(data.map((d) => d.y));
	const xMin = $derived(Math.min(...xs));
	const xMax = $derived(Math.max(...xs));
	const lo = $derived(yMin ?? Math.min(...ys, baseline ?? Infinity));
	const hi = $derived(yMax ?? Math.max(...ys, baseline ?? -Infinity));

	function sx(x: number): number {
		if (xMax === xMin) return pad.l;
		return pad.l + ((x - xMin) / (xMax - xMin)) * (W - pad.l - pad.r);
	}
	function sy(y: number): number {
		if (hi === lo) return H / 2;
		return pad.t + (1 - (y - lo) / (hi - lo)) * (H - pad.t - pad.b);
	}

	const path = $derived(
		data.length ? data.map((d, i) => `${i ? 'L' : 'M'}${sx(d.x).toFixed(1)},${sy(d.y).toFixed(1)}`).join(' ') : ''
	);
	const area = $derived(
		data.length
			? `${path} L${sx(xMax).toFixed(1)},${(H - pad.b).toFixed(1)} L${sx(xMin).toFixed(1)},${(H - pad.b).toFixed(1)} Z`
			: ''
	);
	const baseY = $derived(baseline === null ? null : sy(baseline));

	let hover = $state<Point | null>(null);
	function onMove(e: MouseEvent) {
		if (!data.length) return;
		const svg = e.currentTarget as SVGSVGElement;
		const rect = svg.getBoundingClientRect();
		const px = ((e.clientX - rect.left) / rect.width) * W;
		// nearest point by x-pixel
		let best = data[0];
		let bd = Infinity;
		for (const d of data) {
			const dd = Math.abs(sx(d.x) - px);
			if (dd < bd) {
				bd = dd;
				best = d;
			}
		}
		hover = best;
	}
	const gid = 'g' + Math.random().toString(36).slice(2, 8);
</script>

<div class="chart">
	{#if data.length === 0}
		<div class="empty">no data</div>
	{:else}
		<svg
			viewBox="0 0 {W} {H}"
			preserveAspectRatio="none"
			role="img"
			aria-label="line chart"
			onmousemove={onMove}
			onmouseleave={() => (hover = null)}
		>
			<defs>
				<linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
					<stop offset="0%" stop-color={color} stop-opacity="0.28" />
					<stop offset="100%" stop-color={color} stop-opacity="0" />
				</linearGradient>
			</defs>
			{#if baseY !== null}
				<line x1={pad.l} x2={W - pad.r} y1={baseY} y2={baseY} class="baseline" />
			{/if}
			{#if fill}
				<path d={area} fill="url(#{gid})" stroke="none" />
			{/if}
			<path d={path} fill="none" stroke={color} stroke-width="2" vector-effect="non-scaling-stroke" />
			{#if hover}
				<line x1={sx(hover.x)} x2={sx(hover.x)} y1={pad.t} y2={H - pad.b} class="cursor" vector-effect="non-scaling-stroke" />
				<circle cx={sx(hover.x)} cy={sy(hover.y)} r="3" fill={color} />
			{/if}
		</svg>
		{#if hover}
			<div class="readout">
				<span class="mono">{format(hover.y)}</span>
				<span class="muted mono">{fmtDate(hover.x)}</span>
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
	.baseline {
		stroke: var(--muted);
		stroke-dasharray: 4 4;
		stroke-width: 1;
		opacity: 0.5;
	}
	.cursor {
		stroke: var(--muted);
		stroke-width: 1;
		opacity: 0.6;
	}
	.readout {
		position: absolute;
		top: 4px;
		right: 6px;
		display: flex;
		gap: 0.6rem;
		font-size: 0.78rem;
		background: color-mix(in srgb, var(--bg-elev) 85%, transparent);
		padding: 0.1rem 0.4rem;
		border-radius: 6px;
	}
	.readout .muted {
		color: var(--muted);
	}
	.empty {
		color: var(--muted);
		font-style: italic;
		padding: 1.5rem;
		text-align: center;
	}
</style>
