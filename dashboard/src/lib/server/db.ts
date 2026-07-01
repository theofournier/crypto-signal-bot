/**
 * Read-only access to the bot's SQLite journal (`storage.db`).
 *
 * Uses Node's built-in `node:sqlite` (Node 22.5+/24) so the dashboard needs no
 * native build step. The DB is the single source of truth (PLAN.md §5.2) and the
 * dashboard is "just another reader" (PLAN.md §10) — it never writes.
 */
import { DatabaseSync } from 'node:sqlite';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { env } from '$env/dynamic/private';

const here = dirname(fileURLToPath(import.meta.url));
// dashboard/src/lib/server -> project root is four levels up.
const DEFAULT_DB = resolve(here, '../../../../storage.db');
const DB_PATH = env.CRYPTOBOT_DB ? resolve(env.CRYPTOBOT_DB) : DEFAULT_DB;

let _db: DatabaseSync | null = null;

function db(): DatabaseSync {
	if (_db) return _db;
	if (!existsSync(DB_PATH)) {
		throw new Error(
			`storage.db not found at ${DB_PATH}. Set CRYPTOBOT_DB to its path if it lives elsewhere.`
		);
	}
	_db = new DatabaseSync(DB_PATH, { readOnly: true });
	return _db;
}

export function dbPath(): string {
	return DB_PATH;
}

function all<T = Record<string, unknown>>(sql: string, ...params: (string | number)[]): T[] {
	return db().prepare(sql).all(...params) as T[];
}
function one<T = Record<string, unknown>>(sql: string, ...params: (string | number)[]): T | undefined {
	return db().prepare(sql).get(...params) as T | undefined;
}

// ── Types ───────────────────────────────────────────────────────────────
export interface Coverage {
	table: string;
	rows: number;
	min_ts: number | null;
	max_ts: number | null;
}
export interface Signal {
	id: number;
	ts: number;
	symbol: string;
	market_sub: number | null;
	onchain_sub: number | null;
	sentiment_sub: number | null;
	composite: number | null;
	direction: string;
	gate_passed: number;
	reason: string | null;
}
export interface Candle {
	ts: number;
	symbol: string;
	timeframe: string;
	open: number;
	high: number;
	low: number;
	close: number;
	volume: number;
	volume_ratio: number | null;
	bid_ask_imbalance: number | null;
	bb_width: number | null;
	rsi: number | null;
	vwap_distance: number | null;
}
export interface Onchain {
	ts: number;
	symbol: string;
	exchange_inflow: number | null;
	exchange_outflow: number | null;
	net_flow: number | null;
	whale_tx_count: number | null;
	flow_signal: string | null;
}
export interface Sentiment {
	ts: number;
	symbol: string;
	sentiment_score: number | null;
	credibility: number | null;
	novelty: number | null;
	mention_count: number | null;
	source: string;
}
export interface Trade {
	id: number;
	symbol: string;
	direction: string;
	mode: string;
	entry_ts: number | null;
	entry_price: number | null;
	size: number | null;
	stop_loss: number | null;
	take_profit: number | null;
	exit_ts: number | null;
	exit_price: number | null;
	exit_reason: string | null;
	pnl: number | null;
	pnl_pct: number | null;
	status: string;
	win: number | null;
}

// ── Overview ────────────────────────────────────────────────────────────
const TABLES = ['market_data', 'onchain_data', 'sentiment_data', 'signals', 'trades'] as const;

export function coverage(): Coverage[] {
	return TABLES.map((t) => {
		const hasTs = t !== 'trades';
		const row = one<{ rows: number; min_ts: number | null; max_ts: number | null }>(
			hasTs
				? `SELECT COUNT(*) AS rows, MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM ${t}`
				: `SELECT COUNT(*) AS rows, MIN(entry_ts) AS min_ts, MAX(COALESCE(exit_ts, entry_ts)) AS max_ts FROM ${t}`
		);
		return { table: t, rows: row?.rows ?? 0, min_ts: row?.min_ts ?? null, max_ts: row?.max_ts ?? null };
	});
}

export function symbols(): string[] {
	return all<{ symbol: string }>(
		'SELECT DISTINCT symbol FROM market_data ORDER BY symbol'
	).map((r) => r.symbol);
}

/** Latest signal evaluation per symbol. */
export function latestSignals(): Signal[] {
	return all<Signal>(
		`SELECT s.* FROM signals s
		 JOIN (SELECT symbol, MAX(ts) AS mts FROM signals GROUP BY symbol) m
		   ON s.symbol = m.symbol AND s.ts = m.mts
		 ORDER BY s.composite DESC`
	);
}

/** Latest candle per symbol (for a price/indicator snapshot). */
export function latestCandles(): Candle[] {
	return all<Candle>(
		`SELECT c.* FROM market_data c
		 JOIN (SELECT symbol, MAX(ts) AS mts FROM market_data GROUP BY symbol) m
		   ON c.symbol = m.symbol AND c.ts = m.mts
		 ORDER BY c.symbol`
	);
}

// ── Signals ─────────────────────────────────────────────────────────────
export function signals(symbol: string | null, limit = 200): Signal[] {
	if (symbol) {
		return all<Signal>(
			'SELECT * FROM signals WHERE symbol = ? ORDER BY ts DESC, id DESC LIMIT ?',
			symbol,
			limit
		);
	}
	return all<Signal>('SELECT * FROM signals ORDER BY ts DESC, id DESC LIMIT ?', limit);
}

export function signalStats(): { total: number; gated: number; avg_composite: number | null; max_composite: number | null } {
	return (
		one(
			`SELECT COUNT(*) AS total,
			        SUM(gate_passed) AS gated,
			        AVG(composite) AS avg_composite,
			        MAX(composite) AS max_composite
			 FROM signals`
		) ?? { total: 0, gated: 0, avg_composite: null, max_composite: null }
	);
}

