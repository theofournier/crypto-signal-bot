import type { PageServerLoad } from './$types';
import * as db from '$lib/server/db';
import { resolveRange } from '$lib/server/range';

export const load: PageServerLoad = ({ url }) => {
	const symbols = db.symbols();
	const symbol = url.searchParams.get('symbol') ?? symbols[0] ?? '';
	const range = resolveRange(url, db.tableRange('market_data'), 14);
	const candles =
		symbol && range ? db.candles(symbol, { from: range.fromTs, to: range.toTs }) : [];
	const latest = candles.at(-1) ?? null;
	return { symbols, symbol, range, candles, latest };
};
