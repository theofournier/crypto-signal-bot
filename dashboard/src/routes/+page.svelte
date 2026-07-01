<script lang="ts">
	import type { PageProps } from './$types';
	import KpiCard from '$lib/components/KpiCard.svelte';
	import ScoreBar from '$lib/components/ScoreBar.svelte';
	import LineChart from '$lib/components/LineChart.svelte';
	import { fmtDay, fmtNum, fmtUsd, directionColor } from '$lib/format';

	let { data }: PageProps = $props();

	const ss = $derived(data.signalStats);
	const ts = $derived(data.tradeStats);
	const coverageMap = $derived(new Map(data.coverage.map((c) => [c.table, c])));
	function rows(t: string): string {
		return fmtNum(coverageMap.get(t)?.rows ?? 0, 0);
	}
	const winRate = $derived(ts.win_rate === null ? '—' : `${(ts.win_rate * 100).toFixed(1)}%`);
</script>

<div class="page">
	<h1>Overview</h1>
	<p class="sub">Single-glance status of everything the bot has observed and decided.</p>

	<div class="grid kpis">
		<KpiCard label="Signals evaluated" value={fmtNum(ss.total, 0)} hint="{fmtNum(ss.gated ?? 0, 0)} passed the gate" />
		<KpiCard
			label="Max composite"
			value={ss.max_composite === null ? '—' : ss.max_composite.toFixed(1)}
			hint="threshold {data.threshold ?? '—'}"
			tone={data.threshold && ss.max_composite && ss.max_composite >= data.threshold ? 'pos' : 'muted'}
		/>
		<KpiCard label="Closed trades" value={fmtNum(ts.closed, 0)} hint="{fmtNum(ts.open, 0)} open" />
		<KpiCard
			label="Win rate"
			value={winRate}
			hint={ts.closed < 100 ? `${ts.closed}/100 for a verdict` : 'sample sufficient'}
			tone="muted"
		/>
		<KpiCard
			label="Net P&L (after fees)"
			value={ts.net_pnl === null ? '—' : fmtUsd(ts.net_pnl)}
			tone={ts.net_pnl === null ? 'muted' : ts.net_pnl >= 0 ? 'pos' : 'neg'}
		/>
	</div>

	<div class="grid cols-2" style="margin-top:1rem">
		<div class="card">
			<h2 style="margin-top:0">Latest signal per symbol</h2>
			{#if data.latestSignals.length === 0}
				<div class="empty">no signals yet</div>
			{:else}
				<div class="table-wrap">
					<table>
						<thead>
							<tr>
								<th>Symbol</th>
								<th>Composite</th>
								<th>Market</th>
								<th>On-chain</th>
								<th>Sent.</th>
								<th>Dir</th>
							</tr>
						</thead>
						<tbody>
							{#each data.latestSignals as s (s.symbol)}
								<tr>
									<td><a href="/signals?symbol={encodeURIComponent(s.symbol)}">{s.symbol}</a></td>
									<td style="min-width:150px"><ScoreBar value={s.composite} threshold={data.threshold} /></td>
									<td class="mono">{fmtNum(s.market_sub, 0)}</td>
									<td class="mono">{fmtNum(s.onchain_sub, 0)}</td>
									<td class="mono">{fmtNum(s.sentiment_sub, 0)}</td>
									<td><span style:color={directionColor(s.direction)}>{s.direction}</span></td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<div class="card">
			<h2 style="margin-top:0">Fear &amp; Greed (market sentiment)</h2>
			<LineChart
				data={data.fearGreed}
				height={200}
				color="var(--amber)"
				yMin={-1}
				yMax={1}
				baseline={0}
				format={(y) => y.toFixed(2)}
			/>
			<p class="sub" style="margin:0.5rem 0 0">−1 extreme fear · 0 neutral · +1 extreme greed</p>
		</div>
	</div>

	<h2>Data coverage</h2>
	<div class="card">
		<div class="table-wrap">
			<table>
				<thead>
					<tr><th>Table</th><th>Rows</th><th>From</th><th>To</th></tr>
				</thead>
				<tbody>
					{#each data.coverage as c (c.table)}
						<tr>
							<td class="mono">{c.table}</td>
							<td class="mono">{rows(c.table)}</td>
							<td class="mono muted">{fmtDay(c.min_ts)}</td>
							<td class="mono muted">{fmtDay(c.max_ts)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<p class="sub" style="margin:0.6rem 0 0">Source: <span class="mono">{data.dbPath}</span></p>
	</div>
</div>
