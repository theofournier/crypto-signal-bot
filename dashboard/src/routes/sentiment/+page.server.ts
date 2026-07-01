import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';
import { resolveRange } from '$lib/server/range';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.sentimentSymbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? null;
	const range = resolveRange(url, db.tableRange('sentiment_data'), 30);
	const rangeOpt = range ? { from: range.fromTs, to: range.toTs } : {};
	return {
		sources: db.sentimentSources(),
		range,
		fearGreed: db.fearGreedSeries(rangeOpt).map((s) => ({ x: s.ts, y: s.sentiment_score ?? 0 })),
		symbols,
		symbol,
		series: symbol ? db.sentimentForSymbol(symbol, rangeOpt) : []
	};
};
