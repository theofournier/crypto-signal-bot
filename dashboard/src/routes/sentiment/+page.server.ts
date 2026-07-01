import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.sentimentSymbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? null;
	return {
		sources: db.sentimentSources(),
		fearGreed: db.fearGreedSeries(400).map((s) => ({ x: s.ts, y: s.sentiment_score ?? 0 })),
		symbols,
		symbol,
		series: symbol ? db.sentimentForSymbol(symbol, 500) : []
	};
};
