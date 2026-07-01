<script lang="ts">
	import type { PageProps } from './$types';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import LineChart from '$lib/components/LineChart.svelte';
	import DateFilter from '$lib/components/DateFilter.svelte';
	import { fmtDate, fmtUsd, fmtNum } from '$lib/format';

	let { data }: PageProps = $props();

	function pick(e: Event) {
		const p = new URLSearchParams(page.url.searchParams);
		p.set('symbol', (e.currentTarget as HTMLSelectElement).value);
		goto(`?${p.toString()}`, { keepFocus: true, noScroll: true });
	}

	const netFlow = $derived(data.rows.map((r) => ({ x: r.ts, y: r.net_flow ?? 0 })));
	const whales = $derived(data.rows.map((r) => ({ x: r.ts, y: r.whale_tx_count ?? 0 })));
	const totalDist = $derived(data.distribution.reduce((a, d) => a + d.n, 0));
	const recent = $derived([...data.rows].reverse().slice(0, 60));

	function flowClass(f: string | null): string {
		if (f === 'accumulation') return 'green';
		if (f === 'distribution') return 'red';
		return 'muted';
	}
</script>

<div class="page">
	<h1>On-chain</h1>
	<p class="sub">Exchange in/out flows and whale activity, reduced to an accumulation/distribution reading.</p>

	<h2>Flow signal mix (all symbols)</h2>
	<div class="card">
		<div class="bars">
			{#each data.distribution as d (d.flow_signal)}
				<div class="bar-row">
					<span class="lbl">
						<span class="pill {flowClass(d.flow_signal)}">{d.flow_signal}</span>
					</span>
					<div class="bar-track">
						<div class="bar-fill {flowClass(d.flow_signal)}" style:width="{totalDist ? (d.n / totalDist) * 100 : 0}%"></div>
					</div>
					<span class="mono val">{fmtNum(d.n, 0)}</span>
				</div>
			{/each}
		</div>
	</div>

	<div class="controls" style="margin-top:1rem">
		<label>Symbol
			<select onchange={pick} value={data.symbol ?? ''}>
				{#each data.symbols as s (s)}<option value={s}>{s}</option>{/each}
			</select>
		</label>
		{#if data.range}
			<DateFilter from={data.range.from} to={data.range.to} min={data.range.min} max={data.range.max} />
		{/if}
	</div>

	{#if data.rows.length === 0}
		<div class="card"><div class="empty">no on-chain rows for this symbol</div></div>
	{:else}
		<div class="grid cols-2">
			<div class="card">
				<h2 style="margin-top:0">Net flow (outflow − inflow)</h2>
				<LineChart data={netFlow} height={180} color="var(--accent)" baseline={0} format={fmtUsd} />
				<p class="sub" style="margin:0.4rem 0 0">Positive = leaving exchanges (accumulation-leaning).</p>
			</div>
			<div class="card">
				<h2 style="margin-top:0">Whale transfers</h2>
				<LineChart data={whales} height={180} color="var(--amber)" format={(y) => fmtNum(y, 0)} />
			</div>
		</div>

		<h2>Recent observations</h2>
		<div class="card">
			<div class="table-wrap">
				<table>
					<thead>
						<tr><th>Time</th><th>Inflow</th><th>Outflow</th><th>Net flow</th><th>Whales</th><th>Signal</th></tr>
					</thead>
					<tbody>
						{#each recent as r (r.ts)}
							<tr>
								<td class="mono muted">{fmtDate(r.ts)}</td>
								<td class="mono">{fmtUsd(r.exchange_inflow)}</td>
								<td class="mono">{fmtUsd(r.exchange_outflow)}</td>
								<td class="mono" class:pos={(r.net_flow ?? 0) >= 0} class:neg={(r.net_flow ?? 0) < 0}>{fmtUsd(r.net_flow)}</td>
								<td class="mono">{fmtNum(r.whale_tx_count, 0)}</td>
								<td><span class="pill {flowClass(r.flow_signal)}">{r.flow_signal ?? '—'}</span></td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}
</div>

<style>
	.bars {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.bar-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	.lbl {
		width: 130px;
	}
	.bar-track {
		flex: 1;
		height: 12px;
		background: var(--bg-elev2);
		border-radius: 999px;
		overflow: hidden;
	}
	.bar-fill {
		height: 100%;
	}
	.bar-fill.green {
		background: var(--green);
	}
	.bar-fill.red {
		background: var(--red);
	}
	.bar-fill.muted {
		background: var(--muted);
	}
	.val {
		width: 4rem;
		text-align: right;
	}
</style>
