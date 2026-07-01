import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';
import { resolveRange } from '$lib/server/range';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol');
	const range = resolveRange(url, db.tableRange('signals'), 30);
	const signals = range
		? db.signals(symbol, { from: range.fromTs, to: range.toTs, limit: 1000 })
		: db.signals(symbol, { limit: 300 });
	return {
		symbols,
		symbol,
		range,
		threshold: db.inferredThreshold(),
		stats: db.signalStats(),
		signals
	};
};
