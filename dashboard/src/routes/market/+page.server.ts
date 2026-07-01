import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? '';
	const limit = Number(url.searchParams.get('limit') ?? 300);
	const candles = symbol ? db.candles(symbol, limit) : [];
	const latest = candles.at(-1) ?? null;
	return { symbols, symbol, limit, candles, latest };
};