/** Pull the configured threshold out of a stored reason string, if present. */
export function inferredThreshold(): number | null {
	const row = one<{ reason: string | null }>(
		"SELECT reason FROM signals WHERE reason LIKE '%threshold%' ORDER BY id DESC LIMIT 1"
	);
	const m = row?.reason?.match(/threshold\s+([\d.]+)/i);
	return m ? Number(m[1]) : null;
}

// ── Market ──────────────────────────────────────────────────────────────
export function candles(symbol: string, limit = 500): Candle[] {
	const rows = all<Candle>(
		'SELECT * FROM market_data WHERE symbol = ? ORDER BY ts DESC LIMIT ?',
		symbol,
		limit
	);
	return rows.reverse();
}

// ── On-chain ────────────────────────────────────────────────────────────
export function onchain(symbol: string | null, limit = 500): Onchain[] {
	const rows = symbol
		? all<Onchain>('SELECT * FROM onchain_data WHERE symbol = ? ORDER BY ts DESC LIMIT ?', symbol, limit)
		: all<Onchain>('SELECT * FROM onchain_data ORDER BY ts DESC LIMIT ?', limit);
	return rows.reverse();
}

export function flowDistribution(): { flow_signal: string; n: number }[] {
	return all<{ flow_signal: string; n: number }>(
		"SELECT COALESCE(flow_signal,'unknown') AS flow_signal, COUNT(*) AS n FROM onchain_data GROUP BY flow_signal ORDER BY n DESC"
	);
}

// ── Sentiment ───────────────────────────────────────────────────────────
export function sentimentSources(): { source: string; n: number }[] {
	return all<{ source: string; n: number }>(
		'SELECT source, COUNT(*) AS n FROM sentiment_data GROUP BY source ORDER BY n DESC'
	);
}

/** Fear & Greed style market-wide series (source = fear_greed). */
export function fearGreedSeries(limit = 400): Sentiment[] {
	const rows = all<Sentiment>(
		"SELECT * FROM sentiment_data WHERE source = 'fear_greed' ORDER BY ts DESC LIMIT ?",
		limit
	);
	return rows.reverse();
}

export function sentimentForSymbol(symbol: string, limit = 500): Sentiment[] {
	const rows = all<Sentiment>(
		"SELECT * FROM sentiment_data WHERE symbol = ? AND source != 'fear_greed' ORDER BY ts DESC LIMIT ?",
		symbol,
		limit
	);
	return rows.reverse();
}

export function sentimentSymbols(): string[] {
	return all<{ symbol: string }>(
		"SELECT DISTINCT symbol FROM sentiment_data WHERE source != 'fear_greed' ORDER BY symbol"
	).map((r) => r.symbol);
}

// ── Trades ──────────────────────────────────────────────────────────────
export function trades(limit = 500): Trade[] {
	return all<Trade>('SELECT * FROM trades ORDER BY COALESCE(entry_ts, id) DESC LIMIT ?', limit);
}

export interface TradeStats {
	total: number;
	closed: number;
	open: number;
	wins: number;
	losses: number;
	win_rate: number | null;
	net_pnl: number | null;
	avg_win: number | null;
	avg_loss: number | null;
	max_drawdown: number | null;
}

export function tradeStats(): TradeStats {
	const base = one<{ total: number; closed: number; open: number; wins: number; net_pnl: number | null }>(
		`SELECT COUNT(*) AS total,
		        SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
		        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
		        SUM(CASE WHEN win=1 THEN 1 ELSE 0 END) AS wins,
		        SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END) AS net_pnl
		 FROM trades`
	) ?? { total: 0, closed: 0, open: 0, wins: 0, net_pnl: null };
	const avgWin = one<{ v: number | null }>("SELECT AVG(pnl) AS v FROM trades WHERE status='closed' AND win=1")?.v ?? null;
	const avgLoss = one<{ v: number | null }>("SELECT AVG(pnl) AS v FROM trades WHERE status='closed' AND win=0")?.v ?? null;
	const closed = base.closed ?? 0;
	const wins = base.wins ?? 0;
	const losses = closed - wins;

	// Equity-curve max drawdown over closed trades in chronological order.
	const pnls = all<{ pnl: number | null }>(
		"SELECT pnl FROM trades WHERE status='closed' ORDER BY COALESCE(exit_ts, entry_ts, id)"
	).map((r) => r.pnl ?? 0);
	let equity = 0;
	let peak = 0;
	let maxDd = 0;
	for (const p of pnls) {
		equity += p;
		if (equity > peak) peak = equity;
		if (peak - equity > maxDd) maxDd = peak - equity;
	}

	return {
		total: base.total ?? 0,
		closed,
		open: base.open ?? 0,
		wins,
		losses,
		win_rate: closed > 0 ? wins / closed : null,
		net_pnl: base.net_pnl ?? null,
		avg_win: avgWin,
		avg_loss: avgLoss,
		max_drawdown: pnls.length ? maxDd : null
	};
}

/** Cumulative net-P&L points (chronological) for the equity curve. */
export function equityCurve(): { ts: number; equity: number }[] {
	const rows = all<{ ts: number | null; pnl: number | null }>(
		"SELECT COALESCE(exit_ts, entry_ts) AS ts, pnl FROM trades WHERE status='closed' ORDER BY COALESCE(exit_ts, entry_ts, id)"
	);
	let equity = 0;
	return rows.map((r) => {
		equity += r.pnl ?? 0;
		return { ts: r.ts ?? 0, equity };
	});
}
