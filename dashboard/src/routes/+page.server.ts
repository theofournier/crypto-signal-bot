import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = () => {
	return {
		dbPath: db.dbPath(),
		coverage: db.coverage(),
		latestSignals: db.latestSignals(),
		signalStats: db.signalStats(),
		tradeStats: db.tradeStats(),
		threshold: db.inferredThreshold(),
		fearGreed: db.fearGreedSeries(240).map((s) => ({ x: s.ts, y: s.sentiment_score ?? 0 }))
	};
};
