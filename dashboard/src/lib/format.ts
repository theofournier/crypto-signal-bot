/** Small, dependency-free formatting helpers shared across pages. */

export function fmtDate(ts: number | null | undefined): string {
	if (!ts) return '—';
	return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ');
}

export function fmtDay(ts: number | null | undefined): string {
	if (!ts) return '—';
	return new Date(ts * 1000).toISOString().slice(0, 10);
}

export function fmtNum(n: number | null | undefined, digits = 2): string {
	if (n === null || n === undefined || Number.isNaN(n)) return '—';
	return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtPrice(n: number | null | undefined): string {
	if (n === null || n === undefined) return '—';
	const digits = n >= 100 ? 2 : n >= 1 ? 4 : 6;
	return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

/** Compact USD like $1.2M / $3.4K. */
export function fmtUsd(n: number | null | undefined): string {
	if (n === null || n === undefined || Number.isNaN(n)) return '—';
	const sign = n < 0 ? '-' : '';
	const a = Math.abs(n);
	if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
	if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(2)}M`;
	if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(1)}K`;
	return `${sign}$${a.toFixed(0)}`;
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
	if (n === null || n === undefined || Number.isNaN(n)) return '—';
	return `${n >= 0 ? '' : ''}${(n * 100).toFixed(digits)}%`;
}

/** Colour for a 0–100 sub/composite score. */
export function scoreColor(n: number | null | undefined): string {
	if (n === null || n === undefined) return 'var(--muted)';
	if (n >= 72) return 'var(--green)';
	if (n >= 55) return 'var(--amber)';
	return 'var(--red)';
}

export function directionColor(dir: string | null | undefined): string {
	if (dir === 'long') return 'var(--green)';
	if (dir === 'bearish' || dir === 'distribution') return 'var(--red)';
	return 'var(--muted)';
}
