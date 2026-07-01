<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { addDays, daysBetween, clampDay } from '$lib/daterange';

	interface Props {
		from: string;
		to: string;
		min: string;
		max: string;
	}
	let { from, to, min, max }: Props = $props();

	/** window length in days, inclusive of both ends */
	const span = $derived(daysBetween(from, to) + 1);
	const atStart = $derived(from <= min);
	const atEnd = $derived(to >= max);

	function apply(nf: string, nt: string) {
		const p = new URLSearchParams(page.url.searchParams);
		p.set('from', clampDay(nf, min, max));
		p.set('to', clampDay(nt, min, max));
		goto(`?${p.toString()}`, { keepFocus: true, noScroll: true });
	}

	/** slide the whole window by its own length (paging through history) */
	function step(dir: number) {
		const shifted = span * dir;
		let nf = addDays(from, shifted);
		let nt = addDays(to, shifted);
		// keep the window length constant when it bumps a boundary
		if (nf < min) {
			nf = min;
			nt = addDays(min, span - 1);
		}
		if (nt > max) {
			nt = max;
			nf = addDays(max, -(span - 1));
		}
		apply(nf, nt);
	}

	function preset(days: number) {
		apply(addDays(max, -(days - 1)), max);
	}
	function all() {
		apply(min, max);
	}

	function onFrom(e: Event) {
		apply((e.currentTarget as HTMLInputElement).value, to);
	}
	function onTo(e: Event) {
		apply(from, (e.currentTarget as HTMLInputElement).value);
	}
</script>

<div class="datefilter">
	<button class="nav" onclick={() => step(-1)} disabled={atStart} title="Previous {span} days">◀</button>
	<div class="inputs">
		<input type="date" value={from} {min} {max} onchange={onFrom} aria-label="from date" />
		<span class="arrow">→</span>
		<input type="date" value={to} {min} {max} onchange={onTo} aria-label="to date" />
	</div>
	<button class="nav" onclick={() => step(1)} disabled={atEnd} title="Next {span} days">▶</button>

	<span class="span">{span}d</span>

	<div class="presets">
		<button onclick={() => preset(1)}>1d</button>
		<button onclick={() => preset(7)}>7d</button>
		<button onclick={() => preset(30)}>30d</button>
		<button onclick={all}>All</button>
	</div>
</div>

<style>
	.datefilter {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		background: var(--bg-elev);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0.45rem 0.6rem;
	}
	.inputs {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	.arrow {
		color: var(--muted);
	}
	input[type='date'] {
		background: var(--bg-elev2);
		color: var(--text);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.3rem 0.5rem;
		font-size: 0.85rem;
		font-variant-numeric: tabular-nums;
		color-scheme: dark;
	}
	.nav {
		padding: 0.3rem 0.6rem;
		font-size: 0.85rem;
	}
	.nav:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.span {
		color: var(--muted);
		font-size: 0.78rem;
		min-width: 2.4rem;
		text-align: center;
		font-variant-numeric: tabular-nums;
	}
	.presets {
		display: flex;
		gap: 0.25rem;
		margin-left: auto;
	}
	.presets button {
		padding: 0.25rem 0.5rem;
		font-size: 0.78rem;
	}
</style>
