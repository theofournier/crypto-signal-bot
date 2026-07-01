<script lang="ts">
	import type { PageProps } from './$types';
	import KpiCard from '$lib/components/KpiCard.svelte';
	import LineChart from '$lib/components/LineChart.svelte';
	import { fmtDate, fmtPrice, fmtUsd, fmtNum, fmtPct } from '$lib/format';

	let { data }: PageProps = $props();

	const s = $derived(data.stats);
	const equity = $derived(data.equity.map((p) => ({ x: p.ts, y: p.equity })));
	const winRate = $derived(s.win_rate === null ? '—' : `${(s.win_rate * 100).toFixed(1)}%`);
	const wl = $derived(s.avg_loss ? Math.abs((s.avg_win ?? 0) / s.avg_loss) : null);

	// verdict banner per FR-LE-4 sample-size rule
	const verdict = $derived(
		s.closed >= 100
			? { txt: 'Sample sufficient for a verdict (≥100 closed trades).', cls: 'ok' }
			: s.closed >= 30
				? { txt: `Weak preliminary read only — ${s.closed}/100 closed trades.`, cls: 'warn' }
				: { txt: `Below 30 closed trades this is noise, not evidence (${s.closed} so far).`, cls: 'warn' }
	);
</script>

<div class="page">
	<h1>Trades</h1>
	<p class="sub">The journal (net of fees) and the equity curve it produces.</p>

	<div class="grid kpis">
		<KpiCard label="Closed / open" value="{fmtNum(s.closed, 0)} / {fmtNum(s.open, 0)}" />
		<KpiCard label="Win rate" value={winRate} hint="{s.wins}W · {s.losses}L" tone="muted" />
		<KpiCard label="Avg win ÷ avg loss" value={wl === null ? '—' : `${wl.toFixed(2)}×`} />
		<KpiCard label="Net P&L" value={s.net_pnl === null ? '—' : fmtUsd(s.net_pnl)} tone={s.net_pnl === null ? 'muted' : s.net_pnl >= 0 ? 'pos' : 'neg'} />
		<KpiCard label="Max drawdown" value={s.max_drawdown === null ? '—' : fmtUsd(-s.max_drawdown)} tone={s.max_drawdown ? 'neg' : 'muted'} />
	</div>

	{#if s.total > 0}
		<div class="banner {verdict.cls}">{verdict.txt}</div>
	{/if}

	<div class="card" style="margin-top:1rem">
		<h2 style="margin-top:0">Equity curve (cumulative net P&L)</h2>
		{#if equity.length > 1}
			<LineChart data={equity} height={220} color="var(--green)" baseline={0} format={fmtUsd} />
		{:else}
			<div class="empty">No closed trades yet — the equity curve appears once trades close.</div>
		{/if}
	</div>

	<h2>Journal</h2>
	<div class="card">
		{#if data.trades.length === 0}
			<div class="empty">
				No trades recorded. Run the engine in dry-run (<span class="mono">scripts/run_engine.py</span>) or a
				backtest to populate this table.
			</div>
		{:else}
			<div class="table-wrap">
				<table>
					<thead>
						<tr>
							<th>Symbol</th>
							<th>Mode</th>
							<th>Entry</th>
							<th>Entry px</th>
							<th>Exit</th>
							<th>Exit px</th>
							<th>Reason</th>
							<th>P&L</th>
							<th>%</th>
							<th>Status</th>
						</tr>
					</thead>
					<tbody>
						{#each data.trades as t (t.id)}
							<tr>
								<td>{t.symbol}</td>
								<td><span class="pill {t.mode === 'live' ? 'red' : 'muted'}">{t.mode}</span></td>
								<td class="mono muted">{fmtDate(t.entry_ts)}</td>
								<td class="mono">{fmtPrice(t.entry_price)}</td>
								<td class="mono muted">{fmtDate(t.exit_ts)}</td>
								<td class="mono">{fmtPrice(t.exit_price)}</td>
								<td class="muted">{t.exit_reason ?? '—'}</td>
								<td class="mono" class:pos={(t.pnl ?? 0) >= 0} class:neg={(t.pnl ?? 0) < 0}>{t.pnl === null ? '—' : fmtUsd(t.pnl)}</td>
								<td class="mono" class:pos={(t.pnl_pct ?? 0) >= 0} class:neg={(t.pnl_pct ?? 0) < 0}>{t.pnl_pct === null ? '—' : fmtPct(t.pnl_pct / 100)}</td>
								<td><span class="pill {t.status === 'open' ? 'green' : 'muted'}">{t.status}</span></td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>

<style>
	.banner {
		margin-top: 1rem;
		padding: 0.6rem 0.9rem;
		border-radius: var(--radius);
		font-size: 0.85rem;
		border: 1px solid var(--border);
	}
	.banner.ok {
		color: var(--green);
		border-color: color-mix(in srgb, var(--green) 40%, transparent);
		background: color-mix(in srgb, var(--green) 10%, transparent);
	}
	.banner.warn {
		color: var(--amber);
		border-color: color-mix(in srgb, var(--amber) 40%, transparent);
		background: color-mix(in srgb, var(--amber) 10%, transparent);
	}
</style>
