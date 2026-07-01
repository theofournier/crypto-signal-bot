import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol');
	const signals = db.signals(symbol, 300);
	return {
		symbols,
		symbol,
		threshold: db.inferredThreshold(),
		stats: db.signalStats(),
		signals
	};
};
