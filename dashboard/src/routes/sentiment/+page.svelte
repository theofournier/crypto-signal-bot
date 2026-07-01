<script lang="ts">
	import type { PageProps } from './$types';
	import { goto } from '$app/navigation';
	import LineChart from '$lib/components/LineChart.svelte';
	import { fmtNum } from '$lib/format';

	let { data }: PageProps = $props();

	function pick(e: Event) {
		const v = (e.currentTarget as HTMLSelectElement).value;
		goto(`/sentiment?symbol=${encodeURIComponent(v)}`, { keepFocus: true });
	}

	const symScore = $derived(data.series.map((s) => ({ x: s.ts, y: s.sentiment_score ?? 0 })));
	const mentions = $derived(data.series.map((s) => ({ x: s.ts, y: s.mention_count ?? 0 })));
	const totalMentions = $derived(data.sources.reduce((a, s) => a + s.n, 0));
</script>

<div class="page">
	<h1>Sentiment</h1>
	<p class="sub">Social &amp; market mood reduced to a directional score with credibility and novelty.</p>

	<div class="card" style="margin-bottom:1rem">
		<h2 style="margin-top:0">Fear &amp; Greed index</h2>
		<LineChart data={data.fearGreed} height={200} color="var(--amber)" yMin={-1} yMax={1} baseline={0} format={(y) => y.toFixed(2)} />
		<p class="sub" style="margin:0.4rem 0 0">−1 extreme fear · 0 neutral · +1 extreme greed</p>
	</div>

	<h2>Sources</h2>
	<div class="grid cols-3">
		{#each data.sources as src (src.source)}
			<div class="card">
				<div class="muted mono">{src.source}</div>
				<div style="font-size:1.3rem;font-weight:700">{fmtNum(src.n, 0)}</div>
				<div class="muted" style="font-size:0.8rem">{totalMentions ? ((src.n / totalMentions) * 100).toFixed(0) : 0}% of rows</div>
			</div>
		{/each}
	</div>

	<h2>Per-symbol sentiment</h2>
	<div class="controls">
		<label>Symbol
			<select onchange={pick} value={data.symbol ?? ''}>
				{#each data.symbols as s (s)}<option value={s}>{s}</option>{/each}
			</select>
		</label>
	</div>

	{#if data.symbol}
		<div class="grid cols-2">
			<div class="card">
				<h2 style="margin-top:0">{data.symbol} — sentiment score</h2>
				<LineChart data={symScore} height={180} color="var(--green)" yMin={-1} yMax={1} baseline={0} format={(y) => y.toFixed(2)} />
			</div>
			<div class="card">
				<h2 style="margin-top:0">{data.symbol} — mentions</h2>
				<LineChart data={mentions} height={180} color="var(--muted)" format={(y) => fmtNum(y, 0)} />
			</div>
		</div>
	{:else}
		<div class="card"><div class="empty">no per-symbol sentiment rows</div></div>
	{/if}
</div>
