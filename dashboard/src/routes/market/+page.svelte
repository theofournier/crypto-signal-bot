<script lang="ts">
	import type { PageProps } from './$types';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import CandleChart from '$lib/components/CandleChart.svelte';
	import LineChart from '$lib/components/LineChart.svelte';
	import KpiCard from '$lib/components/KpiCard.svelte';
	import DateFilter from '$lib/components/DateFilter.svelte';
	import { fmtPrice, fmtNum, fmtDate } from '$lib/format';

	let { data }: PageProps = $props();

	function pick(e: Event) {
		const p = new URLSearchParams(page.url.searchParams);
		p.set('symbol', (e.currentTarget as HTMLSelectElement).value);
		goto(`?${p.toString()}`, { keepFocus: true, noScroll: true });
	}

	const rsi = $derived(data.candles.filter((c) => c.rsi !== null).map((c) => ({ x: c.ts, y: c.rsi as number })));
	const vol = $derived(data.candles.map((c) => ({ x: c.ts, y: c.volume })));
	const l = $derived(data.latest);
</script>

<div class="page">
	<h1>Market</h1>
	<p class="sub">Closed 1h candles and derived indicators, straight from <span class="mono">market_data</span>.</p>

	<div class="controls">
		<label>Pair
			<select onchange={pick} value={data.symbol}>
				{#each data.symbols as s (s)}<option value={s}>{s}</option>{/each}
			</select>
		</label>
		{#if data.range}
			<DateFilter from={data.range.from} to={data.range.to} min={data.range.min} max={data.range.max} />
		{/if}
	</div>

	{#if l}
		<div class="grid kpis">
			<KpiCard label="Last close" value={fmtPrice(l.close)} hint={fmtDate(l.ts)} />
			<KpiCard label="RSI" value={fmtNum(l.rsi, 1)} tone={l.rsi != null && l.rsi >= 70 ? 'neg' : l.rsi != null && l.rsi <= 30 ? 'pos' : 'default'} />
			<KpiCard label="Vol ratio (30d)" value={l.volume_ratio == null ? '—' : `${fmtNum(l.volume_ratio, 2)}×`} />
			<KpiCard label="BB width" value={fmtNum(l.bb_width, 4)} />
			<KpiCard label="VWAP dist" value={l.vwap_distance == null ? '—' : `${fmtNum(l.vwap_distance, 2)}%`} tone={l.vwap_distance != null && l.vwap_distance >= 0 ? 'pos' : 'neg'} />
			<KpiCard label="Bid/ask imbalance" value={fmtNum(l.bid_ask_imbalance, 3)} />
		</div>
	{/if}

	<div class="card" style="margin-top:1rem">
		<h2 style="margin-top:0">{data.symbol} — price</h2>
		<CandleChart data={data.candles} height={300} />
	</div>

	<div class="grid cols-2" style="margin-top:1rem">
		<div class="card">
			<h2 style="margin-top:0">RSI</h2>
			<LineChart data={rsi} height={160} color="var(--accent)" yMin={0} yMax={100} baseline={50} format={(y) => y.toFixed(1)} />
			<p class="sub" style="margin:0.4rem 0 0">70 overbought · 30 oversold</p>
		</div>
		<div class="card">
			<h2 style="margin-top:0">Volume</h2>
			<LineChart data={vol} height={160} color="var(--muted)" format={(y) => fmtNum(y, 0)} />
		</div>
	</div>
</div>
