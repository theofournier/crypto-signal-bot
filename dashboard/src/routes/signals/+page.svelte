<script lang="ts">
	import type { PageProps } from './$types';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import ScoreBar from '$lib/components/ScoreBar.svelte';
	import LineChart from '$lib/components/LineChart.svelte';
	import DateFilter from '$lib/components/DateFilter.svelte';
	import { fmtDate, fmtNum, directionColor } from '$lib/format';

	let { data }: PageProps = $props();

	function filter(e: Event) {
		const p = new URLSearchParams(page.url.searchParams);
		const v = (e.currentTarget as HTMLSelectElement).value;
		if (v) p.set('symbol', v);
		else p.delete('symbol');
		goto(`?${p.toString()}`, { keepFocus: true, noScroll: true });
	}

	// composite trend is only meaningful for a single symbol (chronological)
	const trend = $derived(
		data.symbol
			? [...data.signals].reverse().map((s) => ({ x: s.ts, y: s.composite ?? 0 }))
			: []
	);

	let openId = $state<number | null>(null);
	function toggle(id: number) {
		openId = openId === id ? null : id;
	}
</script>

<div class="page">
	<h1>Signals</h1>
	<p class="sub">
		Every evaluation — firing and non-firing (FR-SG-4). A signal fires only above the threshold
		with all sources agreeing long.
	</p>

	<div class="controls">
		<label>Symbol
			<select onchange={filter} value={data.symbol ?? ''}>
				<option value="">All symbols</option>
				{#each data.symbols as s (s)}<option value={s}>{s}</option>{/each}
			</select>
		</label>
		{#if data.range}
			<DateFilter from={data.range.from} to={data.range.to} min={data.range.min} max={data.range.max} />
		{/if}
	</div>
	<p class="sub" style="margin:-0.5rem 0 1rem">
		{fmtNum(data.signals.length, 0)} in range · {fmtNum(data.stats.total, 0)} total ·
		{fmtNum(data.stats.gated ?? 0, 0)} gated · threshold <span class="mono">{data.threshold ?? '—'}</span>
	</p>

	{#if data.symbol && trend.length > 1}
		<div class="card" style="margin-bottom:1rem">
			<h2 style="margin-top:0">{data.symbol} — composite over time</h2>
			<LineChart data={trend} height={180} color="var(--accent)" yMin={0} yMax={100} baseline={data.threshold} format={(y) => y.toFixed(1)} />
		</div>
	{/if}

	<div class="card">
		{#if data.signals.length === 0}
			<div class="empty">no signals</div>
		{:else}
			<div class="table-wrap">
				<table>
					<thead>
						<tr>
							<th>Time</th>
							<th>Symbol</th>
							<th>Composite</th>
							<th>Mkt</th>
							<th>Chain</th>
							<th>Sent</th>
							<th>Dir</th>
							<th>Gate</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each data.signals as s (s.id)}
							<tr>
								<td class="mono muted">{fmtDate(s.ts)}</td>
								<td>{s.symbol}</td>
								<td style="min-width:150px"><ScoreBar value={s.composite} threshold={data.threshold} /></td>
								<td class="mono">{fmtNum(s.market_sub, 0)}</td>
								<td class="mono">{fmtNum(s.onchain_sub, 0)}</td>
								<td class="mono">{fmtNum(s.sentiment_sub, 0)}</td>
								<td><span style:color={directionColor(s.direction)}>{s.direction}</span></td>
								<td>
									<span class="pill {s.gate_passed ? 'green' : 'muted'}">{s.gate_passed ? 'fired' : 'held'}</span>
								</td>
								<td>
									{#if s.reason}
										<button class="link" onclick={() => toggle(s.id)}>{openId === s.id ? 'hide' : 'why'}</button>
									{/if}
								</td>
							</tr>
							{#if openId === s.id && s.reason}
								<tr class="reason-row">
									<td colspan="9"><div class="reason mono">{s.reason}</div></td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>

<style>
	button.link {
		background: none;
		border: none;
		color: var(--accent);
		padding: 0;
		font-size: 0.8rem;
	}
	button.link:hover {
		text-decoration: underline;
	}
	.reason-row td {
		text-align: left;
		background: var(--bg);
	}
	.reason {
		white-space: pre-wrap;
		font-size: 0.8rem;
		color: var(--muted);
		padding: 0.3rem 0;
		line-height: 1.6;
	}
</style>
