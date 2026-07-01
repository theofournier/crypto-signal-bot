import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? null;
	return {
		symbols,
		symbol,
		distribution: db.flowDistribution(),
		rows: symbol ? db.onchain(symbol, 400) : []
	};
};
